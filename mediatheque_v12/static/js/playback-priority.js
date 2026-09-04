/*
 * PRIORITÉ À LA LECTURE VIDÉO
 * ---------------------------
 * Heartbeat périodique vers le serveur pour suspendre le scan pendant
 * la lecture. Version robuste soft-nav :
 *  - écoute via délégation sur document (le <video> est réutilisé)
 *  - regroupement des POST (emptied+pause+play rapides)
 *  - ignore emptied pendant un soft-nav (évite rafale inactive/active)
 */
(function () {
  "use strict";

  var HEARTBEAT_MS = 8000;
  var timer = null;
  var lastState = null;
  var pendingTimer = null;
  var pendingActive = null;

  function currentPlayer() {
    return document.getElementById("main-player");
  }

  // CORRECTIF "Disques actifs" : arrête réellement une lecture en cours
  // (plein écran ou réduite en mini-lecteur flottant) dès que le serveur
  // signale, via la réponse du battement, que le disque de cette vidéo
  // vient d'être décoché. Sans ça, une vidéo déjà chargée dans le
  // navigateur continuait de jouer indéfiniment malgré la case décochée
  // (les gardes serveur ne protègent qu'un accès qui n'a pas encore
  // commencé — voir /stream/<id>, /video/<id>/transcode/*).
  function stopDisallowedPlayback() {
    // Ferme TOUJOURS mini-lecteur + lecteur page si le disque est décoché.
    try {
      if (window.__destroyPersistedPlayer) window.__destroyPersistedPlayer();
    } catch (e0) {}
    try {
      var nodes = document.querySelectorAll("#main-player, #mini-player-video-slot video");
      for (var i = 0; i < nodes.length; i++) {
        var p = nodes[i];
        try { p.pause(); } catch (e1) {}
        try {
          while (p.firstChild) p.removeChild(p.firstChild);
          p.removeAttribute("src");
          p.load();
        } catch (e2) {}
        try {
          if (p.closest && p.closest("#mini-player-video-slot")) p.remove();
        } catch (e3) {}
      }
    } catch (e4) {}
    try {
      var host = document.getElementById("mini-player-host");
      if (host) {
        host.hidden = true;
        var slot = document.getElementById("mini-player-video-slot");
        if (slot) {
          while (slot.firstChild) slot.removeChild(slot.firstChild);
        }
      }
    } catch (e5) {}
    if (window.showToast) {
      window.showToast("⏹ Lecture arrêtée : ce disque vient d'être désactivé dans Réglages.", true);
    }
    stop();
  }
  window.__stopDisallowedPlayback = stopDisallowedPlayback;

  function send(active) {
    pendingActive = !!active;
    if (pendingTimer) return;
    pendingTimer = setTimeout(function () {
      pendingTimer = null;
      var a = pendingActive;
      if (lastState === a) return;
      lastState = a;
      var p = currentPlayer();
      var videoId = p ? p.dataset.videoId : null;
      try {
        fetch("/api/playback/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active: a, video_id: videoId || null }),
          keepalive: true,
          cache: "no-store",
        })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (res) {
            if (res && res.allowed === false) stopDisallowedPlayback();
          })
          .catch(function () {});
      } catch (e) {}
    }, 200);
  }

  function start() {
    if (window.__softNavBusy) return;
    send(true);
    if (timer) return;
    timer = setInterval(function () {
      if (!window.__softNavBusy) send(true);
    }, HEARTBEAT_MS);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    send(false);
  }

  // CORRECTIF "Disques actifs" : appelée depuis templates/settings.html
  // juste après un changement de case à cocher, pour vérifier tout de
  // suite (sans attendre jusqu'à 8s) si la vidéo actuellement en lecture
  // (plein écran ou mini-lecteur) vient d'être coupée par ce changement —
  // utile en particulier quand le mini-lecteur tourne pendant qu'on est
  // sur la page Réglages elle-même (cas le plus fréquent pour ce réglage).
  function checkNow() {
    var p = currentPlayer()
      || document.querySelector("#mini-player-video-slot video");
    if (!p) return;
    var videoId = (p.dataset && p.dataset.videoId) || p.getAttribute("data-video-id");
    if (!videoId) return;
    // Vérifie même en pause : le mini peut être "paused" un instant pendant
    // un soft-nav alors qu'il doit quand même être fermé si disque off.
    fetch("/api/playback/heartbeat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: !p.paused && !p.ended, video_id: videoId }),
      cache: "no-store",
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (res) {
        if (res && res.allowed === false) stopDisallowedPlayback();
      })
      .catch(function () {});
  }
  window.__checkCurrentPlaybackAllowed = checkNow;

  // Délégation : survit au soft-nav (même nœud <video> ou remplacé)
  document.addEventListener("playing", function (e) {
    if (e.target && e.target.id === "main-player") start();
  }, true);
  document.addEventListener("play", function (e) {
    if (e.target && e.target.id === "main-player") start();
  }, true);
  document.addEventListener("pause", function (e) {
    if (e.target && e.target.id === "main-player") {
      if (window.__softNavBusy) return;
      stop();
    }
  }, true);
  document.addEventListener("ended", function (e) {
    if (e.target && e.target.id === "main-player") stop();
  }, true);
  document.addEventListener("emptied", function (e) {
    if (e.target && e.target.id === "main-player") {
      // Pendant soft-nav on vide volontairement la source : ne pas spammer
      if (window.__softNavBusy) return;
      stop();
    }
  }, true);

  window.addEventListener("pagehide", stop);
  window.addEventListener("beforeunload", stop);
  document.addEventListener("visibilitychange", function () {
    var p = currentPlayer();
    if (document.visibilityState === "hidden" && p && p.paused) stop();
  });

  var p = currentPlayer();
  if (p && !p.paused && !p.ended) start();
})();
