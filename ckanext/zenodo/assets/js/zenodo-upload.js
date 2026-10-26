/**
 * Built-in Zenodo upload for CKAN's own "new resource" form, for sites
 * without ckanext-fair3r (its resource-upload.js does this and more, and
 * the template only renders the markup this script drives when fair3r is
 * not enabled - see templates/package/snippets/resource_form.html).
 *
 * A file that belongs on Zenodo is sent with XMLHttpRequest (the only way to
 * get upload-progress events) to this plugin's /zenodo/upload/<dataset>
 * endpoint instead of being posted with the form. Anything else submits the
 * form natively, exactly as without this script. One file at a time: CKAN's
 * form has a single file field.
 *
 * Deliberately not here (fair3r has them): several files at once, the live
 * capacity badge, the "file with this name already exists" warning.
 */
(function () {
  "use strict";

  // Where a file goes. UX only: the endpoint re-derives it server-side
  // (blueprints/zenodo_upload.py) and never trusts this decision.
  function shouldRouteToZenodo(file, config, forced) {
    if (forced) {
      return true;
    }
    if (!config) {
      // Can't tell: stay on CKAN rather than surprise the user.
      return false;
    }
    if (file.size > config.max_resource_size_bytes) {
      return true;
    }
    var ext = (file.name.split(".").pop() || "").toLowerCase();
    var type = (file.type || "").toLowerCase();
    var readable = config.xloader_formats.some(function (f) {
      return f.toLowerCase() === ext || f.toLowerCase() === type;
    });
    return !readable;
  }

  function init() {
    var box = document.getElementById("zenodo-upload-option");
    var form = document.getElementById("resource-edit");
    if (!box || !form) {
      return;
    }

    var config = JSON.parse(box.dataset.config);
    var forceBox = document.getElementById("zenodo-force");
    var hint = document.getElementById("zenodo-hint");
    var progress = document.getElementById("zenodo-progress");
    var errorBox = document.getElementById("zenodo-error");
    var cancelButton = document.getElementById("zenodo-cancel");
    var controller = null;

    function csrfToken() {
      var field = document.querySelector('meta[name="csrf_field_name"]');
      var token = field && document.querySelector('meta[name="' + field.content + '"]');
      return token ? token.content : null;
    }

    // Looked up each time: CKAN's "Remove" button can replace this element.
    function selectedFile() {
      var input = document.getElementById("field-resource-upload");
      return (input && input.files && input.files[0]) || null;
    }

    function saveButtons() {
      return form.querySelectorAll('button[name="save"]');
    }

    function setBusy(busy) {
      Array.prototype.forEach.call(saveButtons(), function (button) {
        button.disabled = busy;
      });
      progress.hidden = !busy;
      cancelButton.hidden = !busy;
      if (busy) {
        progress.value = 0;
        errorBox.hidden = true;
      }
    }

    function showError(message) {
      errorBox.textContent = message;
      errorBox.hidden = false;
      setBusy(false);
      progress.hidden = true;
    }

    function updateHint() {
      var file = selectedFile();
      var routed = file && shouldRouteToZenodo(file, config, forceBox.checked);
      hint.textContent = routed ? box.dataset.msgRouted : box.dataset.msgStays;
    }

    // In chunks, see zenodo-chunked-upload.js (same bundle).
    function upload(file, forced, onDone) {
      controller = new AbortController();
      window.ZenodoChunkedUpload.upload(file, box.dataset.packageName, {
        forced: forced,
        signal: controller.signal,
        onProgress: function (percent) {
          progress.value = percent;
        }
      }).then(function (result) {
        onDone(result.ok ? null : result.errorMessage || box.dataset.msgFailed);
      });
    }

    // A Zenodo file creates no CKAN resource until the dataset is public, so
    // CKAN's own "Finish" would refuse ("add at least one resource"); the
    // draft is activated here instead, with package_revise (a row-locked
    // read-modify-write, unlike package_patch).
    function activateDraft() {
      var headers = { "Content-Type": "application/json" };
      var token = csrfToken();
      if (token) {
        headers["X-CSRFToken"] = token;
      }
      return fetch(box.dataset.reviseUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: JSON.stringify({
          match: { name: box.dataset.packageName },
          update: { state: "active" },
        }),
      }).catch(function () {
        // best effort: the dataset page still shows the uploaded file
      });
    }

    form.addEventListener("change", function (event) {
      if (event.target.id === "field-resource-upload") {
        updateHint();
      }
    });
    forceBox.addEventListener("change", updateHint);
    cancelButton.addEventListener("click", function () {
      if (controller) {
        controller.abort();
      }
    });
    updateHint();

    form.addEventListener("submit", function (event) {
      var submitter = event.submitter;
      var save = submitter ? submitter.value : null;
      var file = selectedFile();
      // "Previous", no file, or a file that stays on CKAN: native submit.
      if (save === "go-dataset" || !file || !shouldRouteToZenodo(file, config, forceBox.checked)) {
        return;
      }
      event.preventDefault();
      setBusy(true);
      hint.textContent = box.dataset.msgUploading;

      upload(file, forceBox.checked, function (error) {
        if (error) {
          updateHint();
          showError(error);
          return;
        }
        var finishing = save === "go-metadata" || save === "go-dataset-complete";
        (finishing ? activateDraft() : Promise.resolve()).then(function () {
          if (save === "again") {
            // a plain reload can restore the previous file selection
            var input = document.getElementById("field-resource-upload");
            if (input) {
              input.value = "";
            }
            window.location.reload();
          } else {
            window.location.href = box.dataset.packageUrl;
          }
        });
      });
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { shouldRouteToZenodo: shouldRouteToZenodo };
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
