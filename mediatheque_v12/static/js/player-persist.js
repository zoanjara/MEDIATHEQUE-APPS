/*
 * player-persist.js — Navigation instantanée + mini-lecteur flottant
 * =====================================================================
 * Améliore la navigation de l'application (panneau latéral, recherche,
 * changement de vidéo) : quand une lecture vidéo est en cours, ces
 * actions ne coupent plus jamais la lecture.
 *
 *   - Clic sur "🎬 Médiathèque", "Toutes les vidéos", "⚙ Paramètres",
 *     n'importe quel lien du panneau latéral gauche, ou une recherche :
 *     la page change instantanément (sans rechargement complet), et la
 *     lecture en cours se réduit dans une petite fenêtre flottante en
 *     bas à droite (façon YouTube), avec une croix pour la fermer.
 *
 *   - Clic sur une autre vidéo (carte de grille, vidéo "à découvrir
 *     aussi", bouton précédent/suivant...) pendant qu'une lecture est en
 *     cours : la vidéo cliquée démarre immédiatement, sans rechargement
 *     de page ni interruption perceptible.
 *
 * Fonctionnement : ce module ne modifie AUCUNE route ni template côté
 * serveur. Il récupère les mêmes pages HTML que d'habitude via fetch()
 * et remplace le contenu de .layout (panneau + contenu) à la place d'une
 * vraie navigation, tout en déplaçant le VRAI nœud <video> (jamais
 * détruit ni recréé) plutôt que de le laisser se faire retirer du DOM —
 * c'est ce déplacement synchrone qui garantit qu'il continue de jouer
 * sans interruption, exactement comme le fait YouTube.
 *
 * Si quoi que ce soit d'inattendu se produit (page introuvable, réseau
 * coupé, structure de page non reconnue...), on retombe systématiquement
 * sur une vraie navigation classique (window.location.href) : aucune
 * action de l'utilisateur ne peut donc rester bloquée ou "sans erreur"
 * silencieuse.
 */
