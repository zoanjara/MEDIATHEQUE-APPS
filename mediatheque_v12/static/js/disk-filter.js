/**
 * Disques actifs — intercepte submit (onchange="this.form.requestSubmit()").
 * Un seul envoi, debounced, sans geler l'UI.
 */
(function () {
  "use strict";

  var generation = 0;
  var debounceTimer = null;
  var inflight = null;
  var lastPayload = "";

  function currentVideoId() {
    var player = document.getElementById("main-player")
      || document.querySelector("#mini-player-video-slot video");
    if (!player) return null;
    var id = (player.dataset && player.dataset.videoId)
      || player.getAttribute("data-video-id");
    if (!id) return null;
    var n = parseInt(id, 10);
    return isNaN(n) ? null : n;
  }

  function setStatus(text) {
    var status = document.getElementById("disk-filter-status");
    if (status) status.textContent = text || "";
  }

  function collectDisabled(form) {
    var disabled = [];
    var boxes = form.querySelectorAll("input[type=checkbox][name=enabled_folder]");
    for (var i = 0; i < boxes.length; i++) {
      if (!boxes[i].checked) {
        var folder = boxes[i].getAttribute("data-folder")
          || (boxes[i].dataset && boxes[i].dataset.folder)
          || boxes[i].value;
        if (folder) disabled.push(folder);
      }
    }
    return disabled;
  }

  function sendAjax(form) {
    var disabled = collectDisabled(form);
    var payloadKey = disabled.slice().sort().join("\0");
    if (payloadKey === lastPayload) return; // identique au précédent : ignore
    lastPayload = payloadKey;

    var myGeneration = ++generation;
    setStatus("Application...");

    if (inflight && typeof inflight.abort === "function") {
      try { inflight.abort(); } catch (e) {}
    }

    var videoId = currentVideoId();
    var headers = {
      "Content-Type": "application/json",
      "Accept": "application/json",
    };
    if (window.__withCsrfHeaders) headers = window.__withCsrfHeaders(headers);

    var controller = typeof AbortController !== "undefined"
      ? new AbortController()
      : null;
    inflight = controller;

    var csrf = "";
    try {
      csrf = typeof window.__csrfToken === "function" ? window.__csrfToken() : "";
    } catch (e) {}

    fetch("/api/disk-filter", {
      method: "POST",
      body: JSON.stringify({
        disabled_folders: disabled,
        video_id: videoId,
        csrf_token: csrf || undefined,
      }),
      cache: "no-store",
      headers: headers,
      signal: controller ? controller.signal : undefined,
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (res) {
        if (myGeneration !== generation) return;
        if (!res || !res.ok) throw new Error((res && res.error) || "Erreur");
        if (res.allowed === false) {
          if (window.__stopDisallowedPlayback) window.__stopDisallowedPlayback();
          else if (window.__checkCurrentPlaybackAllowed) window.__checkCurrentPlaybackAllowed();
        }
        setStatus("✅ Mis à jour");
        setTimeout(function () {
          if (myGeneration === generation) setStatus("");
        }, 2500);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (myGeneration !== generation) return;
        lastPayload = ""; // permet un nouvel essai
        setStatus("❌ Échec — réessaie");
      });
  }

  function schedule(form) {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () { sendAjax(form); }, 120);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.id !== "disk-filter-form") return;
    e.preventDefault();
    e.stopPropagation();
    schedule(form);
  }, true);

  window.__diskFilterChanged = function () {
    var form = document.getElementById("disk-filter-form");
    if (form) schedule(form);
  };
  window.__diskFilterSend = window.__diskFilterChanged;
})();
