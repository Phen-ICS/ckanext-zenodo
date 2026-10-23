/**
 * Sends one file to ckanext-zenodo in chunks (see lib/chunked_upload.py):
 * start, then one short PUT per chunk, then finish - the CKAN worker sends
 * the file on to Zenodo. Every request stays well under the web server's
 * 50 s limit, whatever the file's size.
 *
 * A chunk whose request fails (network, server busy) is sent again, with
 * a growing pause; a refused one (permission, limits) stops the upload.
 *
 * Shared by this plugin's own upload form (zenodo-upload.js) and
 * ckanext-fair3r's resource-upload.js:
 *
 *   window.ZenodoChunkedUpload.upload(file, packageName, {
 *     forced: true|false, onProgress: function (percent) {}
 *   }).then(function (result) { result.ok, result.errorMessage });
 */
(function () {
  "use strict";

  var RETRY_DELAYS_MS = [2000, 5000, 10000, 20000, 30000];

  function translate(text) {
    var i18n = window.ckan && window.ckan.i18n;
    return i18n && typeof i18n._ === "function" ? i18n._(text) : text;
  }

  function csrfToken() {
    var field = document.querySelector('meta[name="csrf_field_name"]');
    var token = field && document.querySelector('meta[name="' + field.content + '"]');
    return token ? token.content : null;
  }

  function headers(extra) {
    var result = extra || {};
    var token = csrfToken();
    if (token) {
      result["X-CSRFToken"] = token;
    }
    return result;
  }

  function errorFrom(payload, fallback) {
    var message = payload && payload.error && payload.error.message;
    return typeof message === "string" && message ? message : fallback;
  }

  function wait(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  // Worth sending again: no answer, or the server/proxy was busy.
  function isTransient(status) {
    return status === 0 || status === 429 || status >= 500;
  }

  function putChunk(url, blob, onLoaded) {
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", url, true);
      xhr.withCredentials = true;
      var token = csrfToken();
      if (token) {
        xhr.setRequestHeader("X-CSRFToken", token);
      }
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.addEventListener("progress", function (e) {
        if (e.lengthComputable) {
          onLoaded(e.loaded);
        }
      });
      xhr.onload = function () {
        var payload = null;
        try {
          payload = JSON.parse(xhr.responseText);
        } catch (e) {
          payload = null;
        }
        resolve({ status: xhr.status, payload: payload });
      };
      xhr.onerror = function () {
        resolve({ status: 0, payload: null });
      };
      xhr.send(blob);
    });
  }

  function postJson(url, body, method) {
    return fetch(url, {
      method: method || "POST",
      credentials: "same-origin",
      headers: headers({ "Content-Type": "application/json" }),
      body: body ? JSON.stringify(body) : undefined
    }).then(function (response) {
      return response.json().catch(function () {
        return null;
      }).then(function (payload) {
        return { status: response.status, payload: payload };
      });
    }, function () {
      return { status: 0, payload: null };
    });
  }

  function upload(file, packageName, options) {
    options = options || {};
    var onProgress = typeof options.onProgress === "function" ? options.onProgress : function () {};
    var base = "/zenodo/uploads/" + encodeURIComponent(packageName);
    var failed = translate("Upload to Zenodo failed");

    return postJson(base, {
      filename: file.name,
      size: file.size,
      mimetype: file.type,
      force_zenodo: !!options.forced
    }).then(function (start) {
      if (start.status !== 200 || !start.payload || !start.payload.success) {
        return { ok: false, errorMessage: errorFrom(start.payload, start.status ? failed : translate("Network error")) };
      }
      var uploadUrl = base + "/" + encodeURIComponent(start.payload.result.upload_id);
      var chunkSize = start.payload.result.chunk_size;

      function giveUp(message) {
        // Frees the server's disk space now rather than at the next cleanup.
        postJson(uploadUrl, null, "DELETE");
        return { ok: false, errorMessage: message };
      }

      function sendFrom(offset, attempt) {
        if (offset >= file.size) {
          return postJson(uploadUrl + "/finish").then(function (finish) {
            if (finish.status === 200 && finish.payload && finish.payload.success) {
              onProgress(100);
              return { ok: true, errorMessage: null };
            }
            return giveUp(errorFrom(finish.payload, failed));
          });
        }
        var end = Math.min(offset + chunkSize, file.size);
        return putChunk(uploadUrl + "/chunk?offset=" + offset, file.slice(offset, end), function (loaded) {
          onProgress(Math.floor(((offset + loaded) / file.size) * 99));
        }).then(function (result) {
          if (result.status === 200) {
            return sendFrom(end, 0);
          }
          if (isTransient(result.status) && attempt < RETRY_DELAYS_MS.length) {
            return wait(RETRY_DELAYS_MS[attempt]).then(function () {
              return sendFrom(offset, attempt + 1);
            });
          }
          return giveUp(errorFrom(result.payload, result.status ? failed : translate("Network error")));
        });
      }

      return sendFrom(0, 0);
    });
  }

  window.ZenodoChunkedUpload = { upload: upload };
})();