(function () {
  "use strict";

  var layout = document.querySelector(".layout");
  var miniHost = document.getElementById("mini-player-host");
  var miniTitle = document.getElementById("mini-player-title");
  var miniSlot = document.getElementById("mini-player-video-slot");
  var miniCloseBtn = document.getElementById("mini-player-close");
  var miniExpandBtn = document.getElementById("mini-player-expand");

  if (!layout || !miniHost || !miniSlot) return; // structure inattendue : on n'active rien, comportement d'origine intact

  var activeVideoId = null; // id de la vidéo actuellement chargée dans le lecteur persistant QUAND il est réduit en mini-lecteur, ou null
  var navToken = 0; // permet d'ignorer une réponse fetch devenue obsolète (navigations rapides successives)

  function currentPlayer() {
    return document.getElementById("main-player");
  }

  function isVideoPath(pathname) {
    return /^\/video\/\d+(\/|$)/.test(pathname);
  }

  function extractVideoId(pathname) {
    var m = /^\/video\/(\d+)/.exec(pathname);
    return m ? m[1] : null;
  }

  // Id de la vidéo actuellement chargée dans le lecteur persistant, QUE ce
  // dernier soit affiché en plein (page vidéo) ou réduit en mini-lecteur.
  // Contrairement à "activeVideoId" (qui ne vit que pendant la réduction en
  // mini-lecteur, et vaut null dès que le lecteur revient en plein écran),
  // celle-ci reste toujours à jour : c'est elle qui permet de savoir si une
  // navigation vers une page vidéo pointe vers LA MÊME vidéo déjà en cours
  // de lecture (auquel cas on ne doit surtout pas recharger sa source, sous
  // peine de repartir du tout début), y compris quand le lecteur est déjà
  // affiché en plein écran (ex. rafraîchissement en temps réel de la page
  // après l'enregistrement de ses propres métadonnées — voir live-sync.js).
  var currentPersistedVideoId = isVideoPath(location.pathname) ? extractVideoId(location.pathname) : null;

  // ---------------------------------------------------------------------
  // Récupération d'une page via fetch, avec repli sûr sur une vraie
  // navigation si quoi que ce soit se passe mal.
  // ---------------------------------------------------------------------
  // Une seule navigation douce à la fois : annule le fetch HTML précédent.
  var pageAbort = null;
  function fetchPage(url) {
    if (pageAbort) {
      try { pageAbort.abort(); } catch (e) {}
    }
    pageAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var opts = {
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
      cache: "no-store",
    };
    if (pageAbort) opts.signal = pageAbort.signal;
    return fetch(url, opts)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var newLayout = doc.querySelector(".layout");
        if (!newLayout) throw new Error("structure de page inattendue");
        return { doc: doc, newLayout: newLayout };
      });
  }

  function swapInLayout(newLayout, doc, url, pushState, skipInit, preserveSidebar) {
    layout.innerHTML = "";
    while (newLayout.firstChild) {
      layout.appendChild(newLayout.firstChild);
    }
    if (doc.title) document.title = doc.title;
    if (doc.body) {
      // On resynchronise les classes "serveur" du body (ex. "video-page").
      // "scan-in-progress" (sondage /scan/status dans main.js) est toujours
      // préservée : sinon chaque navigation douce pendant un scan réaffiche
      // par erreur "✎ Modifier les métadonnées" sur les cartes jusqu'au
      // prochain sondage.
      // "sidebar-open" n'est préservée QUE si preserveSidebar est vrai
      // (rafraîchissement automatique en arrière-plan de la MÊME page, voir
      // live-sync.js) : dans ce cas précis, on ne doit pas refermer le
      // panneau tout seul pendant qu'on y modifie quelque chose (renommer
      // une catégorie, une playlist...). Dans tous les autres cas — un vrai
      // clic sur un lien du panneau, ou le retour au plein écran depuis le
      // mini-lecteur — le panneau doit se refermer, exactement comme le
      // ferait n'importe quel menu qu'on vient d'utiliser pour naviguer :
      // sans cette fermeture, il reste affiché par-dessus la page suivante
      // et bloque tous les clics dessous (c'était le bug du ☰/"Toutes les
      // vidéos" qui semblait "planter" l'appli pendant la lecture d'une
      // playlist).
      var sidebarOpen = preserveSidebar ? document.body.classList.contains("sidebar-open") : false;
      var scanInProgress = document.body.classList.contains("scan-in-progress");
      document.body.className = doc.body.className;
      document.body.classList.toggle("sidebar-open", sidebarOpen);
      document.body.classList.toggle("scan-in-progress", scanInProgress);
    }
    if (pushState !== false) {
      history.pushState({ softNav: true }, "", url);
    }
    if (!skipInit && window.__initDynamicPage) window.__initDynamicPage(layout);
  }

  // ---------------------------------------------------------------------
  // Mini-lecteur flottant
  // ---------------------------------------------------------------------
  function showMiniPlayer(player, videoId, title) {
    activeVideoId = videoId;
    currentPersistedVideoId = videoId;
    if (miniTitle) miniTitle.textContent = title || "";
    // Retire les hauteurs/styles imposés par initPlayerFineFit (plein écran
    // page) : sinon le <video> garde height:560px en inline et casse le
    // mini-lecteur (overflow, gel, ou arrêt imprévisible sur codecs lourds).
    try {
      player.style.height = "";
      player.style.maxHeight = "";
      player.style.minHeight = "";
      player.style.width = "";
    } catch (eStyle) { /* ignore */ }
    player.classList.add("mini-player-video");
    // Flag pour player-smooth : en mini, ne pas lancer de recovery native
    // agressive (load()) sur codec lourd — préférer mode compatible.
    try { player.dataset.miniPlayer = "1"; } catch (eD) { /* ignore */ }
    miniSlot.appendChild(player); // déplacement synchrone : la lecture continue
    miniHost.hidden = false;
    // Relance douce si le navigateur a mis en pause lors du déplacement DOM
    // (certains cas codecs lourds / soft-nav). Ne force pas si pause user.
    if (!window.__userPausedPlayback && player.paused && !player.ended) {
      var p = player.play();
      if (p && typeof p.catch === "function") p.catch(function () { /* ignore */ });
    }
  }

  function hideMiniPlayerHost() {
    miniHost.hidden = true;
    if (miniTitle) miniTitle.textContent = "";
  }

  function closeMiniPlayer() {
    var v = miniSlot.querySelector("video");
    if (v) {
      try { v.pause(); } catch (e) { /* ignore */ }
      v.removeAttribute("src");
      v.querySelectorAll("source").forEach(function (s) { s.remove(); });
      try { v.load(); } catch (e) { /* ignore */ }
      v.remove();
    }
    hideMiniPlayerHost();
    activeVideoId = null;
    currentPersistedVideoId = null;
  }

  // ---------------------------------------------------------------------
  // Détruit complètement le lecteur persistant en place (plein écran ou
  // mini), sans passer par la case mini-lecteur : utilisé quand la vidéo
  // qu'il contient vient d'être supprimée du disque (bouton "🗑 Supprimer
  // le fichier" de templates/video.html, voir static/js/main.js) et qu'il
  // n'y a pas de vidéo suivante vers laquelle enchaîner. Sans cet appel,
  // la navigation "douce" suivante (softNavigate vers "/") relocaliserait
  // par réflexe ce lecteur — pourtant toujours présent dans le DOM — dans
  // le mini-lecteur flottant, qui se retrouverait alors à proposer la
  // lecture d'un fichier qui n'existe plus.
  // ---------------------------------------------------------------------
  function destroyPersistedPlayer() {
    var v = currentPlayer();
    if (!v) return;
    try { v.pause(); } catch (e) { /* ignore */ }
    v.removeAttribute("src");
    v.querySelectorAll("source").forEach(function (s) { s.remove(); });
    try { v.load(); } catch (e) { /* ignore */ }
    v.remove();
    hideMiniPlayerHost();
    activeVideoId = null;
    currentPersistedVideoId = null;
  }
  window.__destroyPersistedPlayer = destroyPersistedPlayer;

  function relocateCurrentVideoToMini() {
    var player = currentPlayer();
    if (!player || miniSlot.contains(player)) return; // rien à jouer, ou déjà dans le mini-lecteur
    var navButtons = layout.querySelector("#player-nav-buttons");
    var videoId = navButtons ? navButtons.dataset.videoId : activeVideoId;
    var titleEl = layout.querySelector("#video-title-display");
    var title = titleEl ? titleEl.textContent : "";
    showMiniPlayer(player, videoId, title);
  }

  // ---------------------------------------------------------------------
  // Navigation douce vers une page NON-vidéo (grille, catégorie,
  // playlist, paramètres, organiser, recherche...). Si une lecture est
  // en cours, elle est réduite dans le mini-lecteur avant le
  // remplacement du contenu, et ne s'arrête jamais.
  // ---------------------------------------------------------------------
  function softNavigate(url, opts) {
    opts = opts || {};
    var token = ++navToken;
    var player = currentPlayer();

    return fetchPage(url)
      .then(function (result) {
        if (token !== navToken) return; // une navigation plus récente a pris le dessus
        if (result.newLayout.querySelector(".player-layout")) {
          // La cible est en fait une page vidéo : on délègue à la
          // fonction dédiée, qui sait réutiliser le lecteur en place.
          return softNavigateToVideo(url, opts);
        }
        if (player) relocateCurrentVideoToMini();
        swapInLayout(result.newLayout, result.doc, url, opts.pushState, false, opts.preserveSidebar);
      })
      .catch(function () {
        if (token !== navToken) return;
        window.location.href = url; // repli sûr : navigation classique
      });
  }

  // ---------------------------------------------------------------------
  // Navigation douce vers une page VIDÉO. Si un lecteur est déjà présent
  // (en plein écran de page ou déjà réduit en mini-lecteur), on réutilise
  // le MÊME élément <video> : on ne fait que changer sa source, ce qui
  // permet un enchaînement immédiat, sans double-chargement ni coupure.
  // ---------------------------------------------------------------------
  var softNavInFlightUrl = null;
  function softNavigateToVideo(url, opts) {
    opts = opts || {};
    // Normalise l'URL pour comparer (évite double soft-nav identique)
    var abs;
    try { abs = new URL(url, location.href); } catch (e) { abs = null; }
    var destPath = abs ? (abs.pathname + abs.search) : url;
    // Déjà en train d'aller vers cette même destination : ignore
    if (softNavInFlightUrl === destPath) return Promise.resolve();
    softNavInFlightUrl = destPath;
    window.__softNavBusy = true;

    var token = ++navToken;
    var persisted = currentPlayer();
    var destId = extractVideoId(abs ? abs.pathname : url);
    var isSameVideo = !!(persisted && currentPersistedVideoId != null && destId != null && String(destId) === String(currentPersistedVideoId));

    // Annule immédiatement les warms / prefetch spéculatifs (related, next)
    // pour libérer bande passante et I/O au profit de la vidéo demandée.
    try {
      document.dispatchEvent(new CustomEvent("mediatheque:abort-preload"));
    } catch (eAbort) { /* ignore */ }

    // ARRÊT IMMÉDIAT (avant le fetch HTML) si on CHANGE de vidéo :
    // indispensable pour codecs lourds / 4K — sinon le décodage continue
    // pendant le téléchargement de la page suivante → CPU saturé, soft-nav
    // figé. Ne PAS faire ça si on reste sur la même vidéo (mini-lecteur).
    if (!isSameVideo) {
      try {
        if (window.__abortPlayerListeners) window.__abortPlayerListeners();
      } catch (eA0) {}
      if (window.__guaranteeTimers && window.__guaranteeTimers.length) {
        for (var _gi = 0; _gi < window.__guaranteeTimers.length; _gi++) {
          clearTimeout(window.__guaranteeTimers[_gi]);
        }
        window.__guaranteeTimers = [];
      }
      if (persisted) {
        try { persisted.pause(); } catch (eP0) {}
        try {
          while (persisted.firstChild) persisted.removeChild(persisted.firstChild);
          persisted.removeAttribute("src");
        } catch (eP1) {}
        try { delete persisted.dataset.autoCompat; } catch (eP2) {}
        try { delete persisted.dataset.resumeReady; } catch (eP3) {}
        try { delete persisted.dataset.suppressRecovery; } catch (eP4) {}
        try { delete persisted.__pauseBackgroundWarmUntil; } catch (eP5) {}
      }
      // Heartbeat inactif : au plus une fois par vague de soft-nav
      // (évite la rafale de POST vue dans les logs).
      if (!window.__hbNavCooldown) {
        window.__hbNavCooldown = true;
        setTimeout(function () { window.__hbNavCooldown = false; }, 1500);
        try {
          fetch("/api/playback/heartbeat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ active: false }),
            keepalive: true,
            cache: "no-store"
          }).catch(function () {});
        } catch (eHb) {}
      }
    }

    // Si la vidéo demandée est déjà celle en cours de lecture dans le
    // lecteur persistant (typiquement : clic sur "revenir à la vidéo"
    // depuis le mini-lecteur, ou clic sur le lien vers la vidéo qu'on est
    // déjà en train de regarder), on ne doit surtout pas la recharger — un
    // rechargement (changement de source + .load()) repartirait du tout
    // début, même si la lecture était déjà bien avancée. On se contente
    // alors de rapatrier la page (contenu, playlists suggérées...) et de
    // replacer le lecteur en place, en laissant la lecture se poursuivre
    // exactement là où elle en était.

    // Feedback immédiat sur le cadre lecteur (évite l'impression de "rien
    // ne se passe" pendant le fetch + démarrage du flux, surtout depuis
    // les cartes de la colonne de droite). Le cadre est remplacé au swap :
    // on re-pose la classe sur le NOUVEAU #video-frame après insertion.
    var frame = document.getElementById("video-frame");
    if (frame && !isSameVideo) frame.classList.add("is-switching");
    function clearSwitching() {
      var f = document.getElementById("video-frame");
      if (f) f.classList.remove("is-switching");
    }
    function markSwitching() {
      if (isSameVideo) return;
      var f = document.getElementById("video-frame");
      if (f) f.classList.add("is-switching");
    }

    return fetchPage(url)
      .then(function (result) {
        if (token !== navToken) {
          clearSwitching();
          return; // une nav plus récente a pris le dessus
        }

        // Important : on ne déplace JAMAIS le <video> en cours de lecture
        // dans l'arbre encore détaché renvoyé par fetch()/DOMParser (un
        // document distinct du document réellement affiché) — un tel
        // déplacement entre deux documents différents peut faire perdre
        // la lecture en cours selon les navigateurs. À la place, on
        // laisse un simple marqueur à la place du lecteur "frais" de la
        // page récupérée, puis, une fois le contenu inséré dans le VRAI
        // document (via swapInLayout), on échange ce marqueur contre le
        // lecteur persistant : un déplacement au sein d'un même document,
        // toujours sûr et sans interruption de lecture.
        var freshPlayer = result.newLayout.querySelector("#main-player");
        var pendingSrc = "";
        if (persisted && freshPlayer) {
          if (!isSameVideo) {
            // On coupe TOUT ce qui restait accroché au lecteur pour la
            // vidéo précédente avant de toucher à sa source : sinon un
            // mécanisme de secours encore actif (sondage de conversion,
            // nouvelle tentative de lecture différée...) pouvait réécrire
            // la source ou relancer un chargement juste après, et laisser
            // la vidéo suivante définitivement figée.
            if (window.__abortPlayerListeners) window.__abortPlayerListeners();
            // Reset matériel du pipeline média (évite décodeurs/résidus CPU
            // de la vidéo précédente, y compris après une session VLC).
            try { persisted.pause(); } catch (eR0) {}
            try {
              while (persisted.firstChild) persisted.removeChild(persisted.firstChild);
              persisted.removeAttribute("src");
              persisted.load();
            } catch (eR1) {}
            if (window.__guaranteeTimers && window.__guaranteeTimers.length) {
              for (var gix = 0; gix < window.__guaranteeTimers.length; gix++) {
                clearTimeout(window.__guaranteeTimers[gix]);
              }
              window.__guaranteeTimers = [];
            }
            var newSource = freshPlayer.querySelector("source");
            // Certaines pages peuvent porter l'URL directement sur le
            // <video> (attribut src) plutôt que sur un <source> enfant :
            // sans ce repli, la source de l'ANCIENNE vidéo restait en
            // place et le lecteur semblait "bloqué" sur place.
            pendingSrc = (newSource && newSource.src) || freshPlayer.getAttribute("src") || freshPlayer.src || "";
            // Codec lourd sans version compatible : AUCUNE source ne doit
            // être chargée ni lue (sinon décodage logiciel → CPU 100 %,
            // même si l'utilisateur ouvre ensuite dans VLC).
            var isHeavyGate = (freshPlayer.getAttribute("data-heavy-gate") === "1")
              || (freshPlayer.dataset && freshPlayer.dataset.heavyGate === "1");
            if (isHeavyGate) {
              pendingSrc = "";
            }
            var poster = freshPlayer.getAttribute("poster");
            if (poster) persisted.setAttribute("poster", poster);
            else persisted.removeAttribute("poster");

            // CORRECTIF ARRÊT SILENCIEUX DE LA VIDÉO SUIVANTE APRÈS UN
            // "SUIVANT"/"PRÉCÉDENT" (NAVIGATION INSTANTANÉE) :
            // Seuls le poster et la source ci-dessus étaient jusqu'ici
            // resynchronisés sur la nouvelle vidéo. Tous les autres
            // attributs data-* du lecteur (data-stream-url, data-warm-url,
            // data-file-size, data-transcode-start-url,
            // data-transcode-status-url, data-transcode-stream-url —
            // voir templates/video.html) restaient ceux de l'ANCIENNE
            // vidéo, puisque le nœud <video> réellement affiché est
            // réutilisé (jamais recréé) d'une navigation à l'autre. Or
            // player-smooth.js s'appuie entièrement sur ces attributs
            // (réchauffage de la timeline, préchauffage de fond, et
            // surtout la bascule automatique de secours vers la
            // conversion en cas de démarrage trop lent ou de gel) : avec
            // des URLs pointant vers la vidéo précédente, cette bascule de
            // secours pouvait échanger la source de la vidéo qu'on vient
            // d'ouvrir contre le flux (ou le statut) de la vidéo
            // précédente — la nouvelle vidéo restait alors bloquée, sans
            // la moindre progression ni erreur visible. On resynchronise
            // donc désormais l'intégralité des attributs data-* du
            // lecteur sur ceux de la vidéo réellement chargée, à chaque
            // changement de vidéo.
            var i;
            for (i = persisted.attributes.length - 1; i >= 0; i--) {
              var oldAttr = persisted.attributes[i];
              if (oldAttr.name.indexOf("data-") === 0) persisted.removeAttribute(oldAttr.name);
            }
            for (i = 0; i < freshPlayer.attributes.length; i++) {
              var freshAttr = freshPlayer.attributes[i];
              if (freshAttr.name.indexOf("data-") === 0) persisted.setAttribute(freshAttr.name, freshAttr.value);
            }
            // Un éventuel garde-fou temporaire posé par la vidéo
            // précédente (voir commitSeek dans player-smooth.js) ne doit
            // jamais retarder le préchauffage de fond de cette nouvelle
            // vidéo.
            delete persisted.__pauseBackgroundWarmUntil;
            try { delete persisted.dataset.autoCompat; } catch (eAC) {}
            try { delete persisted.dataset.resumeReady; } catch (eRR) {}
          }
          var marker = result.doc.createElement("span");
          marker.setAttribute("data-main-player-marker", "1");
          freshPlayer.replaceWith(marker);
        }

        hideMiniPlayerHost();
        var hasPersistedSwap = !!(persisted && freshPlayer);
        swapInLayout(result.newLayout, result.doc, url, opts.pushState, hasPersistedSwap, opts.preserveSidebar);
        // Le cadre a été remplacé par le HTML frais : reposer le spinner
        // jusqu'à ce que le flux de la nouvelle vidéo soit prêt.
        markSwitching();

        // Renvoie directement à l'écran de lecture (haut de page, là où se
        // trouve le lecteur) : sans ceci, la navigation douce remplace le
        // contenu SANS jamais toucher au défilement, donc un clic sur une
        // case "À découvrir aussi" (ou précédent/suivant) tout en bas de
        // page laisse l'utilisateur scrollé en bas — face à la nouvelle
        // section "À découvrir aussi" de la vidéo suivante, comme si rien
        // ne s'était passé — au lieu du lecteur qui vient de démarrer.
        window.scrollTo(0, 0);
        // Depuis que .player-main et .player-related défilent chacun de
        // façon indépendante (voir static/css/style.css, section "Écran de
        // lecture + infos + bloc métadonnées FIGÉS..."), remettre la PAGE
        // en haut ne suffit plus : ce sont eux qui portent leur propre
        // scrollTop. Sans ce reset, une navigation douce depuis une carte
        // "À découvrir aussi" cliquée après avoir déjà scrollé ce volet
        // laissait le nouveau titre/les nouvelles infos hors champ (on ne
        // voyait que le bloc "Modifier les métadonnées" et le chemin du
        // fichier tout en bas, la vidéo restant collée en haut grâce au
        // sticky). "overflow-anchor: none" sur ces deux volets couvre déjà
        // le cas d'un ajustement automatique du navigateur pendant le
        // chargement ; ce reset couvre le cas où le volet est simplement
        // resté scrollé depuis une interaction précédente.
        var freshPlayerMain = layout.querySelector(".player-main");
        if (freshPlayerMain) freshPlayerMain.scrollTop = 0;
        var freshPlayerRelated = layout.querySelector(".player-related");
        if (freshPlayerRelated) freshPlayerRelated.scrollTop = 0;

        if (hasPersistedSwap) {
          var liveMarker = layout.querySelector("[data-main-player-marker]");
          persisted.classList.remove("mini-player-video");
          try { delete persisted.dataset.miniPlayer; } catch (eM) { /* ignore */ }
          if (liveMarker) {
            liveMarker.replaceWith(persisted);
          } else if (!layout.contains(persisted)) {
            layout.appendChild(persisted); // filet de sécurité improbable : ne jamais perdre la vidéo
          }
          if (!isSameVideo) {
            var heavyNow = (persisted.getAttribute("data-heavy-gate") === "1")
              || (persisted.dataset && persisted.dataset.heavyGate === "1");
            if (heavyNow) {
              // Codec lourd : vider toute source résiduelle et NE PAS
              // appeler play() — le portail VLC/MPV prend le relais.
              // load() une fois à vide pour annuler les Range en cours
              // (sinon le décodeur de la vidéo précédente reste actif).
              try { persisted.pause(); } catch (e0) {}
              while (persisted.firstChild) persisted.removeChild(persisted.firstChild);
              try { persisted.removeAttribute("src"); } catch (e1) {}
              try { persisted.preload = "none"; } catch (e1b) {}
              try { persisted.load(); } catch (e2) {}
              try { delete persisted.dataset.autoCompat; } catch (e3) {}
              if (window.__guaranteeTimers && window.__guaranteeTimers.length) {
                for (var gi0 = 0; gi0 < window.__guaranteeTimers.length; gi0++) {
                  clearTimeout(window.__guaranteeTimers[gi0]);
                }
                window.__guaranteeTimers = [];
              }
            } else if (pendingSrc) {
              var srcEl = persisted.querySelector("source");
              if (srcEl) srcEl.src = pendingSrc;
              else persisted.src = pendingSrc;
              // Un src résiduel posé directement sur le <video> par une
              // conversion de secours précédente prendrait sinon le pas sur
              // le <source> qu'on vient de mettre à jour.
              if (srcEl && persisted.hasAttribute("src")) persisted.removeAttribute("src");
              try { persisted.load(); } catch (e) { /* ignore */ }
              var playAttempt = persisted.play();
              if (playAttempt && playAttempt.catch) {
                playAttempt.catch(function () { /* la relance sera retentée via initGuaranteedAutoplay */ });
              }
              // Retirer le spinner dès que le flux a assez de données, ou
              // au plus tard après un court délai (filet de sécurité).
              var onReady = function () {
                clearSwitching();
                persisted.removeEventListener("loadeddata", onReady);
                persisted.removeEventListener("playing", onReady);
                persisted.removeEventListener("error", onReady);
              };
              persisted.addEventListener("loadeddata", onReady);
              persisted.addEventListener("playing", onReady);
              persisted.addEventListener("error", onReady);
              setTimeout(clearSwitching, 4000);
            } else {
              try { persisted.load(); } catch (e) { /* ignore */ }
              var playAttempt2 = persisted.play();
              if (playAttempt2 && playAttempt2.catch) {
                playAttempt2.catch(function () {});
              }
              clearSwitching();
            }
            if (heavyNow) clearSwitching();
            // -----------------------------------------------------------
            // GARANTIE ANTI-BLOCAGE DE LA VIDÉO SUIVANTE
            // -----------------------------------------------------------
            // Désactivée pour les codecs lourds (portail VLC) : un
            // "stillStuck" est normal (aucune source volontairement),
            // et un load()/play() forcé relancerait le décodage CPU.
            if (window.__guaranteeTimers && window.__guaranteeTimers.length) {
              for (var gi = 0; gi < window.__guaranteeTimers.length; gi++) {
                clearTimeout(window.__guaranteeTimers[gi]);
              }
            }
            window.__guaranteeTimers = [];
            if (!heavyNow) {
            (function guaranteeStart(expectedId) {
              var retried = false;
              function stillStuck() {
                var v = currentPlayer();
                if (!v || String(currentPersistedVideoId) !== String(expectedId)) return false;
                if (v.getAttribute("data-heavy-gate") === "1") return false;
                return v.readyState < 2 && v.currentTime === 0 && !v.error;
              }
              var t1 = setTimeout(function () {
                if (!stillStuck() || retried) return;
                retried = true;
                var v = currentPlayer();
                if (v && v.getAttribute("data-heavy-gate") === "1") return;
                try { v.load(); } catch (e) { /* ignore */ }
                var p2 = v.play();
                if (p2 && p2.catch) p2.catch(function () { /* dernier recours ci-dessous */ });
              }, 3500);
              window.__guaranteeTimers.push(t1);
              var t2 = setTimeout(function () {
                if (stillStuck()) window.location.href = url;
              }, 9000);
              window.__guaranteeTimers.push(t2);
            })(destId);
            }
          }
          // Si isSameVideo : on ne touche ni à la source ni à .load()/.play()
          // — la lecture, déjà en cours, se poursuit sans interruption ni
          // retour au début.
          if (isSameVideo) clearSwitching();
          activeVideoId = null; // le lecteur n'est plus réduit : plus de vidéo "active en mini" à suivre
          currentPersistedVideoId = destId; // le lecteur (plein écran désormais) correspond à cette vidéo
          // Le lecteur persistant est maintenant en place dans le nouveau
          // contenu : on (ré)initialise les fonctionnalités qui en
          // dépendent (vitesse, plein écran, lecture auto, aléatoire...)
          // seulement à présent, pour qu'elles s'accrochent au bon nœud.
          if (window.__initDynamicPage) window.__initDynamicPage(layout);
          // dynamic-page-init est déjà émis dans initDynamicPage (main.js)
        } else {
          clearSwitching();
        }
        if (token === navToken) { softNavInFlightUrl = null; window.__softNavBusy = false; }
        window.__softNavBusy = false;
      })
      .catch(function (err) {
        clearSwitching();
        if (token === navToken) { softNavInFlightUrl = null; window.__softNavBusy = false; }
        if (token !== navToken) return;
        // Abort volontaire (nouvelle nav) : ne pas hard-redirect
        if (err && err.name === "AbortError") return;
        window.location.href = url;
      });
  }

  window.__softNavigate = softNavigate;
  window.__softNavigateToVideo = softNavigateToVideo;

  // ---------------------------------------------------------------------
  // Interception des clics : tout lien interne de l'application (panneau
  // latéral, marque, paramètres, pagination, filtres, catégories,
  // playlists, liens vers une vidéo...) — uniquement quand une lecture
  // est en cours (un lecteur existe déjà, en page ou en mini-lecteur).
  // ---------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

    var a = e.target.closest("a[href]");
    if (!a) return;
    if (a.target && a.target !== "" && a.target !== "_self") return;
    if (a.hasAttribute("download")) return;

    var url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    if (url.origin !== location.origin) return;

    var destination = url.pathname + url.search;

    // Toujours navigation douce (même sans lecteur) : évite les rechargements
    // complets de tous les JS (gels de 10–30 s entre Paramètres / Accueil).
    if (isVideoPath(url.pathname)) {
      e.preventDefault();
      softNavigateToVideo(destination);
      return;
    }

    e.preventDefault();
    softNavigate(destination);
  });

  // ---------------------------------------------------------------------
  // Recherche depuis le bandeau du haut : ne coupe jamais la lecture en
  // cours (façon YouTube : la recherche s'affiche, le mini-lecteur reste
  // actif).
  // ---------------------------------------------------------------------
  var searchForm = document.querySelector(".topbar .searchbar");
  if (searchForm) {
    searchForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var action = searchForm.getAttribute("action") || location.pathname;
      var params = new URLSearchParams(new FormData(searchForm));
      var qs = params.toString();
      softNavigate(action + (qs ? "?" + qs : ""));
    });
  }

  // ---------------------------------------------------------------------
  // Mini-lecteur : clic pour revenir à la vidéo en plein affichage, ou
  // pour fermer (arrête la lecture).
  // ---------------------------------------------------------------------
  if (miniCloseBtn) {
    miniCloseBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      closeMiniPlayer();
    });
  }
  // Le lecteur principal n'a plus de contrôles natifs (voir templates/
  // video.html) : dans le mini-lecteur flottant, qui n'a pas la barre de
  // boutons dédiée de l'écran principal, un clic sur l'aperçu bascule
  // lecture/pause, pour ne pas perdre cette possibilité une fois réduit.
  if (miniSlot) {
    miniSlot.addEventListener("click", function (e) {
      e.stopPropagation();
      var v = miniSlot.querySelector("video");
      if (!v) return;
      if (v.paused || v.ended) {
        var p = v.play();
        if (p && p.catch) p.catch(function () { /* lecture refusée : rien de plus à faire ici */ });
      } else {
        v.pause();
      }
    });
  }
  function expandMiniPlayer(e) {
    if (e) e.stopPropagation();
    if (activeVideoId) softNavigateToVideo("/video/" + activeVideoId);
  }
  if (miniExpandBtn) miniExpandBtn.addEventListener("click", expandMiniPlayer);
  var miniBar = miniHost.querySelector(".mini-player-bar");
  if (miniBar) {
    miniBar.addEventListener("click", function (e) {
      if (e.target.closest("#mini-player-close") || e.target.closest("#mini-player-expand")) return;
      expandMiniPlayer(e);
    });
  }

  // ---------------------------------------------------------------------
  // Navigation précédente/suivante du navigateur (bouton Retour) : on
  // rejoue la navigation douce correspondante plutôt que de laisser le
  // navigateur recharger la page (ce qui couperait la lecture en cours).
  // Sans lecture en cours, un simple rechargement classique reste le
  // choix le plus sûr et le plus fidèle au comportement d'origine.
  // ---------------------------------------------------------------------
  window.addEventListener("popstate", function () {
    var url = location.pathname + location.search;
    var player = currentPlayer();
    if (!player) {
      window.location.reload();
      return;
    }
    if (isVideoPath(location.pathname)) {
      softNavigateToVideo(url, { pushState: false });
    } else {
      softNavigate(url, { pushState: false });
    }
  });
})();
