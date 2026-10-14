/**
 * Stage 3 design artifact, not shipped code: the pure client-side routing
 * decision for "should this file go to Zenodo instead of CKAN storage?",
 * written and verified in isolation here (run with `node
 * docs/stage3-routing-design.js`) before Stage 7 drops it - unchanged -
 * into ckanext-fair3r's resource-upload.js, right before the existing
 * createOneResource(file, ...) call in its upload forEach loop.
 *
 * No JS test framework exists anywhere in this project's extensions, so
 * this isn't wired into any CI job - it's a reviewable, runnable design
 * check, with worked examples as the "tests". See the implementation
 * plan's Stage 3 section.
 *
 * shouldRouteToZenodo is pure (no DOM, no network) and unit-testable as
 * shown below. zenodoRoutingConfig is NOT pure (reads a <meta> tag from
 * the live page) and can only be verified in a real browser - that
 * happens manually during Stage 7's own verification, not here.
 */

function shouldRouteToZenodo(file, config, forced) {
  if (forced) {
    return true;
  }
  if (!config) {
    // Can't decide - the client-side check is a UX nicety only, never
    // authoritative (the server re-validates size/format for real in
    // Stage 5's blueprint) - default to "stays on CKAN" rather than
    // surprising the user by routing somewhere they didn't ask for.
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

// Reads the <meta name="zenodo-routing-config"> tag ckanext-zenodo's
// zenodo_routing_config_json() helper renders into base.html (Stage 7).
// Needs a live DOM - not exercised by the plain-node checks below.
function zenodoRoutingConfig() {
  var raw = document
    .querySelector('meta[name="zenodo-routing-config"]')
    .getAttribute('content');
  try {
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

/* ---- worked examples, run via `node docs/stage3-routing-design.js` ---- */

if (typeof require !== 'undefined' && require.main === module) {
  var assert = require('assert');

  var config = {
    max_resource_size_bytes: 20 * 1024 * 1024, // 20 MB, see the plan
    xloader_formats: ['csv', 'application/csv'],
  };

  // forced always wins, even for a tiny CSV that would otherwise stay on CKAN.
  assert.strictEqual(
    shouldRouteToZenodo({ name: 'small.csv', size: 10, type: 'text/csv' }, config, true),
    true,
    'forced should always route to Zenodo'
  );

  // No config at all (meta tag missing/malformed) - default to CKAN, never guess.
  assert.strictEqual(
    shouldRouteToZenodo({ name: 'whatever.csv', size: 10 }, null, false),
    false,
    'no config should default to staying on CKAN'
  );

  // Small CSV, within the size limit, xloader-readable format -> stays on CKAN.
  assert.strictEqual(
    shouldRouteToZenodo(
      { name: 'results.csv', size: 1024 * 1024, type: 'text/csv' },
      config,
      false
    ),
    false,
    'a small, xloader-readable CSV should stay on CKAN'
  );

  // Oversized CSV (>20MB) -> Zenodo, even though the format is readable.
  assert.strictEqual(
    shouldRouteToZenodo(
      { name: 'huge.csv', size: 25 * 1024 * 1024, type: 'text/csv' },
      config,
      false
    ),
    true,
    'an oversized file should route to Zenodo regardless of format'
  );

  // Small but non-tabular format (e.g. a ZIP) -> Zenodo.
  assert.strictEqual(
    shouldRouteToZenodo(
      { name: 'archive.zip', size: 1024, type: 'application/zip' },
      config,
      false
    ),
    true,
    'a small but non-xloader-readable format should route to Zenodo'
  );

  // Matches by MIME type too, not just file extension (e.g. no extension
  // at all, but the browser reports the right MIME type).
  assert.strictEqual(
    shouldRouteToZenodo(
      { name: 'noextension', size: 1024, type: 'application/csv' },
      config,
      false
    ),
    false,
    'a recognised MIME type should count even without a matching extension'
  );

  console.log('All stage3-routing-design.js worked examples passed.');
}

module.exports = { shouldRouteToZenodo, zenodoRoutingConfig };
