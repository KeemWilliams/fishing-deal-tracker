/**
 * MV3 service worker: the ONLY place this extension makes a network
 * request. Content scripts run in the retailer page's origin and are
 * subject to that page's CSP/CORS -- routing the POST through the
 * background worker instead means:
 *   - the ingest endpoint/token never has to be reachable-and-CORS-
 *     enabled from arbitrary retailer origins (no `Access-Control-*`
 *     configuration needed on the server, see fpt/capture/server.py);
 *   - a hostile page's own script cannot observe or tamper with the
 *     request (different execution context, no shared JS heap);
 *   - the bearer token (if configured) is read from chrome.storage.sync
 *     here and never touches the page context at all.
 */
"use strict";

var DEFAULT_ENDPOINT = "http://localhost:8787/v1/captures";

function getOptions() {
  return new Promise(function (resolve) {
    chrome.storage.sync.get({ endpoint: DEFAULT_ENDPOINT, token: "" }, function (items) {
      resolve(items);
    });
  });
}

function postCapture(payload, options) {
  var headers = { "Content-Type": "application/json" };
  if (options.token) {
    headers["Authorization"] = "Bearer " + options.token;
  }
  return fetch(options.endpoint, {
    method: "POST",
    headers: headers,
    body: JSON.stringify(payload),
  })
    .then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
    })
    .catch(function (err) {
      return { ok: false, status: 0, body: { error: { message: String(err) } } };
    });
}

function setBadge(ok) {
  try {
    chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
    chrome.action.setBadgeBackgroundColor({ color: ok ? "#2e7d32" : "#c62828" });
    setTimeout(function () {
      chrome.action.setBadgeText({ text: "" });
    }, 4000);
  } catch (err) {
    // action API may be unavailable in some contexts (e.g. during extension update) --
    // the badge is a convenience indicator only, never load-bearing.
  }
}

chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== "FPT_CAPTURE") return false;

  getOptions()
    .then(function (options) {
      return postCapture(message.payload, options);
    })
    .then(function (result) {
      setBadge(result.ok);
      sendResponse(result);
    });

  return true; // keep the message channel open for the async sendResponse above
});
