/*
 * player-ui.js — Bouton lecture/pause personnalisé + masquage auto en
 * plein écran (#main-player)
 * =====================================================================
 * Module 100% additif, complémentaire à player-controls.js (volume) et
 * player-smooth.js (timeline). Trois améliorations :
 *
 * 1) BOUTON LECTURE/PAUSE PERSONNALISÉ (façon YouTube)
 *    Le lecteur natif n'a plus l'attribut "controls" (voir templates/
 *    video.html) : cliquer sur l'écran vidéo lui-même ne fait donc plus
 *    rien. Seul le bouton dédié (#player-playpause-btn), placé à côté de
 *    Précédent/Suivant/Aléatoire, met en pause ou relance la lecture. Son
 *    icône (▶ / ⏸) reste synchronisée avec l'état réel du lecteur, quelle
 *    que soit la cause du changement (bouton, lecture automatique,
 *    enchaînement en fin de vidéo, reprise après un plantage...).
 *
 * 2) MASQUAGE AUTOMATIQUE EN PLEIN ÉCRAN
 *    Hors plein écran, la timeline et les boutons ne s'affichent déjà que
 *    lorsque la souris survole l'écran de lecture (voir les règles CSS
 *    existantes sur .video-frame:hover). En plein écran, la souris se
 *    trouve quasiment toujours au-dessus de l'écran (qui occupe tout le
 *    moniteur) : on ajoute donc un comportement complémentaire — la
 *    timeline et les boutons restent affichés tant que la souris bouge
 *    (ou vient de bouger), puis se masquent avec le curseur dès qu'elle
 *    reste immobile un court instant, exactement comme sur YouTube. Ce
 *    masquage est suspendu tant que la souris est directement sur la
 *    barre de contrôles, ou pendant un glissement de la timeline, pour ne
 *    jamais faire disparaître un contrôle en cours d'utilisation.
 *
 * 3) FLÈCHES CLAVIER GAUCHE/DROITE = AVANCE/RECUL FIN DE LA TIMELINE
 *    La timeline (player-smooth.js) sait déjà réagir aux flèches, mais
 *    seulement quand elle a elle-même le focus clavier (accessibilité,
 *    rarement atteint en pratique). Ici, les flèches gauche/droite avancent
 *    ou reculent la lecture (petit saut de 5s, comme la timeline) depuis
 *    N'IMPORTE OÙ sur la page vidéo — y compris en plein écran, où il est
 *    impossible de "tabuler" jusqu'à la timeline. Neutralisé dès qu'un
 *    champ de saisie (texte, curseur de volume, etc.) a le focus, ou
 *    qu'une touche de raccourci navigateur (Alt/Ctrl/Cmd/Maj + flèche) est
 *    utilisée, pour ne jamais interférer avec autre chose.
 * ---------------------------------------------------------------------
 */
