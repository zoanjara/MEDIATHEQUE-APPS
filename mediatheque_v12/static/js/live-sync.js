/*
 * live-sync.js — Mise à jour en temps réel
 * =====================================================================
 * Se connecte au flux /api/live (Server-Sent Events). Dès qu'une donnée
 * change n'importe où dans l'application (édition de métadonnées, note,
 * tags, catégories, playlists, suppression, paramètres, scan qui détecte
 * de nouvelles vidéos...), la page actuellement affichée se met à jour
 * d'elle-même — jamais besoin de retaper/recharger l'adresse dans le
 * navigateur.
 *
 * Le rafraîchissement réutilise la navigation douce déjà en place
 * (player-persist.js) quand elle est disponible : le contenu est
 * remplacé sans rechargement complet, et une lecture vidéo en cours
 * n'est jamais coupée. Si cette navigation douce n'est pas disponible
 * pour une raison quelconque, on retombe sur un rechargement classique.
 *
 * Prudence : on ne rafraîchit jamais pendant qu'une saisie est en cours
 * (champ de formulaire actif, popup d'édition ouverte) — le prochain
 * signal reçu déclenchera le rafraîchissement une fois la saisie
 * terminée.
 */
(function () {
  "use strict";

  if (!("EventSource" in window)) return; // navigateur trop ancien : comportement d'origine intact

  var refreshTimer = null;
  var pendingRefresh = false;
  var lastScanning = false;
  // Pendant qu'un scan tourne, on espace nettement les rafraîchissements
  // automatiques (page complète re-générée à chaque signal) : avec
  // potentiellement des dizaines de nouvelles vidéos détectées coup sur
  // coup, un rafraîchissement trop fréquent ralentissait l'appli.
  // 800 ms hors scan : laisse finir les écritures légères (Disques actifs)
  // avant un soft-nav qui régénère toute la page — réduit le gel perçu.
  var REFRESH_DEBOUNCE_MS = 800;
  var REFRESH_DEBOUNCE_MS_SCANNING = 4000;

  function isUserBusy() {
    var el = document.activeElement;
    if (el) {
      var tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable) {
        return true;
      }
    }
    // Popups d'édition (métadonnées, tags...) : voir templates/base.html
    // et static/js/main.js, qui basculent la classe "open" sur ces
    // éléments ".modal-overlay" pendant qu'elles sont affichées.
    if (document.querySelector(".modal-overlay.open")) return true;
    // Panneau "✎ Modifier les métadonnées" de la page vidéo dédiée (et
    // panneaux d'édition catégorie/playlist) : contrairement aux popups
    // ci-dessus, ce panneau reste affiché en permanence dans la page (ce
    // n'est pas une ".modal-overlay"), donc le focus clavier seul ne
    // suffit pas à détecter une saisie en cours (ex. après avoir coché
    // une case ou choisi une note, le focus n'est plus dans un champ).
    // voir static/js/main.js (markLiveFormDirty / clearLiveFormDirty) :
    // tant qu'une modification n'a pas été confirmée enregistrée par le
    // serveur, on ne rafraîchit jamais, pour ne jamais l'effacer en
    // silence.
    if (window.__hasDirtyEditForm && window.__hasDirtyEditForm()) return true;
    // CORRECTIF : le panneau "✎ Modifier les métadonnées" de la page vidéo
    // dédiée MET LA VIDÉO EN PAUSE dès qu'on l'ouvre (voir
    // pauseForMetadataEdit dans static/js/main.js), ce qui désactive du
    // même coup la protection "vidéo en cours de lecture" ci-dessous. Tant
    // que ce panneau est déverrouillé (ouvert), on ne dépend donc plus QUE
    // du suivi "dirty" (data-live-dirty) — fragile, car il suffit d'un seul
    // widget qui oublie de le poser (ça a été le cas du retrait de tag,
    // voir main.js) pour qu'un rafraîchissement automatique remplace tout
    // le panneau — et le bas du lecteur vidéo autour — EN PLEIN MILIEU
    // d'une modification non enregistrée : la modification semble "ne pas
    // marcher", et les boutons du lecteur, recréés à cet instant précis,
    // peuvent sembler ne plus répondre au clic suivant. On traite donc le
    // panneau ouvert comme "occupé" par défaut, quel que soit l'état
    // "dirty" — bien plus sûr qu'une liste de sélecteurs à tenir à jour.
    var openEditForm = document.querySelector("#video-edit-form:not(.edit-form-locked)");
    if (openEditForm) return true;
    // FLUIDITÉ : une vidéo est en cours de lecture. Un rafraîchissement
    // automatique re-génère toute la page côté serveur puis remplace le DOM
    // côté navigateur — un pic de travail (SQL, HTML, mise en page, images
    // de la grille) qui tombe pile pendant le décodage vidéo et provoque
    // exactement les micro-arrêts d'image constatés, en particulier pendant
    // un scan (un signal toutes les quelques secondes). On reporte donc
    // simplement le rafraîchissement à la pause / la fin de la lecture : la
    // page se met bien à jour, jamais au détriment de l'image.
    var vid = document.getElementById("main-player");
    if (vid && !vid.paused && !vid.ended) return true;
    // Soft-nav en cours : ne jamais recouvrir le DOM au milieu d'un switch
    if (window.__softNavBusy) return true;
    return false;
  }

  function refreshNow() {
    if (location.pathname === "/settings") {
      // La page Paramètres affiche déjà elle-même la progression du scan
      // en temps réel (voir templates/settings.html) : la rafraîchir ici
      // remplacerait ce suivi en cours d'affichage sans le relancer.
      return;
    }
    if (document.hidden) {
      pendingRefresh = true; // onglet en arrière-plan : on rafraîchira à son retour au premier plan
      return;
    }
    if (isUserBusy()) {
      pendingRefresh = true;
      return;
    }
    pendingRefresh = false;
    var url = location.pathname + location.search;
    if (window.__softNavigate) {
      // preserveSidebar : ce rafraîchissement recharge la MÊME page en
      // arrière-plan (pas une navigation choisie par l'utilisateur) — le
      // panneau latéral, s'il était ouvert, ne doit pas se refermer tout
      // seul pendant qu'on y modifie quelque chose.
      window.__softNavigate(url, { pushState: false, preserveSidebar: true });
    } else {
      location.reload();
    }
  }

  function scheduleRefresh(scanning) {
    clearTimeout(refreshTimer);
    var delay = scanning ? REFRESH_DEBOUNCE_MS_SCANNING : REFRESH_DEBOUNCE_MS;
    // Page vidéo : debounce plus long (évite soft-nav concurrent pendant lecture)
    if (/^\/video\/\d+/.test(location.pathname)) {
      delay = Math.max(delay, scanning ? 6000 : 2000);
    }
    refreshTimer = setTimeout(refreshNow, delay);
  }

  // ---------------------------------------------------------------------
  // CORRECTIF "clic sur une vidéo lent à démarrer juste après un scan" :
  // un rafraîchissement automatique programmé ci-dessus (souvent le plus
  // volumineux juste après la fin d'un scan — bilan final, potentiellement
  // des dizaines de nouvelles vidéos) peut tomber PILE au moment où
  // l'utilisateur clique sur une case vidéo. Annuler la requête côté
  // navigateur (voir pageAbort dans static/js/player-persist.js) ne
  // suffit pas : le serveur Flask, lui, a déjà commencé à régénérer toute
  // la page (requêtes SQL, rendu Jinja2) et continue de tourner dessus
  // même après l'abandon côté client — or Python (GIL) exécute ce travail
  // de façon sérialisée avec celui de la VRAIE page vidéo demandée juste
  // après, qui doit donc attendre son tour. On annule donc ce
  // rafraîchissement PROGRAMMÉ dès qu'un vrai clic de navigation a lieu
  // (avant même que sa requête ne soit envoyée), pour qu'il ne parte
  // jamais côté serveur et ne puisse plus jamais retarder le clic.
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    if (a.target && a.target !== "" && a.target !== "_self") return;
    if (a.hasAttribute("download")) return;
    var url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    if (url.origin !== location.origin) return;
    clearTimeout(refreshTimer);
    pendingRefresh = false;
  }, true);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && pendingRefresh) scheduleRefresh(false);
  });

  // ---------------------------------------------------------------------
  // Filet de sécurité : un signal de changement (ex. enregistrement des
  // métadonnées d'une vidéo depuis la popup "✎ Modifier les métadonnées")
  // peut arriver PENDANT que la popup est encore techniquement ouverte
  // (elle ne se ferme, côté client, qu'une fois la réponse du serveur
  // reçue — la notification temps réel, elle, voyage sur une connexion
  // séparée et peut arriver avant). Dans ce cas, isUserBusy() renvoyait
  // "occupé", pendingRefresh restait à true, et RIEN ne le reconsidérait
  // ensuite (seul un changement d'onglet le faisait) : le panneau ☰ (et
  // le reste de la page) ne se mettait alors jamais à jour tout seul,
  // même après la fermeture de la popup — c'est le bug corrigé ici. On
  // revérifie donc régulièrement, tant qu'un rafraîchissement reste en
  // attente, si la voie est libre pour le déclencher.
  setInterval(function () {
    if (pendingRefresh && !document.hidden && !isUserBusy()) {
      pendingRefresh = false;
      scheduleRefresh(lastScanning);
    }
  }, 700);

  function connect() {
    var source = new EventSource("/api/live");
    var lastVersion = null;

    source.onmessage = function (evt) {
      var payload = null;
      try {
        payload = JSON.parse(evt.data);
      } catch (e) {
        payload = null; // ancien format brut (numéro de version seul) : repli ci-dessous
      }
      var version = payload && payload.v !== undefined ? payload.v : evt.data;
      var scanning = !!(payload && payload.scanning);
      lastScanning = scanning;
      if (lastVersion === null) {
        lastVersion = version; // 1er message : état initial, rien à rafraîchir
        return;
      }
      if (version !== lastVersion) {
        lastVersion = version;
        scheduleRefresh(scanning);
      }
    };

    source.onerror = function () {
      source.close();
      setTimeout(connect, 3000); // reconnexion automatique (réseau coupé, serveur relancé...)
    };
  }

  connect();
})();
