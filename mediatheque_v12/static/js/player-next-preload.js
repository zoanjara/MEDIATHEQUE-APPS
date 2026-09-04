/*
 * Préchargement discret de la vidéo suivante / précédente
 * ---------------------------------------------------------
 * Règle d'or : ne JAMAIS concurrencer la lecture en cours.
 *
 * Correctifs majeurs (anti-rafale de requêtes) :
 *  - Un seul cycle d'init actif à la fois (timers + listeners nettoyés
 *    à chaque soft-nav / abort-preload).
 *  - JAMAIS de Speculation Rules "prerender" (exécutait toute la page
 *    vidéo en arrière-plan → tempête GET /video/N + warm + heartbeat).
 *  - Pas de prefetch multi-pages sur les cartes "À découvrir".
 *  - Warm serveur uniquement : survol explicite du bouton ⏭, ou tout
 *    près de la fin de la vidéo en cours (urgence).
 */
(function () {
  "use strict";

  var active = null; // état du cycle d'init courant

  function videoIdFromUrl(url) {
    var m = /\/video\/(\d+)/.exec(url || "");
    return m ? m[1] : null;
  }

  function removeOldSpeculationScripts() {
    try {
      var nodes = document.querySelectorAll('script[type="speculationrules"][data-mediatheque-preload]');
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].parentNode.removeChild(nodes[i]);
      }
    } catch (e) { /* ignore */ }
  }

  function teardown() {
    if (!active) return;
    var s = active;
    active = null;
    if (s.poll) { try { clearInterval(s.poll); } catch (e) {} s.poll = null; }
    if (s.startTimer) { try { clearTimeout(s.startTimer); } catch (e) {} s.startTimer = null; }
    if (s.controller) {
      try { s.controller.abort(); } catch (e) {}
      s.controller = null;
    }
    if (s.player && s.onPlayerEv) {
      ["waiting", "stalled", "seeking"].forEach(function (ev) {
        try { s.player.removeEventListener(ev, s.onPlayerEv); } catch (e) {}
      });
    }
    removeOldSpeculationScripts();
  }

  function warmStream(videoId) {
    if (!videoId) return;
    try {
      fetch("/stream/" + videoId + "/warm?t=0&span=" + 2 * 1024 * 1024, {
        cache: "no-store",
        credentials: "same-origin",
      }).catch(function () {});
    } catch (e) { /* ignore */ }
  }

  function prefetchDocument(url) {
    if (!url) return;
    try {
      // Évite les doublons
      var existing = document.querySelector('link[rel="prefetch"][href="' + url.replace(/"/g, "") + '"]');
      if (existing) return;
      var link = document.createElement("link");
      link.rel = "prefetch";
      link.as = "document";
      link.href = url;
      document.head.appendChild(link);
    } catch (e) { /* ignore */ }
  }

  function installConservativePrefetch(nextUrl) {
    if (!nextUrl) return;
    if (!(HTMLScriptElement.supports && HTMLScriptElement.supports("speculationrules"))) {
      return;
    }
    removeOldSpeculationScripts();
    try {
      var el = document.createElement("script");
      el.type = "speculationrules";
      el.setAttribute("data-mediatheque-preload", "1");
      // Prefetch document uniquement — JAMAIS prerender
      el.textContent = JSON.stringify({
        prefetch: [{ source: "list", urls: [nextUrl], eagerness: "conservative" }],
      });
      document.head.appendChild(el);
    } catch (e) { /* ignore */ }
  }

  function init() {
    var nav = document.getElementById("player-nav-buttons");
    if (!nav) return;
    // Si déjà prêt pour CE bloc nav, ne rien refaire
    if (nav.dataset.preloadReady === "1") return;
    // Nouveau bloc nav (soft-nav) : nettoyer l'ancien cycle
    teardown();
    nav.dataset.preloadReady = "1";

    var player = document.getElementById("main-player");
    var nextUrl = nav.dataset.nextUrl || "";
    var prevUrl = nav.dataset.prevUrl || "";
    if (!nextUrl && !prevUrl) return;

    var saveData = !!(navigator.connection && navigator.connection.saveData);
    if (saveData) return;

    var state = {
      poll: null,
      startTimer: null,
      controller: null,
      player: player,
      onPlayerEv: null,
      done: false,
      navigating: false,
      warmed: false,
    };
    active = state;

    function abortPreload() {
      if (state.controller) {
        try { state.controller.abort(); } catch (e) {}
        state.controller = null;
      }
    }

    function bufferedAhead() {
      if (!player) return Infinity;
      try {
        var b = player.buffered, t = player.currentTime || 0;
        for (var i = 0; i < b.length; i++) {
          if (b.start(i) <= t && t <= b.end(i)) return b.end(i) - t;
        }
      } catch (e) { /* ignore */ }
      return 0;
    }

    function playbackHealthy() {
      if (!player) return true;
      if (player.seeking) return false;
      if (player.readyState < 3) return false;
      return bufferedAhead() >= 20;
    }

    function nearEnd() {
      if (!player || !isFinite(player.duration) || !player.duration) return false;
      return (player.duration - player.currentTime) <= 30;
    }

    function warmNextIfNeeded() {
      if (state.done || state.navigating || state.warmed) return;
      var id = videoIdFromUrl(nextUrl);
      if (!id) return;
      state.warmed = true;
      state.done = true;
      warmStream(id);
    }

    function tick() {
      if (!active || active !== state) {
        if (state.poll) { clearInterval(state.poll); state.poll = null; }
        return;
      }
      if (state.done || state.navigating) {
        if (state.poll) { clearInterval(state.poll); state.poll = null; }
        return;
      }
      // Uniquement en fin de vidéo + lecture confortable
      if (!nearEnd() || !playbackHealthy()) return;
      if (nextUrl) prefetchDocument(nextUrl);
      warmNextIfNeeded();
      if (state.poll) { clearInterval(state.poll); state.poll = null; }
    }

    // Prefetch léger de la page suivante seulement (pas prev, pas related)
    installConservativePrefetch(nextUrl);

    // Démarre le sondage tardif (ne pas concurrencer le démarrage)
    state.startTimer = setTimeout(function () {
      state.startTimer = null;
      if (!active || active !== state || state.navigating) return;
      state.poll = setInterval(tick, 2000);
      tick();
    }, 8000);

    // Survol du bouton suivant = intention claire → un seul warm
    function onNavIntent(e) {
      var a = e.target && e.target.closest ? e.target.closest("a") : null;
      if (!a || !nextUrl) return;
      var href = a.getAttribute("href") || "";
      if (href !== nextUrl && a.href !== nextUrl) return;
      if (nextUrl) prefetchDocument(nextUrl);
      warmNextIfNeeded();
    }
    ["pointerenter", "pointerdown"].forEach(function (ev) {
      nav.addEventListener(ev, onNavIntent, true);
    });

    state.onPlayerEv = function () {
      abortPreload();
    };
    if (player) {
      ["waiting", "stalled", "seeking"].forEach(function (ev) {
        player.addEventListener(ev, state.onPlayerEv);
      });
    }

    nav.addEventListener("click", function (e) {
      if (e.target && e.target.closest && e.target.closest("a")) {
        state.navigating = true;
        if (state.poll) { clearInterval(state.poll); state.poll = null; }
        if (state.startTimer) { clearTimeout(state.startTimer); state.startTimer = null; }
        abortPreload();
      }
    }, true);
  }

  // ---- Related cards : warm UNIQUEMENT au survol d'UNE carte ----
  var relatedWarmed = Object.create(null);

  function initRelatedPreload() {
    var related = document.querySelector(".player-related");
    if (!related || related.dataset.relatedPreloadReady === "1") return;
    related.dataset.relatedPreloadReady = "1";

    var saveData = !!(navigator.connection && navigator.connection.saveData);
    if (saveData) return;

    related.addEventListener("pointerenter", function (e) {
      var a = e.target && e.target.closest ? e.target.closest("a.related-card") : null;
      if (!a || !a.href) return;
      var id = videoIdFromUrl(a.href);
      if (!id || relatedWarmed[id]) return;
      relatedWarmed[id] = true;
      warmStream(id);
      // NE PAS ajouter ici un prefetchDocument(a.href) (déjà essayé) :
      // softNavigateToVideo() → fetchPage() (static/js/player-persist.js)
      // appelle fetch() avec "cache: no-store", donc un tel préchargement
      // de page HTML est de toute façon ignoré au clic (aucun gain) — il
      // ne fait qu'ajouter des <link rel="prefetch"> qui s'accumulent
      // dans <head> (rien ne les retire) et concurrencent en réseau la
      // VRAIE requête de navigation dès qu'on survole plusieurs cases
      // avant de cliquer → délai/gel imprévisible au clic. Seul le
      // préchauffage léger du flux (warmStream, petite plage d'octets)
      // est donc fait ici, comme à l'origine.
    }, true);
  }

  function initAll() {
    init();
    initRelatedPreload();
  }

  initAll();
  document.addEventListener("mediatheque:dynamic-page-init", initAll);
  window.addEventListener("pagehide", teardown);
  document.addEventListener("mediatheque:abort-preload", function () {
    teardown();
    relatedWarmed = Object.create(null);
  });
})();