(function () {
  "use strict";

  var IDLE_MS = 2600;

  // =====================================================================
  // 1) Bouton lecture/pause personnalisé (+ clic sur l'image + barre Espace)
  // =====================================================================
  // Flag global : l'utilisateur a explicitement mis en pause. Les modules
  // de récupération / autoplay (player-smooth, initGuaranteedAutoplay) ne
  // doivent JAMAIS relancer la lecture tant que ce drapeau est actif.
  // Réinitialisé uniquement sur une action explicite de lecture (bouton,
  // clic vidéo, Espace) ou sur un changement de source (nouvelle vidéo).
  window.__userPausedPlayback = false;

  function initPlayPause(root, signal) {
    var player = root.querySelector("#main-player");
    var btn = root.querySelector("#player-playpause-btn");
    var frame = root.querySelector("#video-frame");
    if (!player) return;

    // Garantit l'absence des contrôles natifs (play + durée) qui
    // apparaissaient derrière les boutons de navigation personnalisés.
    try {
      player.controls = false;
      player.removeAttribute("controls");
    } catch (e) { /* ignore */ }

    var listenerOpts = signal ? { signal } : undefined;

    function refresh() {
      if (!btn) btn = document.getElementById("player-playpause-btn");
      if (!btn) return;
      var playing = !player.paused && !player.ended;
      btn.textContent = playing ? "⏸" : "▶";
      btn.setAttribute("aria-label", playing ? "Mettre en pause" : "Lecture");
      btn.title = playing ? "Pause" : "Lecture";
    }

    function doPause() {
      window.__userPausedPlayback = true;
      try { player.pause(); } catch (e) { /* ignore */ }
      // Certains pipelines (recovery / soft-nav) pouvaient relancer play()
      // juste après : on force un second pause au tick suivant.
      setTimeout(function () {
        if (window.__userPausedPlayback && player && !player.paused) {
          try { player.pause(); } catch (e2) { /* ignore */ }
        }
        refresh();
      }, 0);
      refresh();
    }

    function doPlay() {
      window.__userPausedPlayback = false;
      var p = player.play();
      if (p && p.catch) p.catch(function () { /* lecture refusée */ });
      refresh();
    }

    function togglePlayPause() {
      // Re-résout le nœud <video> au cas où il aurait été déplacé (persist).
      var live = document.getElementById("main-player") || player;
      if (live && live !== player) player = live;
      if (!player) return;
      if (player.paused || player.ended) {
        doPlay();
      } else {
        doPause();
      }
    }

    // Délégation + capture : le clic est intercepté même si un parent
    // (overlay CSS pointer-events, transition transform) interfère.
    function onBtnClick(e) {
      var target = e.target && e.target.closest
        ? e.target.closest("#player-playpause-btn, .player-playpause-btn")
        : null;
      if (!target) return;
      e.preventDefault();
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
      togglePlayPause();
    }
    document.addEventListener("click", onBtnClick, listenerOpts ? Object.assign({ capture: true }, listenerOpts) : { capture: true });

    // Clic direct sur l'image vidéo (hors contrôles) = play/pause, façon YouTube.
    if (frame) {
      frame.addEventListener("click", function (e) {
        if (e.target.closest && (
          e.target.closest("#player-overlay-bottom") ||
          e.target.closest("#player-playpause-btn") ||
          e.target.closest(".player-nav-buttons") ||
          e.target.closest(".smooth-seek-wrap") ||
          e.target.closest(".heavy-codec-gate") ||
          e.target.closest("button") ||
          e.target.closest("a")
        )) return;
        // Uniquement un clic simple sur le <video> ou le cadre vide.
        if (e.target !== player && e.target !== frame && !(e.target.tagName === "VIDEO")) return;
        e.preventDefault();
        togglePlayPause();
      }, listenerOpts);
    }

    // Barre d'espace = play/pause (sauf si focus dans un champ de saisie).
    document.addEventListener("keydown", function (e) {
      if (e.key !== " " && e.code !== "Space") return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      var el = document.activeElement;
      if (el) {
        var tag = el.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (el.isContentEditable) return;
      }
      // Uniquement sur la page lecteur (cadre vidéo présent).
      if (!document.getElementById("video-frame")) return;
      e.preventDefault();
      togglePlayPause();
    }, listenerOpts);

    player.addEventListener("play", function () {
      // Si une relance non voulue survient alors que l'utilisateur a mis
      // en pause, on re-coupe immédiatement.
      if (window.__userPausedPlayback) {
        try { player.pause(); } catch (e) { /* ignore */ }
        return;
      }
      refresh();
    }, listenerOpts);
    player.addEventListener("pause", refresh, listenerOpts);
    player.addEventListener("ended", function () {
      window.__userPausedPlayback = false;
      refresh();
    }, listenerOpts);
    // Nouvelle source = nouvelle vidéo : on oublie la pause utilisateur.
    player.addEventListener("loadstart", function () {
      window.__userPausedPlayback = false;
      refresh();
    }, listenerOpts);
    refresh();
  }

  // =====================================================================
  // 2) Masquage automatique en plein écran
  // =====================================================================
  function initFullscreenAutoHide(root, signal) {
    var frame = root.querySelector("#video-frame");
    var overlay = root.querySelector("#player-overlay-bottom");
    if (!frame) return;

    var listenerOpts = signal ? { signal } : undefined;
    var idleTimer = null;

    function isFullscreen() {
      return document.fullscreenElement === frame || document.webkitFullscreenElement === frame;
    }

    function isBusy() {
      if (overlay && overlay.matches(":hover")) return true;
      if (overlay) {
        // :focus-visible (outil moderne) ne réagit qu'au VRAI focus clavier
        // (Tab), jamais au focus laissé par un simple clic souris sur un
        // bouton (play/pause, 🔀, volume...) — sans quoi ce focus laissé
        // par le clic empêchait à tort tout masquage par la suite, même la
        // souris parfaitement immobile.
        try {
          if (overlay.querySelector(":focus-visible")) return true;
        } catch (e) {
          // Repli pour un navigateur très ancien sans support de
          // :focus-visible : ancien comportement (tout focus compte).
          if (document.activeElement && overlay.contains(document.activeElement)) return true;
        }
      }
      if (frame.querySelector(".smooth-seek-bar.dragging")) return true;
      return false;
    }

    function scheduleIdle() {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        if (!isFullscreen()) return;
        if (isBusy()) { scheduleIdle(); return; }
        frame.classList.add("controls-idle");
      }, IDLE_MS);
    }

    function wake() {
      if (!isFullscreen()) return;
      frame.classList.remove("controls-idle");
      scheduleIdle();
    }

    function onFullscreenChange() {
      if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
      if (isFullscreen()) {
        wake();
      } else {
        frame.classList.remove("controls-idle");
      }
    }

    document.addEventListener("mousemove", wake, listenerOpts);
    document.addEventListener("mousedown", wake, listenerOpts);
    document.addEventListener("touchstart", wake, listenerOpts);
    document.addEventListener("keydown", wake, listenerOpts);
    document.addEventListener("fullscreenchange", onFullscreenChange, listenerOpts);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange, listenerOpts);

    if (signal) {
      signal.addEventListener("abort", function () {
        if (idleTimer) clearTimeout(idleTimer);
        frame.classList.remove("controls-idle");
      }, { once: true });
    }
  }

  // =====================================================================
  // 3) Flèches clavier gauche/droite = avance/recul fin de la timeline
  // =====================================================================
  function initGlobalSeekKeys(root, signal) {
    var frame = root.querySelector("#video-frame");
    var player = root.querySelector("#main-player");
    if (!frame || !player) return;

    var STEP = 5; // secondes — même valeur que le saut clavier de la timeline (player-smooth.js)
    var listenerOpts = signal ? { signal } : undefined;

    function isEditableTarget(el) {
      if (!el) return false;
      var tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (el.isContentEditable) return true;
      return false;
    }

    document.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      // Jamais de raccourci navigateur/système intercepté (ex. Alt+Gauche = retour).
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      // Jamais d'interférence avec une saisie en cours (champ texte, curseur
      // de volume, etc. — tous des éléments <input>/<textarea>/<select>).
      if (isEditableTarget(document.activeElement)) return;
      // Déjà géré par la timeline elle-même quand elle a le focus direct
      // (player-smooth.js) : on évite ici un double saut (10s au lieu de 5s).
      if (e.target && e.target.closest && e.target.closest(".smooth-seek-bar")) return;

      var dur = player.duration;
      if (!dur || !isFinite(dur)) return;
      if (e.key === "ArrowRight") {
        player.currentTime = Math.min(dur, player.currentTime + STEP);
      } else {
        player.currentTime = Math.max(0, player.currentTime - STEP);
      }
      e.preventDefault();
    }, listenerOpts);
  }

  // =====================================================================
  // Point d'entrée, appelé depuis main.js (initDynamicPage).
  // =====================================================================
  window.__initPlayerUI = function (root, signal) {
    root = root || document;
    initPlayPause(root, signal);
    initFullscreenAutoHide(root, signal);
    initGlobalSeekKeys(root, signal);
  };
})();
