/*
 * player-smooth.js — Fiabilité et fluidité du lecteur vidéo (#main-player)
 * =====================================================================
 * Module 100% additif : ne modifie, ne supprime et ne remplace AUCUN
 * comportement existant (ne touche pas aux contrôles natifs du <video>,
 * ne coupe jamais l'autoplay, le plein écran, la vitesse, l'aléatoire,
 * ni la navigation instantanée gérée par player-persist.js).
 *
 * Trois améliorations, purement additives :
 *
 * 1) RÉCUPÉRATION AUTOMATIQUE EN CAS DE PLANTAGE/GEL
 *    Certains formats (notamment conteneurs comme .mkv/.avi/.ts) ou de
 *    gros fichiers haute résolution peuvent faire planter/geler le
 *    décodeur vidéo du navigateur en cours de lecture (erreur de décodage
 *    ponctuelle, accroc réseau sur un partage NAS, etc.). On surveille le
 *    lecteur en continu : au moindre signe de plantage (événement "error"),
 *    de gel prolongé ("waiting" qui traîne) ou d'arrêt silencieux de la
 *    progression (currentTime figé alors que la vidéo n'est ni en pause ni
 *    terminée), on relance automatiquement la lecture exactement là où
 *    elle s'est arrêtée, de façon quasi invisible pour l'utilisateur.
 *    Après plusieurs tentatives infructueuses, un message discret propose
 *    un nouvel essai manuel plutôt que de laisser le lecteur figé sans
 *    explication.
 *
 * 2) MONITEUR DE SACCADES (voir "1ter" dans le code) : détecte, via
 *    l'API standard getVideoPlaybackQuality(), les formats/codecs qui se
 *    lisent sans erreur mais saccadent (trop d'images perdues de façon
 *    soutenue) et bascule alors automatiquement sur la même conversion
 *    de secours que pour un plantage — la lecture redevient fluide quel
 *    que soit le format d'origine, sans action de l'utilisateur.
 *
 * 3) BARRE DE PROGRESSION FLUIDE (glissage timeline sans à-coups)
 *    Ajoutée SOUS le lecteur, en complément de la barre native (jamais à
 *    la place). Pendant qu'on fait glisser le curseur, l'affichage suit le
 *    doigt/la souris instantanément (pur CSS, aucune requête réseau), et
 *    un seul vrai saut ("seek") est envoyé au serveur au relâchement — au
 *    lieu de dizaines de requêtes partielles envoyées en rafale pendant le
 *    glissement (source fréquente de saccades, voire de plantage, en
 *    particulier sur les vidéos haute résolution ou les fichiers sur
 *    disque/réseau lents).
 *
 *    En complément, dès qu'on survole ou commence à glisser sur cette
 *    barre (donc AVANT le vrai saut), un léger signal est envoyé au
 *    serveur (voir /stream/<id>/warm dans app.py) pour qu'il réchauffe la
 *    zone du fichier visée en cache disque pendant qu'on hésite encore/
 *    qu'on termine le geste. Résultat : au relâchement, la zone a de
 *    bonnes chances d'être déjà chaude, ce qui supprime le petit arrêt de
 *    lecture qui suivrait sinon un saut à froid — en particulier sensible
 *    en navigation "au hasard" dans la timeline. Aucune donnée vidéo
 *    n'est transférée par ce signal ; il ne retarde ni ne remplace jamais
 *    le vrai saut.
 *
 * 4) LISSAGE VISUEL DE LA BARRE, INDÉPENDANT DE LA PUISSANCE MACHINE
 *    (voir "5" dans le code, section buildSeekBar) : entre deux
 *    évènements "timeupdate" du navigateur — dont la cadence n'est pas
 *    garantie et peut chuter sur une machine peu puissante en cours de
 *    décodage — la position affichée est extrapolée image par image via
 *    requestAnimationFrame à partir du dernier temps connu et du temps
 *    réellement écoulé, puis resynchronisée sur la vraie position à
 *    chaque évènement fiable. Coût par frame négligeable (un calcul et
 *    une écriture de style), sans lien avec le décodage vidéo : la barre
 *    avance donc toujours de façon fluide, quelle que soit la machine.
 * ---------------------------------------------------------------------
 */
