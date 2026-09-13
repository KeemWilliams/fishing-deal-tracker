"use strict";

var DEFAULT_ENDPOINT = "http://localhost:8787/v1/captures";

function load() {
  chrome.storage.sync.get({ endpoint: DEFAULT_ENDPOINT, token: "" }, function (items) {
    document.getElementById("endpoint").value = items.endpoint;
    document.getElementById("token").value = items.token;
  });
}

function save() {
  var endpoint = document.getElementById("endpoint").value.trim() || DEFAULT_ENDPOINT;
  var token = document.getElementById("token").value.trim();

  // Basic client-side sanity check only -- the real validation happens server-side
  // (fpt/capture/validate.py); this just avoids saving an obviously-broken value.
  try {
    var parsed = new URL(endpoint);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("endpoint must be http(s)");
    }
  } catch (err) {
    document.getElementById("status").textContent = "Invalid endpoint URL: " + err.message;
    document.getElementById("status").style.color = "#c62828";
    return;
  }

  chrome.storage.sync.set({ endpoint: endpoint, token: token }, function () {
    var status = document.getElementById("status");
    status.style.color = "#2e7d32";
    status.textContent = "Saved.";
    setTimeout(function () {
      status.textContent = "";
    }, 2000);
  });
}

document.addEventListener("DOMContentLoaded", function () {
  load();
  var saveButton = document.getElementById("save");
  if (saveButton) {
    saveButton.addEventListener("click", save);
  }
});
