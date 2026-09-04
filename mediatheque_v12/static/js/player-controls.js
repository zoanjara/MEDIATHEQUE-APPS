/*
 * player-controls.js — Contrôle de volume personnalisé (#main-player)
 * =====================================================================
 * Module 100% additif : n'ajoute qu'un contrôle de volume personnalisé
 * (icône + curseur), placé dans la barre du bas façon YouTube, à côté de
 * Précédent/Suivant/Aléatoire. Le volume natif du navigateur (bouton +
 * curseur intégrés au lecteur) est masqué par CSS (voir style.css,
 * pseudo-éléments ::-webkit-media-controls-*) uniquement sur les
 * navigateurs qui le permettent ; ce module ne touche à aucun autre
 * contrôle natif (lecture, plein écran, PiP...), ni au comportement
 * existant du lecteur (autoplay, navigation instantanée, aléatoire...).
 *
 * Le volume choisi est mémorisé (localStorage) pour être ré-appliqué
 * automatiquement à la prochaine vidéo/ouverture, comme sur YouTube.
 * ---------------------------------------------------------------------
 */
(function () {
  "use strict";

  var VOLUME_KEY = "mediatheque_player_volume";
  var MUTED_KEY = "mediatheque_player_muted";

  function iconFor(volume, muted) {
    if (muted || volume <= 0) return "🔇";
    if (volume < 0.5) return "🔈";
    return "🔊";
  }

  window.__initPlayerVolume = function (root, signal) {
    root = root || document;
    var player = root.querySelector("#main-player");
    var wrap = root.querySelector("#player-volume");
    var btn = root.querySelector("#player-volume-btn");
    var slider = root.querySelector("#player-volume-slider");
    if (!player || !wrap || !btn || !slider) return;

    var listenerOpts = signal ? { signal } : undefined;
    var lastNonZero = 1;

    // Reprend le dernier volume choisi (si mémorisé), sans jamais forcer
    // une valeur si le lecteur en a déjà une différente de sa valeur par
    // défaut (respecte tout réglage déjà en place).
    try {
      var savedVol = parseFloat(localStorage.getItem(VOLUME_KEY));
      if (!isNaN(savedVol) && savedVol >= 0 && savedVol <= 1) {
        player.volume = savedVol;
        if (savedVol > 0) lastNonZero = savedVol;
      }
      var savedMuted = localStorage.getItem(MUTED_KEY);
      if (savedMuted === "1") player.muted = true;
    } catch (e) { /* stockage indisponible : on garde les valeurs par défaut */ }

    function refresh() {
      var vol = isFinite(player.volume) ? player.volume : 1;
      slider.value = String(player.muted ? 0 : vol);
      btn.textContent = iconFor(vol, player.muted);
      wrap.classList.toggle("player-volume-muted", player.muted || vol <= 0);
      if (vol > 0) lastNonZero = vol;
    }

    slider.addEventListener("input", function () {
      var v = parseFloat(slider.value);
      if (isNaN(v)) return;
      player.volume = v;
      player.muted = v <= 0;
      try {
        localStorage.setItem(VOLUME_KEY, String(v));
        localStorage.setItem(MUTED_KEY, player.muted ? "1" : "0");
      } catch (e) { /* tant pis, non mémorisé */ }
    }, listenerOpts);

    btn.addEventListener("click", function () {
      if (player.muted || player.volume <= 0) {
        player.muted = false;
        player.volume = lastNonZero > 0 ? lastNonZero : 1;
      } else {
        lastNonZero = player.volume > 0 ? player.volume : lastNonZero;
        player.muted = true;
      }
      try {
        localStorage.setItem(VOLUME_KEY, String(player.volume));
        localStorage.setItem(MUTED_KEY, player.muted ? "1" : "0");
      } catch (e) { /* tant pis, non mémorisé */ }
    }, listenerOpts);

    player.addEventListener("volumechange", refresh, listenerOpts);
    refresh();
  };
})();