(function () {
  "use strict";

  function fmtTime(t) {
    if (!isFinite(t) || t < 0) t = 0;
    t = Math.floor(t);
    var h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60);
    var s = t % 60;
    var mm = (h > 0 && m < 10) ? "0" + m : String(m);
    var ss = s < 10 ? "0" + s : String(s);
    return h > 0 ? (h + ":" + mm + ":" + ss) : (mm + ":" + ss);
  }

  // =====================================================================
  // 1) Watchdog de récupération automatique
  // =====================================================================
  function initPlaybackWatchdog(player, signal) {
    var MAX_ATTEMPTS = 5;
    // CORRECTIF codecs lourds (juste après scan) : pour les vidéos déjà
    // marquées codec_risky / heavy-gate, on détecte un gel beaucoup plus
    // vite (~2 s) et on bascule vers le mode compatible sans laisser le
    // décodage logiciel saturer le CPU pendant 8–10 s. Pour les codecs
    // légers, les seuils d'origine sont conservés.
    var isHeavy = (player.getAttribute("data-codec-risky") === "1")
      || (player.dataset && player.dataset.codecRisky === "1")
      || (player.getAttribute("data-heavy-gate") === "1")
      || (player.dataset && player.dataset.heavyGate === "1");
    var STALL_CHECK_MS = isHeavy ? 1500 : 4000;
    var STALL_ROUNDS = isHeavy ? 1 : 2;        // ~1.5 s (lourd) ou ~8 s
    var WAITING_TIMEOUT_MS = isHeavy ? 3000 : 10000;

    // Chaque (ré)initialisation (nouvelle vidéo, y compris après une
    // navigation instantanée) repart d'un état "actif" : un éventuel
    // drapeau de suspension laissé par la vidéo précédente (voir
    // suppressRecovery ci-dessous) ne doit jamais affecter celle-ci.
    delete player.dataset.suppressRecovery;

    var attempts = 0;
    var lastTime = -1;
    var stalledRounds = 0;
    var recovering = false;
    var waitingTimer = null;
    var banner = null;
    var bootTimer = null;

    // =====================================================================
    // CORRECTIF DÉFINITIF « LA VIDÉO SUIVANTE RESTE FIGÉE »
    // -----------------------------------------------------------------
    // Le nœud <video> est RÉUTILISÉ d'une vidéo à l'autre (navigation
    // instantanée). Les écouteurs d'évènements de la vidéo précédente
    // étaient bien coupés (AbortController), mais PAS les traitements
    // différés déjà programmés : sondage de conversion (poll), nouvelle
    // tentative de lecture retardée (attemptRecovery), suivi du bouton
    // « mode compatible »… Ces reliquats de la vidéo PRÉCÉDENTE pouvaient
    // s'exécuter une à plusieurs secondes APRÈS le changement de vidéo et
    // réécrire la source / relancer un .load() sur la vidéo qu'on vient
    // d'ouvrir : celle-ci restait alors définitivement figée, sans erreur
    // ni progression. On centralise donc ici TOUS les traitements
    // différés : un seul drapeau "stopped" les neutralise instantanément,
    // et tous les minuteurs encore en attente sont annulés d'un bloc dès
    // que ce lecteur passe à la vidéo suivante.
    // =====================================================================
    var stopped = false;
    var timers = [];
    function later(fn, ms) {
      if (stopped) return null;
      var id = setTimeout(function () {
        if (stopped) return;
        fn();
      }, ms);
      timers.push(id);
      return id;
    }
    function clearAllTimers() {
      for (var i = 0; i < timers.length; i++) clearTimeout(timers[i]);
      timers = [];
    }

    // =====================================================================
    // Détection moderne de gel via requestVideoFrameCallback (Chrome/Edge
    // /Safari récents). Si aucune frame n'est présentée pendant ~1,2 s sur
    // un codec lourd avec autoCompat armé → bascule immédiate, sans attendre
    // les seuils plus longs du polling currentTime.
    // =====================================================================
    var rvfcHandle = null;
    var lastFrameTs = 0;
    function armFrameWatch() {
      if (!isHeavy || typeof player.requestVideoFrameCallback !== "function") return;
      lastFrameTs = performance.now();
      function onFrame(now) {
        if (stopped) return;
        lastFrameTs = now;
        try {
          rvfcHandle = player.requestVideoFrameCallback(onFrame);
        } catch (e) { rvfcHandle = null; }
      }
      try {
        rvfcHandle = player.requestVideoFrameCallback(onFrame);
      } catch (e) { rvfcHandle = null; }
      // Vérifie toutes les 400 ms si des frames arrivent encore
      var frameCheck = setInterval(function () {
        if (stopped) { clearInterval(frameCheck); return; }
        if (player.paused || player.ended || recovering || transcoding || transcodeTried) {
          lastFrameTs = performance.now();
          return;
        }
        if (player.dataset.autoCompat === "1" && (performance.now() - lastFrameTs) > 1200) {
          clearInterval(frameCheck);
          tryTranscodeFallback();
        }
      }, 400);
      timers.push(frameCheck);
    }
    // Armé dès que la lecture démarre réellement
    player.addEventListener("playing", function oncePlay() {
      player.removeEventListener("playing", oncePlay);
      armFrameWatch();
    }, { once: true });

    // Le rechargement (player.load()) déclenché PAR notre propre logique de
    // récupération (même source, juste retentée) fait aussi partir un
    // évènement "loadstart" — sans ce drapeau, le compteur "attempts" serait
    // remis à zéro à chaque tentative, empêchant jamais d'atteindre
    // MAX_ATTEMPTS (donc jamais de bascule vers la conversion automatique
    // ni de message d'abandon). "loadstart" ne doit remettre le compteur à
    // zéro que pour un réel changement de vidéo, pas pour nos propres retries.
    var selfReload = false;

    function removeBanner() {
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
      banner = null;
    }

    // -----------------------------------------------------------------
    // CORRECTIF « infos mélangées sous le lecteur / pendant le scroll de
    // la colonne de droite » : ce bandeau (chargement long, saccades,
    // échec persistant) était jusqu'ici inséré DANS .video-frame, juste
    // après <video>. Or .video-frame fait partie de #player-sticky-col
    // (collé en haut de l'écran pendant le défilement de "À découvrir
    // aussi" à droite — voir static/css/style.css). Ajouter ce bandeau en
    // flux normal À L'INTÉRIEUR de ce bloc agrandissait sa boîte figée et
    // repoussait la rangée de contrôles (.player-overlay-bottom, positionnée
    // en absolu depuis le bas de .video-frame) par-dessus le titre/les
    // infos/le chemin du fichier en dessous — d'où le mélange visuel,
    // particulièrement visible pendant le scroll de la colonne de droite
    // puisque #player-sticky-col reste alors collé plus longtemps à l'écran.
    // Fix : insérer le bandeau juste APRÈS #player-sticky-col plutôt que
    // dedans. Il redevient un simple frère de ce bloc, dans le flux normal
    // — exactement comme le titre/les infos juste en dessous — et bénéficie
    // donc déjà de la règle existante "#player-sticky-col ~ *" qui le passe
    // proprement au-dessus de l'écran figé au lieu de passer dessous.
    // .video-frame retrouve une hauteur strictement égale à celle de la
    // vidéo : les contrôles restent bien superposés à l'image, jamais
    // repoussés. Purement additif : aucun autre comportement du bandeau
    // (contenu, bouton Réessayer, disparition) n'est modifié.
    function insertStatusBanner(el) {
      var stickyCol = document.getElementById("player-sticky-col");
      if (stickyCol && stickyCol.parentNode) {
        stickyCol.parentNode.insertBefore(el, stickyCol.nextSibling);
      } else {
        // Filet de sécurité si la structure attendue est absente.
        var wrap = player.parentNode;
        wrap.insertBefore(el, player.nextSibling);
      }
    }

    function showGiveUpBanner() {
      removeBanner();
      banner = document.createElement("div");
      banner.className = "smooth-player-error-banner";
      banner.innerHTML =
        '<span>⚠ La lecture rencontre un problème persistant sur cette vidéo.</span>' +
        '<button type="button" class="smooth-player-retry-btn">↻ Réessayer</button>';
      insertStatusBanner(banner);
      banner.querySelector(".smooth-player-retry-btn").addEventListener("click", function () {
        attempts = 0;
        removeBanner();
        attemptRecovery();
      });
    }

    // =====================================================================
    // 1bis) Secours : conversion automatique quand le format n'est
    // vraiment pas lisible nativement (voir /video/<id>/transcode/* et
    // /stream/<id>/transcode dans app.py). Déclenché uniquement une fois
    // toutes les tentatives de relecture directe épuisées, jamais avant —
    // ce module n'interfère donc en rien avec la lecture normale.
    // =====================================================================
    var transcodeTried = false;
    window.__tryTranscodeFallback = function () { tryTranscodeFallback(); };
    var transcoding = false; // true pendant toute la durée du secours par conversion (voir tryTranscodeFallback)

    function showTranscodeBanner(text, showRetry) {
      removeBanner();
      banner = document.createElement("div");
      banner.className = "smooth-player-error-banner";
      banner.innerHTML = '<span>' + text + '</span>' +
        (showRetry ? '<button type="button" class="smooth-player-retry-btn">↻ Réessayer</button>' : '');
      insertStatusBanner(banner);
      if (showRetry) {
        banner.querySelector(".smooth-player-retry-btn").addEventListener("click", function () {
          attempts = 0;
          transcodeTried = false;
          removeBanner();
          attemptRecovery();
        });
      }
    }

    function isHeavyGated() {
      return player.getAttribute("data-heavy-gate") === "1"
        || (player.dataset && player.dataset.heavyGate === "1")
        || !!document.getElementById("heavy-codec-gate");
    }

    function tryTranscodeFallback() {
      // Règle stricte : ffmpeg UNIQUEMENT si l'utilisateur a armé le mode
      // compatible (bouton 🚀 ou portail). Jamais en automatique — évite
      // les pics CPU imprévisibles après VLC / next sur vidéos simples.
      if (player.dataset.autoCompat !== "1") return;
      if (isHeavyGated() && player.dataset.autoCompat !== "1") return;
      var startUrl = player.dataset.transcodeStartUrl;
      var statusUrl = player.dataset.transcodeStatusUrl;
      var streamUrl = player.dataset.transcodeStreamUrl;
      if (!startUrl || !statusUrl || !streamUrl) {
        showGiveUpBanner();
        return;
      }
      transcodeTried = true;
      transcoding = true;
      var resumeTime = player.currentTime || 0;
      var wasPlaying = !player.paused;
      // Pour un conteneur ou codec non garanti par le navigateur, ne pas
      // laisser le décodage natif tourner en parallèle de ffmpeg : sur une
      // vidéo 4K cela double immédiatement la charge et provoque des
      // saccades. La conversion devient la seule source de lecture.
      if (player.dataset.autoCompat === "1" && wasPlaying) {
        try { player.pause(); } catch (e) { /* lecture déjà arrêtée */ }
      }
      showTranscodeBanner("⏳ Ce format ne se lit pas nativement — conversion automatique en cours (peut prendre un moment selon la longueur de la vidéo)…", false);

      fetch(startUrl, { method: "POST" }).catch(function () { /* le polling gère l'échec ensuite */ });

      var pollDelay = 2000;
      function poll() {
        if (stopped) return; // changement de vidéo entre-temps : ce sondage ne concerne plus le lecteur affiché
        fetch(statusUrl, signal ? { signal: signal } : undefined).then(function (r) { return r.json(); }).then(function (s) {
          if (stopped) return;
          if (s.status === "ready") {
            transcoding = false;
            var srcEl = player.querySelector("source");
            if (srcEl) srcEl.src = streamUrl; else player.src = streamUrl;
            player.load();
            var onReady = function () {
              player.removeEventListener("loadedmetadata", onReady);
              try { player.currentTime = resumeTime; } catch (e) { /* ignore */ }
              if (wasPlaying && !window.__userPausedPlayback) {
                var p = player.play();
                if (p && p.catch) p.catch(function () { /* rien à faire, l'utilisateur relancera */ });
              }
              removeBanner();
              attempts = 0;
            };
            player.addEventListener("loadedmetadata", onReady, { once: true });
          } else if (s.status === "error") {
            transcoding = false;
            showTranscodeBanner("⚠ La conversion automatique a échoué pour ce fichier. Utilise le bouton \"Ouvrir avec le lecteur externe\" ci-dessous (ex. VLC).", true);
          } else {
            later(poll, pollDelay);
          }
        }).catch(function () {
          later(poll, pollDelay);
        });
      }
      poll();
    }

    // =====================================================================
    // 1quinquies) Bouton manuel "Mode compatible / performance"
    // -----------------------------------------------------------------
    // Tous les mécanismes ci-dessus ne basculent sur la conversion de
    // secours qu'APRÈS avoir constaté un vrai problème (erreur, gel total
    // détecté après plusieurs secondes...). Sur une machine où le
    // décodage natif d'une vidéo haute résolution reste "lisible" mais
    // sature durablement le CPU (saccades, ralentissement du reste du PC,
    // sans jamais déclencher les seuils de gel ci-dessus), l'utilisateur
    // peut désormais basculer lui-même, immédiatement, sur cette même
    // conversion de secours — désormais accélérée par le GPU quand
    // disponible (voir scanner.HW_ENCODER côté serveur), donc nettement
    // moins coûteuse en CPU que l'ancien encodage logiciel seul. Purement
    // opt-in : n'change RIEN au comportement automatique existant, et n'a
    // aucun effet tant que l'utilisateur ne clique pas explicitement.
    // =====================================================================
    var compatBtn = document.getElementById("player-compat-mode-btn");
    if (compatBtn) {
      compatBtn.addEventListener("click", function () {
        if (transcoding) return; // déjà en cours : rien à faire de plus
        if (transcodeTried) return; // déjà basculé (banni ou déjà prêt) : rien à refaire
        compatBtn.disabled = true;
        compatBtn.title = "Conversion vers une version allégée en cours…";
        // Arme explicitement le mode compatible (consentement utilisateur)
        player.dataset.autoCompat = "1";
        tryTranscodeFallback();
        // Petit suivi (best-effort, purement visuel) : une fois la
        // conversion terminée — succès ou échec — on reflète l'état sur le
        // bouton. Sans incidence fonctionnelle si ce suivi échouait pour
        // une raison quelconque : la conversion elle-même (gérée par
        // tryTranscodeFallback) continue normalement dans tous les cas.
        var watchTicks = 0;
        var watch = setInterval(function () {
          if (stopped) { clearInterval(watch); return; }
          watchTicks++;
          if (!transcoding) {
            clearInterval(watch);
            if (transcodeTried) {
              compatBtn.title = "Version allégée active pour cette vidéo.";
              compatBtn.textContent = "🚀";
            } else {
              // Échec : on relaisse la main (voir bannière d'erreur, qui
              // propose déjà un "Réessayer" séparé).
              compatBtn.disabled = false;
              compatBtn.title = "La lecture rame ou saccade ? Bascule sur une version allégée de cette vidéo (conversion automatique, accélérée par le GPU si disponible).";
            }
          } else if (watchTicks > 1800) { // ~30 min : filet de sécurité, n'arrive jamais en pratique
            clearInterval(watch);
          }
        }, 1000);
      }, signal ? { signal: signal } : undefined);
    }

    // =====================================================================
    // 1quater) Watchdog de démarrage : lecture qui ne démarre JAMAIS
    // -----------------------------------------------------------------
    // Cas distinct de tous ceux gérés ci-dessus : sur certains flux H.265/
    // HEVC, le navigateur ne déclenche NI erreur ("error"), NI mise en
    // attente prolongée ("waiting" — cet évènement suppose qu'une tentative
    // de lecture ait au moins démarré), NI gel de progression détectable
    // (le moniteur de gel silencieux plus bas suppose lui aussi une lecture
    // déjà en cours) : le décodeur refuse silencieusement le flux et
    // l'élément vidéo reste inerte indéfiniment (aucune image, poster figé),
    // sans qu'aucun des mécanismes existants n'ait quoi que ce soit à quoi
    // réagir. On arme donc un délai de grâce généreux dès le chargement de
    // CETTE vidéo : si l'évènement "canplay" (qui suppose que le
    // navigateur ait réellement réussi à préparer des images décodées) n'a
    // toujours pas été atteint une fois ce délai écoulé, on considère que
    // la lecture native ne démarrera jamais et on bascule directement sur
    // la conversion de secours — plutôt que de laisser l'utilisateur devant
    // un lecteur silencieusement inerte sans aucun message.
    // =====================================================================
    var BOOT_TIMEOUT_MS = 12000;
    var bootReady = false;

    function armBootWatchdog() {
      bootReady = false;
      if (bootTimer) clearTimeout(bootTimer);
      bootTimer = later(function () {
        if (bootReady || transcoding || transcodeTried) return;
        if (player.error) return; // déjà géré par onError ci-dessus
        // JAMAIS de ffmpeg automatique ici : un simple buffer lent (disque
        // réseau, grosse vidéo) n'est PAS un codec incompatible. Lancer
        // une conversion saturait le CPU de façon imprévisible sur des
        // vidéos "simples". Conversion uniquement sur erreur fatale (code 4)
        // ou action utilisateur explicite (mode compatible).
        if (player.readyState < 3) {
          showTranscodeBanner(
            "⏳ Chargement encore en cours… Si ça bloque vraiment, utilise « Mode compatible » ou ouvre avec VLC.",
            true
          );
        }
      }, BOOT_TIMEOUT_MS);
    }

    function clearBootWatchdog() {
      bootReady = true;
      if (bootTimer) { clearTimeout(bootTimer); bootTimer = null; }
    }

    function isInMiniPlayer() {
      return player.dataset.miniPlayer === "1"
        || !!(player.closest && player.closest("#mini-player-video-slot"));
    }

    function attemptRecovery() {
      if (isHeavyGated() && player.dataset.autoCompat !== "1") return;
      if (stopped) return; // ce watchdog appartient à une vidéo qui n'est plus affichée
      if (player.dataset.suppressRecovery === "1") return; // suppression en cours : ne pas interférer
      if (transcoding) return; // conversion de secours déjà en cours : ne pas interférer (voir tryTranscodeFallback)
      if (recovering) return;
      if (player.ended) return; // fin normale de vidéo : rien à récupérer
      // Pause volontaire de l'utilisateur : ne jamais relancer la lecture.
      if (window.__userPausedPlayback || player.paused) return;

      // Mini-lecteur + codec lourd : player.load() coupe souvent le flux.
      // On bascule vers le mode compatible au lieu de recharger la source.
      if (isInMiniPlayer() && (isHeavy || player.dataset.codecRisky === "1") && !transcodeTried) {
        player.dataset.autoCompat = "1";
        tryTranscodeFallback();
        return;
      }

      attempts += 1;
      if (attempts > MAX_ATTEMPTS) {
        if (isInMiniPlayer() && !transcodeTried) {
          player.dataset.autoCompat = "1";
          tryTranscodeFallback();
          return;
        }
        showGiveUpBanner();
        return;
      }
      recovering = true;
      var resumeTime = player.currentTime || 0;
      var wasPlaying = !player.paused && !window.__userPausedPlayback;
      var srcEl = player.querySelector("source");
      var src = srcEl ? srcEl.src : player.currentSrc;

      var delay = Math.min(attempts * 700, 4000);
      later(function () {
        try {
          selfReload = true;
          if (srcEl && src) srcEl.src = src;
          player.load();
        } catch (e) { /* ignore */ }

        var onReady = function () {
          player.removeEventListener("loadedmetadata", onReady);
          try { player.currentTime = resumeTime; } catch (e) { /* ignore */ }
          if (wasPlaying && !window.__userPausedPlayback) {
            var p = player.play();
            if (p && p.catch) p.catch(function () { /* relance retentée par ailleurs */ });
          }
          recovering = false;
        };
        player.addEventListener("loadedmetadata", onReady, { once: true });
        // Filet de sécurité : si "loadedmetadata" ne revient jamais, on ne
        // reste pas bloqué en "recovering" indéfiniment.
        later(function () { recovering = false; }, 6000);
      }, delay);
    }

    function onError() {
      // Code 4 = MEDIA_ERR_SRC_NOT_SUPPORTED : le navigateur indique que ce
      // fichier ne peut PAS être décodé, point (typiquement du H.265/HEVC
      // sans décodeur matériel ni logiciel disponible dans ce navigateur/
      // cette machine). Recharger le même fichier via attemptRecovery() ne
      // peut que reproduire exactement la même erreur à chaque tentative —
      // inutile de consommer les 5 essais prévus pour les erreurs
      // transitoires (accroc réseau/disque, décodage ponctuel), qui ne font
      // ici que retarder de plusieurs secondes le vrai remède déjà en place
      // : la conversion automatique de secours. On y bascule donc tout de
      // suite dans ce cas précis ; tout autre code d'erreur (1/2/3) garde
      // exactement le comportement existant via attemptRecovery().
      if (player.error && player.error.code === 4 && !transcodeTried && !transcoding) {
        // Format non supporté nativement : proposer, ne pas convertir en silence.
        if (player.dataset.autoCompat === "1") {
          tryTranscodeFallback();
        } else {
          showTranscodeBanner(
            "⚠️ Ce format ne se lit pas nativement. Ouvre avec VLC/MPV, ou lance le mode compatible.",
            true
          );
        }
        return;
      }
      attemptRecovery();
    }

    function onWaiting() {
      if (waitingTimer) clearTimeout(waitingTimer);
      waitingTimer = later(function () {
        if (window.__userPausedPlayback) return;
        if (player.paused || player.ended) return;
        // Codec lourd + mode compatible armé : bascule directe.
        if (isHeavy && player.dataset.autoCompat === "1" && !transcodeTried && !transcoding) {
          tryTranscodeFallback();
          return;
        }
        // Mini-lecteur + codec lourd : armer autoCompat puis basculer
        // (évite l'arrêt imprévisible après soft-nav).
        if (isInMiniPlayer() && isHeavy && !transcodeTried && !transcoding) {
          player.dataset.autoCompat = "1";
          tryTranscodeFallback();
          return;
        }
        attemptRecovery();
      }, WAITING_TIMEOUT_MS);
    }
    function clearWaitingTimer() {
      if (waitingTimer) { clearTimeout(waitingTimer); waitingTimer = null; }
    }

    var listenerOpts = signal ? { signal } : undefined;
    player.addEventListener("error", onError, listenerOpts);
    player.addEventListener("waiting", onWaiting, listenerOpts);
    player.addEventListener("playing", clearWaitingTimer, listenerOpts);
    player.addEventListener("canplay", clearWaitingTimer, listenerOpts);
    player.addEventListener("playing", clearBootWatchdog, listenerOpts);
    player.addEventListener("canplay", clearBootWatchdog, listenerOpts);
    player.addEventListener("timeupdate", function () {
      // Une vraie progression réinitialise le compteur de tentatives et
      // efface un éventuel message d'erreur affiché précédemment.
      if (player.currentTime !== lastTime) {
        attempts = 0;
        stalledRounds = 0;
        if (banner) removeBanner();
      }
    }, listenerOpts);
    player.addEventListener("loadstart", function () {
      if (selfReload) {
        // Rechargement déclenché par notre propre tentative de récupération
        // (même vidéo) : on ne remet PAS le compteur à zéro, sinon
        // MAX_ATTEMPTS ne serait jamais atteint (voir "selfReload" ci-dessus).
        selfReload = false;
        return;
      }
      // Nouvelle vidéo chargée (changement de source, navigation) : on repart de zéro.
      attempts = 0;
      stalledRounds = 0;
      lastTime = -1;
      removeBanner();
      armBootWatchdog();
    }, listenerOpts);
    // Armement initial : la toute première charge de la page ne déclenche
    // pas nécessairement un évènement "loadstart" observable après la pose
    // de cet écouteur (le <video> a déjà commencé à charger avant que ce
    // script ne s'exécute) — on arme donc aussi directement ici.
    armBootWatchdog();

    // Détecte un gel silencieux (aucun événement "waiting"/"error" déclenché,
    // mais la progression n'avance plus alors que la vidéo devrait jouer) —
    // arrive sur certains décodages de vidéos haute résolution.
    var interval = setInterval(function () {
      if (stopped) { clearInterval(interval); return; }
      if (player.paused || player.ended || player.seeking || recovering) {
        stalledRounds = 0;
        lastTime = player.currentTime;
        return;
      }
      if (player.currentTime === lastTime) {
        stalledRounds += 1;
        if (stalledRounds >= STALL_ROUNDS) {
          stalledRounds = 0;
          attemptRecovery();
        }
      } else {
        stalledRounds = 0;
      }
      lastTime = player.currentTime;
    }, STALL_CHECK_MS);

    // =====================================================================
    // 1ter) Moniteur de qualité de décodage (gel silencieux des images)
    // -----------------------------------------------------------------
    // Certains formats/codecs (H.265/HEVC, conteneurs peu optimisés)
    // restent "lisibles" par le navigateur (pas d'erreur, pas de gel
    // détecté ci-dessus) mais le décodeur vidéo se bloque en silence :
    // AUCUNE nouvelle image n'est plus produite du tout, alors que la
    // lecture progresse réellement (piste audio qui avance seule). On
    // bascule alors sur la même conversion de secours que pour les
    // plantages (voir tryTranscodeFallback ci-dessus).
    //
    // Ce moniteur ne réagit PLUS à un simple taux élevé d'images perdues
    // (ancien seuil : 12% sur ~15s). Ce taux peut être élevé sur une
    // vidéo simplement lourde à décoder (haute résolution, ex. 2560×1440,
    // ou haut bitrate) alors que la lecture reste parfaitement
    // fonctionnelle — le décodage logiciel peine mais avance. Déclencher
    // dans ce cas une conversion ffmpeg complète (coûteuse en CPU)
    // AGGRAVAIT la situation au lieu de la résoudre : le décodage natif
    // continuait pendant que l'encodage de secours tournait en même
    // temps, cumulant les deux charges et faisant ramer tout le PC —
    // c'était la cause du ralentissement observé précisément sur les
    // vidéos haute résolution. Seul un arrêt TOTAL et prolongé de la
    // production d'images (cas ci-dessous) indique un vrai échec de
    // décodage justifiant la conversion de secours.
    // =====================================================================
    if (typeof player.getVideoPlaybackQuality === "function") {
      // Même logique accélérée pour les codecs lourds : ~2 s au lieu de ~10 s
      var QUALITY_CHECK_MS = isHeavy ? 1500 : 5000;
      var prevDropped = null;
      var prevTotal = null;
      var FRAME_STALL_ROUNDS_NEEDED = isHeavy ? 1 : 2;
      var frameStallRounds = 0;

      var qualityInterval = setInterval(function () {
        if (player.paused || player.ended || player.seeking || recovering || transcoding || transcodeTried) {
          prevDropped = prevTotal = null;
          frameStallRounds = 0;
          return;
        }
        var q;
        try { q = player.getVideoPlaybackQuality(); } catch (e) { return; }
        if (!q || typeof q.droppedVideoFrames !== "number" || typeof q.totalVideoFrames !== "number") return;

        if (prevDropped !== null && prevTotal !== null) {
          var totalDelta = q.totalVideoFrames - prevTotal;

          if (totalDelta <= 0 && player.currentTime > 0.5) {
            frameStallRounds += 1;
            if (frameStallRounds >= FRAME_STALL_ROUNDS_NEEDED) {
              frameStallRounds = 0;
              if (player.dataset.autoCompat === "1") {
                tryTranscodeFallback();
              } else {
                showTranscodeBanner(
                  "⚠️ Lecture saccadée. Clique « Mode compatible » (🚀) ou ouvre avec VLC.",
                  true
                );
              }
              return;
            }
          } else {
            frameStallRounds = 0;
          }
        }
        prevDropped = q.droppedVideoFrames;
        prevTotal = q.totalVideoFrames;
      }, QUALITY_CHECK_MS);

      if (signal) {
        signal.addEventListener("abort", function () {
          clearInterval(qualityInterval);
        }, { once: true });
      }
    }

    if (signal) {
      signal.addEventListener("abort", function () {
        stopped = true;
        clearAllTimers();
        clearInterval(interval);
        clearWaitingTimer();
        // CORRECTIF : le watchdog de démarrage (armBootWatchdog, déclenché
        // ici même par le "loadstart" natif que provoque le .load() de
        // player-persist.js juste AVANT que ce signal ne soit coupé) posait
        // un bootTimer qui n'était jamais annulé ici, contrairement à
        // "interval" et "waitingTimer" ci-dessus. Ce timer fantôme de
        // l'ANCIENNE vidéo restait actif jusqu'à 12s après un changement de
        // vidéo (bouton "Suivant"/"Précédent", enchaînement automatique...),
        // en parallèle du nouveau watchdog légitime de la vidéo suivante.
        // Si la vidéo suivante mettait un peu de temps à atteindre
        // readyState >= 3 (cas fréquent juste après un changement, cache
        // froid), les DEUX watchdogs se déclenchaient alors en même temps et
        // lançaient chacun leur propre conversion de secours (bannière,
        // requête de transcodage, .load()/.play() concurrents) sur le MÊME
        // lecteur — c'était la cause du "bug" observé sur la vidéo suivante
        // après un clic sur "Suivant". On annule donc systématiquement ce
        // timer ici aussi, exactement comme les autres.
        if (bootTimer) { clearTimeout(bootTimer); bootTimer = null; }
        removeBanner();
      }, { once: true });
    }

    // JAMAIS de transcodage automatique au chargement : un ffmpeg lancé
    // sans action utilisateur peut monter le CPU à 100 %. Le mode compatible
    // ne démarre que sur clic explicite (bouton 🚀 ou portail codec lourd).
    // data-auto-compat="1" est conservé pour compatibilité mais ignoré ici.
    // if (player.dataset.autoCompat === "1") { tryTranscodeFallback(); }
  }

  // =====================================================================
  // Réchauffement d'une zone temporelle donnée (deux volets, voir détail
  // plus bas dans buildSeekBar) — factorisé ici pour être partagé entre le
  // survol/glissement de la timeline ET le préchauffage proactif de toute
  // la vidéo (initTimelinePrewarm, plus bas), qui utilise volontairement
  // une fenêtre plus petite par point (beaucoup de points à couvrir).
  // =====================================================================
  var DEFAULT_FETCH_AHEAD_BYTES = 3 * 1024 * 1024; // ~3 Mo : survol/glissement (un seul point ciblé, on peut se permettre large)

  // ---------------------------------------------------------------------
  // CORRECTIF ARRÊTS EN NAVIGATION "AU HASARD" DANS LA TIMELINE
  // -----------------------------------------------------------------
  // Cause identifiée : le "vrai bloc d'octets" ci-dessous (volet 1) est un
  // authentique téléchargement de plusieurs Mo via fetch(), et il était
  // jusqu'ici RE-DÉCLENCHÉ sans jamais annuler le précédent (survol qui se
  // déplace, glissement qui progresse toutes les 350ms...). Un navigateur
  // n'ouvrant qu'un nombre très limité de connexions simultanées vers la
  // même origine (~6 en HTTP/1.1, le cas ici en local), une navigation "au
  // hasard" (plusieurs survols/glissements rapprochés) empile plusieurs de
  // ces téléchargements de plusieurs Mo en même temps — jusqu'à saturer
  // tout le pool de connexions. Le VRAI saut, déclenché ensuite par le
  // lecteur natif au relâchement, doit alors ATTENDRE qu'une connexion se
  // libère avant même de démarrer : c'est exactement l'arrêt observé.
  //
  // Correction (sans réduire la couverture ni imposer de plafond de
  // taille/nombre) : chaque nouvel appel annule proprement, via
  // AbortController, le téléchargement spéculatif précédent avant d'en
  // lancer un nouveau — au plus UN seul en vol à la fois — et le vrai saut
  // (commitSeek) annule lui aussi tout téléchargement spéculatif encore en
  // cours juste avant de partir, pour ne jamais avoir à faire la queue
  // derrière sa propre optimisation.
  // ---------------------------------------------------------------------

  function warmVideoRegion(player, t, dur, bytesAhead, serverSpanBytes, abortSignal) {
    // Pendant la lecture (hors seek), ne pas solliciter le disque : évite les gels
    try {
      if (player && !player.paused && !player.ended && !player.seeking) {
        return [];
      }
    } catch (eSkip) {}
    bytesAhead = bytesAhead || DEFAULT_FETCH_AHEAD_BYTES;
    var warmUrlBase = player.dataset.warmUrl || "";
    var streamUrl = player.dataset.streamUrl || "";
    var fileSize = parseInt(player.dataset.fileSize, 10) || 0;
    var tasks = [];

    // 1) Vrai bloc d'octets, mis en cache HTTP navigateur : si le vrai saut
    // qui suit demande une plage chevauchante, le navigateur peut la servir
    // directement depuis son cache, sans le moindre aller-retour réseau.
    // Annulable (abortSignal) : superflu dès qu'une position plus récente
    // est visée, pour ne jamais concurrencer le vrai saut sur le pool de
    // connexions limité du navigateur (voir note ci-dessus).
    // CORRECTIF DÉFINITIF (arrêts en navigation timeline + CPU) :
    // ce volet téléchargeait réellement plusieurs Mo par survol/glissement
    // via /stream. Chaque téléchargement mobilisait une des rares
    // connexions du navigateur ET faisait relire au serveur ces mêmes Mo
    // (lecture disque + transfert + travail Python) EN PLUS du flux vidéo
    // réel en cours — donc précisément la contention qui provoquait les
    // petits arrêts pendant la navigation, et une bonne part de la charge
    // CPU du serveur pendant la lecture. Il est supprimé : le navigateur
    // sait parfaitement chercher lui-même les octets dont il a besoin au
    // moment du saut, et le seul préchauffage conservé (volet 2 ci-dessous)
    // ne transfère AUCUNE donnée — il se contente de demander au système
    // de mettre la zone en cache disque.
    void streamUrl; void fileSize; void abortSignal;

    // 2) Réchauffement OS/disque d'une fenêtre plus large côté serveur,
    // sans le moindre transfert au navigateur (quasi gratuit) — absorbe
    // l'imprécision de l'estimation temps → octet.
    if (warmUrlBase) {
      // span : par défaut basé sur bytesAhead (comportement du survol/
      // glissement, seul appelant actuel) ; serverSpanBytes permet à un
      // futur appelant de fournir sa propre valeur si besoin.
      // Fenêtre volontairement modeste : au-delà, le gain devient nul
      // alors que le coût disque côté serveur, lui, continue de croître.
      var span = (serverSpanBytes != null) ? serverSpanBytes : Math.round(bytesAhead);
      span = Math.max(256 * 1024, Math.min(span, 4 * 1024 * 1024));
      try {
        if (window.__timelineWarmAbort) {
          try { window.__timelineWarmAbort.abort(); } catch (eA) {}
        }
        window.__timelineWarmAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
        var wopts = { method: "GET", cache: "no-store", keepalive: true };
        if (window.__timelineWarmAbort) wopts.signal = window.__timelineWarmAbort.signal;
        tasks.push(
          fetch(warmUrlBase + "?t=" + t.toFixed(1) + "&span=" + Math.round(span), wopts)
            .catch(function () { /* idem : simple optimisation */ })
        );
      } catch (e) { /* idem */ }
    }

    return tasks.length ? Promise.all(tasks) : Promise.resolve();
  }

  // =====================================================================
  // 2) Barre de progression fluide (additive, sous le lecteur)
  // =====================================================================
  function buildSeekBar(player, signal) {
    var wrap = document.createElement("div");
    wrap.className = "smooth-seek-wrap";
    wrap.innerHTML =
      '<div class="smooth-seek-bar" role="slider" tabindex="0" aria-label="Position dans la vidéo">' +
      '  <div class="smooth-seek-buffered"></div>' +
      '  <div class="smooth-seek-fill"></div>' +
      '  <div class="smooth-seek-handle"></div>' +
      '  <div class="smooth-seek-tooltip"></div>' +
      '</div>' +
      '<div class="smooth-seek-times">' +
      '  <span class="smooth-seek-current">0:00</span>' +
      '  <span class="smooth-seek-duration">0:00</span>' +
      '</div>';

    var bar = wrap.querySelector(".smooth-seek-bar");
    var fill = wrap.querySelector(".smooth-seek-fill");
    var handle = wrap.querySelector(".smooth-seek-handle");
    var buffered = wrap.querySelector(".smooth-seek-buffered");
    var tooltip = wrap.querySelector(".smooth-seek-tooltip");
    var curEl = wrap.querySelector(".smooth-seek-current");
    var durEl = wrap.querySelector(".smooth-seek-duration");

    var dragging = false;
    var dragRatio = 0;

    // -------------------------------------------------------------------
    // Réchauffement anticipé, à DEUX niveaux, dès qu'on survole ou commence
    // à glisser sur la timeline — donc AVANT le vrai saut, qui lui n'est
    // envoyé qu'au relâchement (voir commitSeek). Objectif : que la zone
    // visée soit déjà chaude, aussi bien côté disque que côté navigateur,
    // par le temps que l'utilisateur relâche réellement le curseur — ce qui
    // supprime l'arrêt de lecture qui suivrait sinon un saut à froid,
    // perceptible en navigation "au hasard" dans la timeline :
    //
    //   1) Un vrai petit bloc d'octets (quelques Mo) est demandé, avec un
    //      en-tête Range, DIRECTEMENT à l'URL de streaming réelle
    //      (data-stream-url — la même que celle utilisée par le lecteur).
    //      Cette réponse partielle (206), servie avec ETag/Last-Modified et
    //      Cache-Control (voir _serve_video_file dans app.py), est mise en
    //      cache HTTP par le navigateur : si le vrai saut qui suit demande
    //      une plage chevauchante, le navigateur peut la servir directement
    //      depuis son propre cache, SANS le moindre aller-retour réseau.
    //      Effet secondaire utile : cette lecture réelle réchauffe aussi le
    //      cache disque/OS du serveur pour cette zone exacte.
    //   2) En parallèle, /stream/<id>/warm (voir app.py) réchauffe une
    //      fenêtre plus large côté serveur UNIQUEMENT en cache disque/OS
    //      (sans transfert au navigateur, donc quasi gratuit) : une marge
    //      de sécurité qui absorbe l'imprécision de l'estimation temps →
    //      octet (le débit n'est jamais parfaitement constant sur un
    //      fichier réel), pour le cas où le vrai saut atterrit un peu à
    //      côté de la petite fenêtre exacte réchauffée en (1).
    //
    // Les deux appels sont "fire-and-forget" (aucune donnée renvoyée n'est
    // utilisée directement ici) : purement additifs, ils ne remplacent ni
    // ne retardent jamais le vrai saut.
    var lastWarmedT = -1;
    var lastWarmCallMs = 0;
    var hoverWarmTimer = null;
    // Téléchargement spéculatif (volet 1 de warmVideoRegion) actuellement en
    // vol, le cas échéant : au plus un seul à la fois, voir note plus haut.
    var pendingWarmAbort = null;

    function cancelPendingWarmFetch() {
      if (pendingWarmAbort) {
        try { pendingWarmAbort.abort(); } catch (e) { /* ignore */ }
        pendingWarmAbort = null;
      }
    }

    function warmAt(ratio) {
      var dur = player.duration;
      if (!dur || !isFinite(dur)) return;
      var t = ratio * dur;
      // Évite de spammer le serveur pour rien : les fenêtres réchauffées
      // couvrent déjà plusieurs Mo (donc plusieurs secondes de vidéo),
      // inutile de réchauffer deux fois quasiment la même zone.
      if (Math.abs(t - lastWarmedT) < 3) return;
      lastWarmedT = t;
      lastWarmCallMs = Date.now();
      // Une position plus récente est visée : le téléchargement spéculatif
      // précédent (s'il traîne encore) ne sert plus à rien et ne doit
      // surtout pas continuer à occuper une connexion — on l'annule avant
      // d'en lancer un nouveau (voir note en tête de fichier).
      cancelPendingWarmFetch();
      var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      pendingWarmAbort = ctrl;
      warmVideoRegion(player, t, dur, undefined, undefined, ctrl ? ctrl.signal : undefined)
        .then(function () { if (pendingWarmAbort === ctrl) pendingWarmAbort = null; })
        .catch(function () { if (pendingWarmAbort === ctrl) pendingWarmAbort = null; });
    }

    // Survol (pas de glissement en cours) : réchauffe seulement quand le
    // curseur se stabilise un instant sur une position, pas à chaque pixel
    // parcouru pendant le mouvement.
    function warmOnHover(ratio) {
      if (hoverWarmTimer) clearTimeout(hoverWarmTimer);
      hoverWarmTimer = setTimeout(function () { warmAt(ratio); }, 150);
    }

    // Glissement en cours : un vrai saut réseau arrivera au relâchement,
    // potentiellement après plusieurs secondes de glissement lent — on
    // réchauffe donc régulièrement (au plus 1 fois/350ms) la position
    // survolée pendant le trajet, pour garder une longueur d'avance même
    // si l'utilisateur change souvent d'avis avant de relâcher.
    function warmDuringDrag(ratio) {
      if (Date.now() - lastWarmCallMs < 350) return;
      warmAt(ratio);
    }

    function ratioFromEvent(e) {
      var rect = bar.getBoundingClientRect();
      var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      var r = rect.width > 0 ? x / rect.width : 0;
      return Math.max(0, Math.min(1, r));
    }

    function setVisual(ratio) {
      var pct = (ratio * 100).toFixed(2) + "%";
      fill.style.width = pct;
      handle.style.left = pct;
    }

    function updateBuffered() {
      var dur = player.duration;
      if (!dur || !isFinite(dur) || player.buffered.length === 0) return;
      var end = player.buffered.end(player.buffered.length - 1);
      buffered.style.width = Math.min(100, (end / dur) * 100) + "%";
    }

    function refresh() {
      if (dragging) return;
      var dur = player.duration;
      if (dur && isFinite(dur)) {
        setVisual(player.currentTime / dur);
        durEl.textContent = fmtTime(dur);
      }
      curEl.textContent = fmtTime(player.currentTime);
      updateBuffered();
    }

    // =====================================================================
    // 5) LISSAGE VISUEL ENTRE DEUX "timeupdate" (purement additif)
    // -----------------------------------------------------------------
    // L'évènement natif "timeupdate" ne se déclenche pas à une fréquence
    // garantie ni régulière (quelques fois par seconde en général, mais
    // cette cadence peut chuter — voire devenir franchement irrégulière —
    // quand la machine est chargée, typiquement pendant un décodage vidéo
    // lourd sur un PC peu puissant ou un gros fichier haute résolution).
    // Sans compensation, le remplissage de la barre avance par petits
    // à-coups visibles au lieu d'un mouvement continu : un défaut
    // purement visuel (la lecture elle-même n'a ici aucun souci) mais
    // perceptible, en particulier sur les machines les moins puissantes.
    //
    // Correctif : entre deux "timeupdate" (ou "seeked"/"play"/
    // "ratechange"/"loadedmetadata", qui resynchronisent aussi l'ancrage
    // ci-dessous sur la vraie position du lecteur), on extrapole la
    // position affichée à partir du dernier temps connu et du temps réel
    // écoulé depuis (performance.now()), recalculée à chaque frame via
    // requestAnimationFrame. Le coût par frame est un simple calcul
    // arithmétique plus une écriture de style CSS : négligeable et
    // totalement indépendant de la puissance de décodage vidéo de la
    // machine — la barre avance donc de façon fluide même sur un PC qui
    // peine à décoder le flux. Aucune extrapolation trompeuse tant que la
    // lecture ne progresse pas réellement (pause, fin, recherche en
    // cours, mise en tampon) : on retombe alors simplement sur la vraie
    // position courante du lecteur.
    // =====================================================================
    var anchorTime = player.currentTime || 0;
    var anchorPerf = (window.performance && performance.now) ? performance.now() : Date.now();
    function resyncAnchor() {
      anchorTime = player.currentTime || 0;
      anchorPerf = (window.performance && performance.now) ? performance.now() : Date.now();
    }

    var rafHandle = null;
    var lastDisplayedRatio = -1;
    var lastDisplayedText = "";
    function visualTick() {
      rafHandle = requestAnimationFrame(visualTick);
      // CORRECTIF CPU : aucune raison de recalculer/réécrire l'affichage 60
      // fois par seconde quand l'onglet est masqué, quand la lecture est
      // en pause/terminée, ou quand la barre n'est pas visible à l'écran :
      // on ne fait alors strictement rien (la resynchronisation exacte
      // reste assurée par les évènements natifs du lecteur).
      if (document.hidden) return;
      if (!dragging && (player.paused || player.ended)) return;
      if (dragging) return; // le glissement garde seul la main sur l'affichage (voir onPointerMove)
      var dur = player.duration;
      if (!dur || !isFinite(dur)) return;
      var display;
      var reallyAdvancing = !player.paused && !player.ended && !player.seeking && player.readyState >= 3;
      if (reallyAdvancing) {
        var now = (window.performance && performance.now) ? performance.now() : Date.now();
        var rate = player.playbackRate || 1;
        display = anchorTime + ((now - anchorPerf) / 1000) * rate;
        if (display < 0) display = 0;
        if (display > dur) display = dur;
      } else {
        display = player.currentTime || 0;
      }
      var ratio = display / dur;
      if (ratio !== lastDisplayedRatio) {
        lastDisplayedRatio = ratio;
        setVisual(ratio);
      }
      var text = fmtTime(display);
      if (text !== lastDisplayedText) {
        lastDisplayedText = text;
        curEl.textContent = text;
      }
    }
    if (typeof requestAnimationFrame === "function") {
      rafHandle = requestAnimationFrame(visualTick);
    }

    function showTooltip(ratio, clientX) {
      var dur = player.duration;
      if (!dur || !isFinite(dur)) { tooltip.style.display = "none"; return; }
      var rect = bar.getBoundingClientRect();
      tooltip.textContent = fmtTime(ratio * dur);
      tooltip.style.display = "block";
      var left = clientX - rect.left;
      tooltip.style.left = Math.max(0, Math.min(rect.width, left)) + "px";
    }
    function hideTooltip() { tooltip.style.display = "none"; }

    var listenerOpts = signal ? { signal } : undefined;

    // ---- Glissement : seul un vrai saut réseau est déclenché, au
    // relâchement — le glissement lui-même est 100% visuel et instantané.
    function onPointerDown(e) {
      if (e.button !== undefined && e.button !== 0) return;
      dragging = true;
      dragRatio = ratioFromEvent(e);
      setVisual(dragRatio);
      showTooltip(dragRatio, e.touches ? e.touches[0].clientX : e.clientX);
      bar.classList.add("dragging");
      // Un vrai saut est désormais quasi certain : on lance le
      // réchauffement immédiatement, sans attendre (pas de debounce ici),
      // pour lui donner le maximum de temps avant le relâchement.
      warmAt(dragRatio);
      e.preventDefault();
    }
    // -------------------------------------------------------------------
    // CORRECTIF FLUIDITÉ : survol déclenché par TOUT mouvement de souris
    // -----------------------------------------------------------------
    // onPointerMove est posé à la fois sur la barre (survol réel) ET sur
    // window (nécessaire pour continuer de suivre un glissement déjà en
    // cours même si le curseur sort du tracé exact de la barre pendant le
    // geste). Ces deux rôles doivent rester strictement séparés : sans
    // cela, la branche "survol" (tooltip + réchauffement réseau
    // spéculatif) se déclenchait sur N'IMPORTE QUEL mouvement de souris,
    // n'importe où sur toute la page — pas seulement au-dessus de la
    // timeline — puisque le seul filtre était "pas en train de glisser",
    // jamais "le curseur est-il réellement sur la barre". Chaque
    // stabilisation de 150ms de la souris ailleurs sur la page déclenchait
    // alors un téléchargement spéculatif de plusieurs Mo pour une position
    // n'ayant aucun rapport avec l'intention réelle de l'utilisateur — un
    // gaspillage constant qui vient occuper l'une des rares connexions
    // simultanées du navigateur vers le serveur, retardant d'autant les
    // vraies requêtes de lecture. Exactement le genre de micro-arrêt
    // évitable que tout le reste de ce fichier cherche à éliminer.
    //
    // Séparation : onBarHover (posé UNIQUEMENT sur la barre) gère le vrai
    // survol ; onPointerMove (posé UNIQUEMENT sur window) ne fait plus que
    // poursuivre un glissement déjà engagé, sans plus jamais réagir au
    // moindre mouvement de souris ordinaire ailleurs sur la page.
    // -------------------------------------------------------------------
    function onPointerMove(e) {
      if (!dragging) return;
      dragRatio = ratioFromEvent(e);
      setVisual(dragRatio);
      showTooltip(dragRatio, e.touches ? e.touches[0].clientX : e.clientX);
      warmDuringDrag(dragRatio);
    }
    function onBarHover(e) {
      if (dragging) return; // le glissement est déjà géré par onPointerMove ci-dessus
      var r = ratioFromEvent(e);
      showTooltip(r, e.clientX);
      warmOnHover(r);
    }
    function commitSeek() {
      // Le vrai saut part maintenant : tout téléchargement spéculatif
      // encore en vol (survol/glissement précédent) est annulé sur-le-champ
      // pour libérer immédiatement sa connexion, plutôt que de laisser le
      // vrai saut faire la queue derrière sa propre optimisation — c'est
      // précisément ce qui provoquait les arrêts en navigation "au hasard".
      cancelPendingWarmFetch();
      // Le préchauffage de fond (initTimelinePrewarm, voir plus bas) peut
      // lui aussi occuper jusqu'à plusieurs connexions simultanées vers le
      // serveur (pool CONCURRENCY) — pile au mauvais moment, ça retarderait
      // d'autant la vraie requête de lecture qui part maintenant. On lui
      // demande donc de patienter brièvement (voir la vérification
      // correspondante dans runPool), le temps que cette requête réelle
      // parte et obtienne ses premiers octets.
      player.__pauseBackgroundWarmUntil = Date.now() + 1500;
      var dur = player.duration;
      if (dur && isFinite(dur)) {
        var target = dragRatio * dur;
        try {
          // fastSeek() (API standard) demande au navigateur de se caler sur
          // l'image-clé la PLUS PROCHE au lieu de décoder jusqu'à l'instant
          // exact : la reprise est quasi immédiate et sans à-coup, au lieu
          // du petit arrêt le temps du décodage de rattrapage. Repli
          // automatique sur currentTime là où fastSeek n'existe pas.
          if (typeof player.fastSeek === "function") player.fastSeek(target);
          else player.currentTime = target;
        } catch (e) {
          try { player.currentTime = target; } catch (e2) { /* ignore */ }
        }
      }
    }
    function onPointerUp() {
      if (!dragging) return;
      dragging = false;
      bar.classList.remove("dragging");
      commitSeek();
      hideTooltip();
    }

    // ---------------------------------------------------------------------
    // CORRECTIF ARRÊTS PERSISTANTS APRÈS UN GLISSEMENT DE LA TIMELINE
    // -----------------------------------------------------------------
    // Cause identifiée : "dragging" ne repassait à false QUE via un
    // véritable évènement "mouseup"/"touchend" reçu sur window. Or ces
    // évènements ne se déclenchent PAS dans plusieurs cas pourtant courants
    // en usage réel :
    //   - relâchement du bouton de la souris alors que le curseur a quitté
    //     la fenêtre du navigateur (fenêtre non maximisée, second écran,
    //     bascule vers une autre application pendant le geste) ;
    //   - "touchcancel" (au lieu de "touchend") sur tactile : un geste
    //     système de l'OS/du navigateur (notification, défilement repris
    //     ailleurs...) interrompt le toucher sans jamais envoyer "touchend".
    // Dans ces deux cas, "dragging" restait bloqué à true indéfiniment :
    // refresh() et visualTick() ignorent alors toute vraie position lue sur
    // le lecteur ("if (dragging) return;"), la barre reste figée exactement
    // là où le geste a été abandonné, ET aucun vrai saut (commitSeek) n'est
    // jamais envoyé — un blocage qui persistait jusqu'au rechargement complet
    // de la page. Deux filets de sécurité purement additifs, qui ne changent
    // rien au déroulé normal d'un glissement mené jusqu'à un vrai
    // mouseup/touchend :
    //   1) "blur" sur window : la fenêtre perd le focus (bouton relâché
    //      ailleurs, changement d'application...) pendant un glissement en
    //      cours -> on le termine proprement (mêmes actions qu'un relâchement
    //      normal) plutôt que de le laisser bloqué.
    //   2) "touchcancel" sur window : traité exactement comme "touchend".
    //   3) Dans onPointerMove lui-même : si un nouveau mouvement de souris
    //      arrive alors que "dragging" est vrai mais qu'aucun bouton n'est
    //      plus enfoncé (e.buttons === 0 — le bouton a été relâché ailleurs
    //      puis le curseur est revenu sur la page), on termine le glissement
    //      immédiatement au lieu d'attendre un mouseup qui ne viendra jamais.
    function forceEndDrag() {
      if (!dragging) return;
      onPointerUp();
    }

    bar.addEventListener("mousedown", onPointerDown, listenerOpts);
    bar.addEventListener("touchstart", onPointerDown, { passive: false, signal: signal || undefined });
    bar.addEventListener("mouseleave", function () { if (!dragging) hideTooltip(); }, listenerOpts);
    bar.addEventListener("mousemove", onBarHover, listenerOpts);
    window.addEventListener("mousemove", function (e) {
      if (dragging && typeof e.buttons === "number" && e.buttons === 0) {
        forceEndDrag();
        return;
      }
      onPointerMove(e);
    }, listenerOpts);
    window.addEventListener("touchmove", onPointerMove, { passive: false, signal: signal || undefined });
    window.addEventListener("mouseup", onPointerUp, listenerOpts);
    window.addEventListener("touchend", onPointerUp, listenerOpts);
    window.addEventListener("touchcancel", onPointerUp, listenerOpts);
    window.addEventListener("blur", forceEndDrag, listenerOpts);


    // Clavier : flèches gauche/droite = petit saut de 5s, comme la plupart
    // des lecteurs modernes (accessibilité, purement additif).
    bar.addEventListener("keydown", function (e) {
      var dur = player.duration;
      if (!dur || !isFinite(dur)) return;
      if (e.key === "ArrowRight") { player.currentTime = Math.min(dur, player.currentTime + 5); e.preventDefault(); }
      else if (e.key === "ArrowLeft") { player.currentTime = Math.max(0, player.currentTime - 5); e.preventDefault(); }
    }, listenerOpts);

    player.addEventListener("timeupdate", refresh, listenerOpts);
    player.addEventListener("loadedmetadata", refresh, listenerOpts);
    player.addEventListener("progress", updateBuffered, listenerOpts);
    // Resynchronisation de l'ancrage d'extrapolation (voir "5" ci-dessus) :
    // à chaque évènement qui reflète une vraie position du lecteur, pour
    // ne jamais laisser l'extrapolation dériver au-delà d'une fraction de
    // seconde. Purement additif, n'affecte aucun des écouteurs existants.
    player.addEventListener("timeupdate", resyncAnchor, listenerOpts);
    player.addEventListener("seeked", resyncAnchor, listenerOpts);
    player.addEventListener("play", resyncAnchor, listenerOpts);
    player.addEventListener("playing", resyncAnchor, listenerOpts);
    player.addEventListener("ratechange", resyncAnchor, listenerOpts);
    player.addEventListener("loadedmetadata", resyncAnchor, listenerOpts);
    refresh();

    if (signal) {
      signal.addEventListener("abort", function () {
        if (hoverWarmTimer) clearTimeout(hoverWarmTimer);
        cancelPendingWarmFetch();
        if (rafHandle && typeof cancelAnimationFrame === "function") {
          cancelAnimationFrame(rafHandle);
          rafHandle = null;
        }
      }, { once: true });
    }

    return wrap;
  }

  function initSmoothSeekBar(player, signal) {
    var wrap = buildSeekBar(player, signal);
    // Si un overlay bas (façon YouTube) est présent, la barre de
    // progression y prend place, tout en haut, juste au-dessus de la
    // rangée de boutons (précédent/suivant/aléatoire/volume...). Sinon
    // (structure de page différente), on retombe sur l'ancien
    // comportement additif d'origine : juste après le lecteur.
    var overlayBottom = player.parentNode && player.parentNode.querySelector
      ? player.parentNode.querySelector("#player-overlay-bottom")
      : null;
    if (overlayBottom) {
      overlayBottom.insertBefore(wrap, overlayBottom.firstChild);
      // Chronos (temps actuel / durée) sur la MÊME ligne que les boutons
      // de navigation, à gauche — évite qu'ils restent cachés derrière
      // ou confondus avec la rangée d'icônes (précédent/suivant/…).
      var times = wrap.querySelector(".smooth-seek-times");
      var nav = overlayBottom.querySelector("#player-nav-buttons");
      if (times && nav && !overlayBottom.querySelector(".player-controls-row")) {
        var row = document.createElement("div");
        row.className = "player-controls-row";
        times.classList.add("player-time-display");
        nav.parentNode.insertBefore(row, nav);
        row.appendChild(times);
        row.appendChild(nav);
      }
    } else {
      player.insertAdjacentElement("afterend", wrap);
    }
    if (signal) {
      signal.addEventListener("abort", function () {
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      }, { once: true });
    }
  }

  // =====================================================================
  // 4) PRÉCHAUFFAGE PROACTIF — FICHIER ENTIER, SANS LIMITE
  //    Jusqu'ici, une zone n'était réchauffée (voir warmVideoRegion) que
  //    lorsqu'on la survolait ou glissait dessus : un vrai gain, mais qui
  //    dépend d'être passé par là une première fois. Ici, dès que la vidéo
  //    est chargée, on couvre en tâche de fond la timeline dans son
  //    intégralité, sans aucune limite de taille ou de nombre de
  //    fragments : le fichier est découpé en morceaux (aucun plafond sur
  //    leur nombre, qui s'adapte naturellement à la taille du fichier),
  //    réchauffés en commençant par les plus proches de la lecture en
  //    cours, jusqu'à couverture complète — pour qu'un saut "au hasard"
  //    n'importe où dans la timeline bénéficie du même gain, même sans
  //    survol préalable à cet endroit précis.
  //
  //    Contrairement au survol (warmVideoRegion, deux volets), ce
  //    préchauffage de FOND n'utilise QUE le signal serveur (cache
  //    disque/OS, aucune donnée renvoyée au navigateur) — jamais de vraie
  //    requête Range de plusieurs Mo vers /stream. Raison : un navigateur
  //    n'ouvre qu'un nombre très limité de connexions simultanées vers la
  //    même origine (~6 en HTTP/1.1). De vrais téléchargements de fond
  //    occuperaient ces connexions et retarderaient d'autant une vraie
  //    requête de lecture au moment d'un saut — provoquant exactement
  //    l'arrêt que ce mécanisme cherche à supprimer. Le signal serveur,
  //    lui, répond quasi instantanément (202, sans corps) : la connexion
  //    se libère aussitôt, sans jamais entrer en concurrence avec la
  //    lecture réelle, quel que soit le nombre de fragments en vol.
  //
  //    Technologies utilisées pour couvrir le fichier vite ET sans jamais
  //    gêner la lecture :
  //      - AbortSignal partagé avec le reste du lecteur : tout
  //        réchauffage encore en vol est annulé net dès qu'on quitte la
  //        page (navigation instantanée via player-persist.js incluse) ;
  //      - pool de requêtes en parallèle (sûr ici car chacune est quasi
  //        gratuite, voir ci-dessus) pour couvrir le fichier entier bien
  //        plus vite ;
  //      - respect de l'économiseur de données du navigateur s'il est
  //        activé (Network Information API), quand disponible ;
  //      - suspendu tant que le lecteur est lui-même en train d'attendre
  //        des données (évite d'aggraver un arrêt en cours) ;
  //      - ne démarre qu'une fois la lecture initiale bien engagée, jamais
  //        avant (la toute première mise en route de la vidéo elle-même
  //        reste toujours prioritaire).
  // =====================================================================
  function initTimelinePrewarm(player, signal) {
    if (!player.dataset.streamUrl) return;
    // Codec lourd : jamais de warm (sinon lecture disque + CPU pendant le portail)
    if (player.getAttribute("data-heavy-gate") === "1"
        || (player.dataset && player.dataset.heavyGate === "1")
        || !!document.getElementById("heavy-codec-gate")) {
      if (player.dataset.autoCompat !== "1") return;
    }

    // Économiseur de données actif : on respecte le choix de l'utilisateur
    // et on renonce simplement au préchauffage de fond (la lecture normale
    // continue de fonctionner à l'identique).
    if (navigator.connection && navigator.connection.saveData) return;

    // CORRECTIF GEL AU DÉMARRAGE : ne démarre le préchauffage de fond qu'après
    // 15 s de lecture réelle. Avant ça, tout le disque/réseau est réservé
    // au flux vidéo (évite la course /warm + /stream observée dans les logs).
    var prewarmStartAt = 0;
    function markPlayProgress() {
      if (!prewarmStartAt && player.currentTime > 0.5) {
        prewarmStartAt = Date.now();
      }
    }
    player.addEventListener("timeupdate", markPlayProgress, signal ? { signal: signal } : undefined);

    var warmUrlBase = player.dataset.warmUrl || "";
    var fileSize = parseInt(player.dataset.fileSize, 10) || 0;
    if (!fileSize || fileSize <= 0 || !warmUrlBase) return;

    var CHUNK_BYTES = 6 * 1024 * 1024; // taille d'un fragment
    // CORRECTIF FLUIDITÉ : le préchauffage de fond reste utile pour absorber
    // un saut proche dans la timeline, mais chaque fragment provoque une
    // vraie lecture disque côté serveur, en concurrence directe avec le flux
    // vidéo en cours. On le rend donc volontairement discret : un seul
    // fragment à la fois, bien espacé, uniquement autour de la position de
    // lecture (voir NEIGHBORHOOD_BYTES), et jamais pendant que le lecteur a
    // besoin du disque pour lui-même.
    var CONCURRENCY = 1;               // un seul fragment à la fois : impact disque/CPU minimal
    var PACE_MS = 2500;                // délai minimal entre deux envois successifs
    var STALL_RECHECK_MS = 700;        // fréquence de re-vérification quand on patiente pour cause de mise en tampon
    var STEP_TIMEOUT_MS = 6000;        // filet de sécurité : ne jamais rester bloqué sur un fragment trop longtemps
    // Rayon autour de la position de lecture au-delà duquel on ne réchauffe
    // plus rien : un saut se fait presque toujours à proximité, et réchauffer
    // le reste du fichier ne servait qu'à occuper le disque.
    var NEIGHBORHOOD_BYTES = 160 * 1024 * 1024;
    // CORRECTIF RALENTISSEMENT SUR GROS FICHIERS (vidéos volumineuses/haute
    // résolution) : sur un petit fichier, ce balayage de fond se termine en
    // quelques secondes et n'a donc aucun impact perceptible. Mais comme il
    // n'était borné que par la taille du fichier ("aucune limite sur leur
    // nombre" ci-dessus), sur un gros fichier (4K, haut bitrate...) il
    // continuait de tourner en arrière-plan pendant TOUTE la durée du
    // visionnage, chaque fragment déclenchant une vraie lecture disque côté
    // serveur (voir _warm_stream_region dans app.py) qui entre en
    // concurrence avec la lecture disque du flux réel en cours — c'était la
    // cause exacte du ralentissement observé, spécifiquement sur les
    // vidéos volumineuses. On plafonne donc désormais le volume total
    // réchauffé par ce balayage de fond, ce mécanisme restant 100%
    // best-effort (aucune conséquence fonctionnelle : les zones non
    // couvertes se réchauffent normalement, à froid, si on finit par y
    // sauter). Le plafond est PROPORTIONNEL à la taille du fichier plutôt
    // que fixe : une vidéo "moyennement grosse" garde une couverture
    // généreuse (elle profite pleinement du préchauffage), tandis qu'une
    // vidéo réellement énorme (celle où la contention disque est la plus
    // sensible et la plus longue à installer) est traitée avec davantage
    // de prudence — sans jamais dépasser un maximum absolu.
    // CORRECTIF CPU DÉFINITIF : ce balayage de fond couvrait jusqu'à
    // plusieurs Go par vidéo, soit des centaines de lectures disque de
    // plusieurs Mo côté serveur PENDANT la lecture — cause directe du CPU
    // et du disque saturés, et donc des saccades. On ne réchauffe plus que
    // le VOISINAGE immédiat de la position de lecture (là où un saut a une
    // vraie probabilité d'arriver), pour un coût désormais négligeable.
    var MAX_PREWARM_TOTAL_BYTES = 24 * 1024 * 1024; // 24 Mo max (était plus large)

    var cancelled = false;
    var started = false;
    var totalWarmed = 0;
    // Marge tampon minimale devant la position de lecture avant d'autoriser
    // le moindre fragment de préchauffage. Relevée : tant que le lecteur n'a
    // pas une avance confortable, le disque doit servir EXCLUSIVEMENT au flux
    // en cours — c'est ce qui supprime les micro-arrêts d'image.
    var MIN_BUFFER_AHEAD_SECONDS = 30;

    function bufferedAheadSeconds() {
      try {
        var buf = player.buffered;
        var t = player.currentTime || 0;
        for (var i = 0; i < buf.length; i++) {
          if (buf.start(i) <= t && t <= buf.end(i)) return buf.end(i) - t;
        }
      } catch (e) { /* ignore, on considère alors la marge inconnue */ }
      return null; // marge inconnue (ex. tout début de lecture) : ne bloque pas le préchauffage
    }

    function chunkOffsetsFor(size) {
      // Découpe l'intégralité du fichier en fragments contigus, sans
      // aucune borne sur leur nombre : un fichier plus gros donne
      // simplement plus de fragments, jamais une couverture partielle
      // (le plafond global ci-dessus s'applique séparément, au moment de
      // l'envoi effectif — voir runPool).
      var offsets = [];
      for (var off = 0; off < size; off += CHUNK_BYTES) offsets.push(off);
      return offsets;
    }

    function orderFromCurrent(offsets, size, dur, current) {
      // Seuls les fragments proches de la position de lecture actuelle sont
      // retenus (voir NEIGHBORHOOD_BYTES), les plus proches d'abord : ce sont
      // les seuls qu'un saut a une vraie chance de viser. Les zones
      // lointaines ne sont plus réchauffées du tout — elles se chargeront
      // normalement, à froid, si on finit par y sauter.
      var currentOffset = (dur > 0) ? (current / dur) * size : 0;
      return offsets
        .filter(function (o) { return Math.abs(o - currentOffset) <= NEIGHBORHOOD_BYTES; })
        .sort(function (a, b) {
          return Math.abs(a - currentOffset) - Math.abs(b - currentOffset);
        });
    }

    function warmChunk(offset) {
      var end = Math.min(fileSize - 1, offset + CHUNK_BYTES - 1);
      // IMPORTANT : uniquement le signal serveur (cache disque/OS, AUCUNE
      // donnée renvoyée au navigateur), jamais une vraie requête Range de
      // plusieurs Mo vers /stream ici. Un navigateur n'ouvre qu'un nombre
      // TRÈS limité de connexions simultanées vers la même origine (~6 en
      // HTTP/1.1) : si ce préchauffage de fond en occupe plusieurs avec de
      // gros téléchargements, un vrai saut dans la timeline peut devoir
      // ATTENDRE qu'une connexion se libère avant même de démarrer — ce qui
      // provoquerait exactement l'arrêt qu'on cherche à éviter. Le signal
      // serveur, lui, répond immédiatement (202, sans corps) et libère la
      // connexion presque aussitôt : aucune concurrence avec la vraie
      // lecture, quel que soit le nombre de fragments en vol.
      if (!warmUrlBase) return Promise.resolve();
      try {
        var t = (fileSize > 0 && player.duration) ? (offset / fileSize) * player.duration : 0;
        return fetch(warmUrlBase + "?t=" + t.toFixed(1) + "&span=" + (end - offset + 1),
                      { method: "GET", cache: "no-store", keepalive: true })
          .catch(function () { /* best-effort : un échec ici n'a aucune conséquence fonctionnelle */ });
      } catch (e) {
        return Promise.resolve();
      }
    }

    function runPool(queue) {
      var idx = 0;

      function next() {
        if (cancelled || idx >= queue.length) return;
        // Enveloppe totale atteinte (voir MAX_PREWARM_TOTAL_BYTES) : le
        // reste du fichier ne sera pas réchauffé par ce balayage de fond,
        // sans aucune conséquence sur la lecture elle-même — seulement une
        // optimisation en moins pour les zones lointaines pas encore
        // visitées, exactement comme si elles n'avaient jamais été
        // réchauffées du tout.
        if (totalWarmed >= MAX_PREWARM_TOTAL_BYTES) return;
        // Ne jamais ajouter de charge disque/réseau pendant que le lecteur
        // attend déjà des données : on patiente plutôt que d'aggraver un
        // arrêt en cours (le préchauffage reprendra dès que ça repart).
        // Attendre 15 s de lecture engagée avant tout warm de fond
        if (!prewarmStartAt || (Date.now() - prewarmStartAt) < 15000) {
          setTimeout(next, 2000);
          return;
        }
        if (player.readyState < 2 /* HAVE_CURRENT_DATA */ && !player.paused && !player.ended) {
          setTimeout(next, STALL_RECHECK_MS);
          return;
        }
        // Marge tampon déjà mince devant la position de lecture actuelle
        // (voir MIN_BUFFER_AHEAD_SECONDS ci-dessus) : on patiente, comme
        // pour le cas de gel ci-dessus, plutôt que d'ajouter de la charge
        // disque pendant que le flux réel a justement besoin de toute la
        // bande passante disponible pour reconstituer son avance. Une
        // marge inconnue (null, ex. tout début de lecture) ne bloque
        // jamais ce préchauffage.
        var aheadSec = bufferedAheadSeconds();
        if (aheadSec !== null && aheadSec < MIN_BUFFER_AHEAD_SECONDS && !player.paused && !player.ended) {
          setTimeout(next, STALL_RECHECK_MS);
          return;
        }
        // Un vrai saut dans la timeline vient de partir (voir commitSeek
        // dans buildSeekBar, ci-dessus) : on laisse le champ libre sur le
        // pool de connexions du navigateur le temps que cette requête
        // réelle, prioritaire, obtienne ses premiers octets — plutôt que
        // de continuer à en occuper une partie avec ce préchauffage de
        // fond, ce qui retarderait d'autant la reprise de lecture.
        if (player.__pauseBackgroundWarmUntil && Date.now() < player.__pauseBackgroundWarmUntil) {
          setTimeout(next, STALL_RECHECK_MS);
          return;
        }
        var offset = queue[idx];
        idx++;
        totalWarmed += Math.min(CHUNK_BYTES, Math.max(0, fileSize - offset));
        var settled = false;
        function advance() {
          if (settled) return;
          settled = true;
          // PACE_MS : espace le prochain envoi de ce worker plutôt que de
          // l'enchaîner immédiatement (voir note CONCURRENCY/PACE_MS
          // ci-dessus) — évite la rafale de requêtes quasi simultanées à
          // l'ouverture d'une vidéo.
          setTimeout(next, PACE_MS);
        }
        warmChunk(offset).then(advance).catch(advance);
        setTimeout(advance, STEP_TIMEOUT_MS);
      }

      // Pool de quelques fragments menés de front, pour couvrir le fichier
      // entier plus rapidement sans jamais saturer la connexion.
      for (var i = 0; i < CONCURRENCY; i++) next();
    }

    function start() {
      if (started || cancelled) return;
      var dur = player.duration;
      started = true;
      var ordered = orderFromCurrent(chunkOffsetsFor(fileSize), fileSize, dur || 0, player.currentTime || 0);
      var idle = window.requestIdleCallback || function (fn) { setTimeout(fn, 0); };
      idle(function () { if (!cancelled) runPool(ordered); });
    }

    var kicked = false;
    function kickOnce() {
      if (kicked) return;
      kicked = true;
      start();
    }

    // Ne démarre le préchauffage de fond QUE 15 s après le premier
    // "playing" réel — jamais sur canplay (trop tôt, concurrence /stream).
    function scheduleAfterPlay() {
      setTimeout(kickOnce, 15000);
    }
    if (!player.paused && player.currentTime > 0.25) {
      scheduleAfterPlay();
    } else {
      var opts = signal ? { once: true, signal: signal } : { once: true };
      player.addEventListener("playing", scheduleAfterPlay, opts);
    }

    if (signal) {
      signal.addEventListener("abort", function () { cancelled = true; }, { once: true });
    }
  }

  // =====================================================================
  // Point d'entrée, appelé depuis main.js (initDynamicPage) à chaque
  // (ré)initialisation de la page vidéo, y compris après une navigation
  // instantanée gérée par player-persist.js.
  // =====================================================================

  // Soft-nav : annule tout warm timeline en cours
  document.addEventListener("mediatheque:abort-preload", function () {
    try {
      if (window.__timelineWarmAbort) window.__timelineWarmAbort.abort();
    } catch (e) {}
  });

  window.__initSmoothPlayer = function (root, signal) {
    root = root || document;
    var player = root.querySelector("#main-player");
    if (!player) return;
    var heavy = player.getAttribute("data-heavy-gate") === "1"
      || (player.dataset && player.dataset.heavyGate === "1")
      || !!document.getElementById("heavy-codec-gate");
    // Codec lourd sans version compatible : pas de préchauffage, pas de
    // watchdog de recovery (évite load/play fantômes et charge CPU).
    if (heavy && player.dataset.autoCompat !== "1") {
      initSmoothSeekBar(player, signal); // barre UI seulement, sans réseau forcé
      return;
    }
    initPlaybackWatchdog(player, signal);
    initSmoothSeekBar(player, signal);
    initTimelinePrewarm(player, signal);
  };
})();
