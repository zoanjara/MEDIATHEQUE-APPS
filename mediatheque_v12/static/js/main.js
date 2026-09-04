// Échappement HTML (défini tôt : utilisé par les chips playlists et le reste).
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

// Ajoute le jeton CSRF à un FormData / à des headers fetch.
function csrfHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (typeof window.__withCsrfHeaders === "function") {
    return window.__withCsrfHeaders(h);
  }
  return h;
}
function csrfFormData(data) {
  if (typeof window.__appendCsrf === "function") return window.__appendCsrf(data);
  return data;
}

// ============ Champ "Playlists" de "✎ Modifier les métadonnées" (popup
// rapide + page vidéo) : "Ajouter à une playlist principale" est une liste
// de cases à cocher réelles. "Ajouter à un sous-playlist" n'affiche QUE
// les groupes des playlists principales cochées ci-dessus (une case
// cochée = un groupe visible ; plusieurs cases cochées = autant de
// groupes visibles) — le groupe d'une playlist principale non cochée
// disparaît toujours, même si une de ses sous-playlists est déjà cochée
// (elle reste alors visible/retirable via sa puce en haut du champ).
// Chaque groupe visible propose les cases à cocher pour ses
// sous-playlists existantes, plus un champ "Créer et ajouter" pour en
// créer une nouvelle directement sous cette playlist, sans avoir à
// resélectionner le parent. ============
function syncPlaylistField(field) {
  const chipsBox = field.querySelector(".js-playlist-chips");
  if (!chipsBox) return;
  const checked = Array.from(field.querySelectorAll("input[name='playlists']:checked"));
  chipsBox.innerHTML = "";
  if (checked.length === 0) {
    const empty = document.createElement("span");
    empty.className = "playlist-chips-empty muted";
    empty.textContent = "Aucune playlist sélectionnée pour le moment";
    chipsBox.appendChild(empty);
  } else {
    checked.forEach(cb => {
      const label = cb.closest("label");
      const isSub = label.classList.contains("playlist-checkbox-sub");
      const name = cb.dataset.name || "";
      const chip = document.createElement("span");
      chip.className = "playlist-chip" + (isSub ? " playlist-chip-sub" : "");
      // Construction DOM (pas innerHTML avec le nom) : anti-XSS.
      if (isSub) chip.appendChild(document.createTextNode("↳ "));
      chip.appendChild(document.createTextNode(name + " "));
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "playlist-chip-remove";
      btn.dataset.value = cb.value;
      btn.title = "Retirer";
      btn.textContent = "✕";
      chip.appendChild(btn);
      chipsBox.appendChild(chip);
    });
  }
  updateSubPlaylistGroups(field);
}

// Un groupe de sous-playlists n'est visible QUE si sa playlist principale
// à lui est cochée ci-dessus (une ou plusieurs cases cochées = autant de
// groupes visibles) : aucune exception. Le groupe d'une playlist
// principale non cochée reste masqué même si une de ses sous-playlists
// est déjà cochée pour cette vidéo (elle reste visible/retirable via sa
// puce en haut du champ). L'indice "impossible" n'apparaît que si aucun
// groupe n'est visible.
function updateSubPlaylistGroups(field) {
  const subRow = field.querySelector(".js-sub-add-row");
  if (!subRow) return;

  let anyVisible = false;
  field.querySelectorAll(".js-playlist-sub-group").forEach(group => {
    const parentId = group.dataset.parentId;
    const topCb = field.querySelector(`.js-top-playlist-cb[value="${parentId}"]`);
    const shouldShow = !!(topCb && topCb.checked);
    group.hidden = !shouldShow;
    if (shouldShow) {
      anyVisible = true;
      group.open = true;
    }
  });

  const hint = subRow.querySelector(".js-sub-lock-hint");
  if (hint) hint.hidden = anyVisible;
  subRow.classList.toggle("playlist-add-row-disabled", !anyVisible);
}

function initPlaylistField(field) {
  if (field.dataset.playlistFieldReady) return;
  field.dataset.playlistFieldReady = "1";

  syncPlaylistField(field);

  field.addEventListener("change", (e) => {
    if (e.target.matches("input[name='playlists']")) syncPlaylistField(field);
  });

  field.addEventListener("click", (e) => {
    const chipRemove = e.target.closest(".playlist-chip-remove");
    if (chipRemove) {
      const cb = field.querySelector(`input[name='playlists'][value="${chipRemove.dataset.value}"]`);
      if (cb) { cb.checked = false; syncPlaylistField(field); }
      return;
    }

    const createTopBtn = e.target.closest(".js-new-top-create-btn");
    if (createTopBtn) { createTopPlaylistInline(field, createTopBtn); return; }

    const createSubBtn = e.target.closest(".js-new-sub-create-btn");
    if (createSubBtn) { createSubPlaylistInline(field, createSubBtn); }
  });
}

function createTopPlaylistInline(field, btn) {
  const row = btn.closest(".playlist-new-top-inline") || btn.parentElement;
  const nameInput = row.querySelector(".js-new-top-name");
  const name = nameInput ? nameInput.value.trim() : "";
  if (!name) { if (nameInput) nameInput.focus(); return; }

  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "...";

  const data = new FormData();
  data.append("name", name);

  fetch("/api/playlist/quick_create", { method: "POST", body: data, cache: "no-store" })
    .then(r => r.json().then(body => ({ status: r.status, body })))
    .then(({ status, body }) => {
      if (status !== 200 || !body.ok) throw new Error(body.error || "erreur inconnue");
      addTopPlaylistEverywhere(body.id, body.name, field);
      nameInput.value = "";
    })
    .catch((err) => {
      alert("Impossible de créer la playlist : " + err.message);
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = originalLabel;
    });
}

function createSubPlaylistInline(field, btn) {
  const group = btn.closest(".js-playlist-sub-group");
  const parentId = group.dataset.parentId;
  const parentName = group.dataset.parentName;
  const nameInput = group.querySelector(".js-new-sub-name");
  const dateInput = group.querySelector(".js-new-sub-date");
  const name = nameInput.value.trim();
  const manualDate = dateInput ? dateInput.value : "";
  if (!name) { nameInput.focus(); return; }
  if (!manualDate) { if (dateInput) dateInput.focus(); return; }

  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "...";

  const data = new FormData();
  data.append("name", name);
  data.append("parent_id", parentId);
  data.append("manual_creation_date", manualDate);

  fetch("/api/playlist/quick_create", { method: "POST", body: data, cache: "no-store" })
    .then(r => r.json().then(body => ({ status: r.status, body })))
    .then(({ status, body }) => {
      if (status !== 200 || !body.ok) throw new Error(body.error || "erreur inconnue");

      // Ajoute la nouvelle sous-playlist, cochée ICI, dans CE champ, mais
      // aussi (non cochée) dans tous les autres champs "Playlists" déjà
      // présents dans la page (ex. la popup rapide "✎ Modifier les
      // métadonnées", toujours présente dans le DOM via templates/base.html
      // même fermée) : sans quoi elle resterait invisible ailleurs jusqu'au
      // prochain rechargement complet de la page.
      addSubPlaylistEverywhere(parentId, parentName, body.id, body.name, field);

      nameInput.value = "";
      if (dateInput) dateInput.value = "";
    })
    .catch((err) => {
      alert("Impossible de créer la sous-playlist : " + err.message);
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = originalLabel;
    });
}

// ============ Répercussion instantanée des catégories/playlists partout où
// elles sont affichées =====================================================
// Chaque catégorie/playlist peut être renommée, créée ou supprimée depuis
// PLUSIEURS endroits (panneau latéral gauche, page "Organiser", en-tête de
// sa page dédiée...). applyEntityRename/applyEntityRemoval et la création
// rapide du panneau latéral géraient déjà le panneau latéral lui-même, les
// cartes "Organiser", l'en-tête de page dédiée et la liste des tags — mais
// pas le champ "Catégorie" (texte + liste déroulante) ni les cases à cocher
// "Playlists" du panneau "✎ Modifier les métadonnées" (page vidéo ET popup
// rapide), qui restaient donc affichés avec l'ancien nom, ou n'affichaient
// pas du tout un élément créé/supprimé pendant que la page était ouverte.
// Les fonctions ci-dessous comblent précisément ce manque, en réutilisant le
// même principe que le reste du fichier : mise à jour directe du DOM à
// partir de l'action effectuée, sans jamais recharger la page ni attendre le
// prochain signal /api/live.

// ============ Protection anti-écrasement des formulaires "✎ Modifier les
// métadonnées" (page vidéo + popup rapide) et des panneaux d'édition
// catégorie/playlist, face au rafraîchissement temps réel (live-sync.js)
// =====================================================================
// Bug corrigé : live-sync.js rafraîchit automatiquement le contenu de la
// page dès qu'une donnée change n'importe où dans l'appli (y compris une
// action lancée depuis CE formulaire lui-même, ex. créer une sous-playlist
// inline ci-dessous), sauf s'il détecte l'utilisateur "occupé". Il ne
// considérait occupé que le focus clavier sur un champ, ou une popup
// ouverte — or le panneau de métadonnées de la page vidéo dédiée n'est pas
// une popup : il reste affiché en permanence dans la page. Un
// rafraîchissement déclenché pendant qu'on le remplit (après avoir quitté
// un champ des yeux, coché une case, choisi une note...) remplaçait alors
// silencieusement tout son contenu par ce qui était encore enregistré en
// base, effaçant sans aucun message toute modification pas encore
// confirmée par "✓ Enregistrer" — donnant l'impression que l'enregistrement
// ne fonctionnait pas.
// On marque ici ces formulaires comme "modifiés" (data-live-dirty="1") dès
// la moindre interaction, et on ne les remet "propres" qu'une fois
// l'enregistrement confirmé réussi par le serveur (ou au chargement de
// données fraîches côté popup rapide) : live-sync.js consulte ce marqueur
// avant tout rafraîchissement automatique.
const LIVE_GUARDED_FORMS_SELECTOR = "#video-edit-form, #quick-edit-form, .org-edit-form";

function markLiveFormDirty(el) {
  const form = el && el.closest && el.closest(LIVE_GUARDED_FORMS_SELECTOR);
  if (form) form.dataset.liveDirty = "1";
}

function clearLiveFormDirty(form) {
  if (form) delete form.dataset.liveDirty;
}

// Saisie de texte, changement de date/nombre, case cochée/décochée
// (catégorie, tags, note cachée, playlists...) : couvre la quasi-totalité
// des interactions natives du formulaire.
document.addEventListener("input", (e) => markLiveFormDirty(e.target));
document.addEventListener("change", (e) => markLiveFormDirty(e.target));
// Note par étoiles, retrait d'une puce playlist, retrait d'une puce tag et
// choix d'une suggestion de tag : ces widgets modifient un champ caché (ou
// ajoutent/retirent une puce) directement en JS, sans passer par un
// événement natif "input"/"change", donc interceptés ici séparément.
// CORRECTIF (double) :
//  1) ".js-tag-chip-remove" et ".tag-suggest-item" manquaient à cette liste
//     — retirer un tag (ou en choisir un dans les suggestions) ne marquait
//     donc PAS le formulaire "modifié".
//  2) Même une fois ajoutés, ça ne suffisait pas : les gestionnaires qui
//     retirent une puce (tag OU playlist, voir initTagField/initPlaylistField
//     plus haut) le font en reconstruisant/retirant l'élément cliqué AVANT
//     que cet écouteur-ci (posé sur "document", phase de bouillonnement) ne
//     s'exécute — l'élément est alors déjà détaché du document, et
//     e.target.closest(...) ne retrouve plus le formulaire parent : le
//     marquage "modifié" échouait silencieusement, y compris pour
//     ".playlist-chip-remove" qui semblait pourtant couvert. Solution :
//     écouter en phase de CAPTURE (dernier argument "true") pour agir
//     AVANT que la puce ne soit retirée du DOM, pas après.
// Une fois le panneau d'édition ouvert (ce qui met la vidéo en pause, voir
// pauseForMetadataEdit), plus rien n'empêchait alors un rafraîchissement
// automatique (live-sync.js) de remplacer le formulaire — et tout son
// contenu autour (dont les boutons du lecteur) — en plein milieu de la
// modification, avant même d'avoir cliqué "Enregistrer" : la modification
// semblait "ne pas marcher", et les boutons cliqués juste après (parfois
// recréés à ce moment précis) pouvaient sembler ne plus répondre.
document.addEventListener("click", (e) => {
  if (e.target.closest(".star-choice, .star-clear, .playlist-chip-remove, .js-tag-chip-remove, .tag-suggest-item")) {
    markLiveFormDirty(e.target);
  }
}, true);

window.__hasDirtyEditForm = function () {
  return !!document.querySelector(
    LIVE_GUARDED_FORMS_SELECTOR.split(",")
      .map((s) => `${s.trim()}[data-live-dirty="1"]`)
      .join(",")
  );
};

function updateCategoryEverywhere(id, newName) {
  document.querySelectorAll(`datalist option[data-cat-id="${id}"]`).forEach(opt => {
    opt.value = newName;
  });
  // Champ "Catégorie" (texte libre, popup rapide) : ne le corrige que
  // s'il affiche actuellement CETTE catégorie précise (identifiée par
  // data-initial-category, posé côté serveur sur la page vidéo, et tenu à
  // jour côté client pour la popup rapide — voir initQuickEdit).
  document.querySelectorAll("[data-initial-category]").forEach(f => {
    if (f.dataset.initialCategory && f.dataset.initialCategory === String(id)) {
      const input = f.querySelector("input[name='category']");
      if (input) input.value = newName;
    }
  });
  // Champ "Catégorie" modernisé (page vidéo dédiée, <select>) : renomme
  // l'option correspondante ; si elle est actuellement sélectionnée, le
  // <select> reflète le nouveau nom immédiatement (changer .value d'une
  // <option> sélectionnée met aussi à jour select.value), et on resynchronise
  // alors l'input caché qui porte la valeur réellement soumise au serveur.
  document.querySelectorAll(".js-category-select").forEach(select => {
    const opt = select.querySelector(`option[data-cat-id="${id}"]`);
    if (!opt) return;
    const wasSelected = select.value === opt.value;
    opt.value = newName;
    opt.textContent = newName;
    if (wasSelected) {
      const field = select.closest(".js-category-field");
      const hidden = field && field.querySelector(".js-category-hidden");
      if (hidden) hidden.value = newName;
    }
  });
}

function removeCategoryEverywhere(id) {
  document.querySelectorAll(`datalist option[data-cat-id="${id}"]`).forEach(opt => opt.remove());
  document.querySelectorAll("[data-initial-category]").forEach(f => {
    if (f.dataset.initialCategory && f.dataset.initialCategory === String(id)) {
      f.dataset.initialCategory = "";
      const input = f.querySelector("input[name='category']");
      if (input) input.value = ""; // la vidéo repassera "sans catégorie" (comportement serveur identique)
    }
  });
  // Champ "Catégorie" modernisé : retire l'option ; si elle était
  // sélectionnée, le <select> retombe naturellement sur "Aucune
  // catégorie" (1ère option, value=""), qu'on répercute sur l'input caché.
  document.querySelectorAll(".js-category-field").forEach(field => {
    const select = field.querySelector(".js-category-select");
    if (!select) return;
    const opt = select.querySelector(`option[data-cat-id="${id}"]`);
    if (!opt) return;
    const wasSelected = select.value === opt.value;
    opt.remove();
    if (wasSelected) {
      select.value = "";
      const hidden = field.querySelector(".js-category-hidden");
      const newBox = field.querySelector(".js-category-new-box");
      const deleteBtn = field.querySelector(".js-category-delete-btn");
      const clearBtn = field.querySelector(".js-category-clear-btn");
      const toggleCreateBtn = field.querySelector(".js-category-toggle-create");
      if (hidden) hidden.value = "";
      if (newBox) { newBox.hidden = true; newBox.style.display = "none"; }
      if (toggleCreateBtn) toggleCreateBtn.hidden = false;
      if (deleteBtn) { deleteBtn.hidden = true; deleteBtn.dataset.id = ""; deleteBtn.dataset.name = ""; }
      if (clearBtn) clearBtn.hidden = true;
      const hint = field.querySelector(".js-category-hint");
      if (hint) {
        const hChoose = hint.querySelector(".cat-hint-choose");
        const hAssigned = hint.querySelector(".cat-hint-assigned");
        const hCreate = hint.querySelector(".cat-hint-create");
        if (hChoose) hChoose.hidden = false;
        if (hAssigned) hAssigned.hidden = true;
        if (hCreate) hCreate.hidden = true;
      }
    }
  });
}

function addCategoryEverywhere(id, name) {
  document.querySelectorAll("#category-list, #quick-edit-category-list, #category-datalist").forEach(dl => {
    if (dl.querySelector(`option[data-cat-id="${id}"]`)) return;
    const opt = document.createElement("option");
    opt.value = name;
    opt.dataset.catId = id;
    dl.appendChild(opt);
  });
  // Champ "Catégorie" modernisé : ajoute l'option à la fin du select.
  document.querySelectorAll(".js-category-select").forEach(select => {
    if (select.querySelector(`option[data-cat-id="${id}"]`)) return;
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    opt.dataset.catId = id;
    select.appendChild(opt);
  });
}

function updatePlaylistEverywhere(id, newName) {
  document.querySelectorAll(".js-playlist-field").forEach(field => {
    const cb = field.querySelector(`input[name='playlists'][value="${id}"]`);
    if (cb) {
      cb.dataset.name = newName;
      const span = cb.nextElementSibling;
      if (span && span.tagName === "SPAN") span.textContent = newName;
      const row = cb.closest(".playlist-item-row");
      const delBtn = row && row.querySelector(".playlist-item-delete");
      if (delBtn) {
        delBtn.dataset.name = newName;
        delBtn.title = `Supprimer définitivement « ${newName} » de la base`;
      }
    }
    // Playlist PRINCIPALE renommée : titre de son groupe de sous-playlists,
    // et attribut "parent" (informatif) de ses sous-playlists existantes.
    const group = field.querySelector(`.js-playlist-sub-group[data-parent-id="${id}"]`);
    if (group) {
      group.dataset.parentName = newName;
      const summary = group.querySelector(".js-playlist-sub-summary");
      if (summary) summary.textContent = "Sous-playlist de " + newName;
      group.querySelectorAll("input[name='playlists']").forEach(subCb => {
        subCb.dataset.parent = newName;
      });
    }
    syncPlaylistField(field); // reconstruit les puces si l'élément renommé est actuellement coché
  });
}

function removePlaylistEverywhere(id) {
  document.querySelectorAll(".js-playlist-field").forEach(field => {
    const cb = field.querySelector(`input[name='playlists'][value="${id}"]`);
    if (cb) {
      const row = cb.closest(".playlist-item-row") || cb.closest("label");
      const list = row ? row.parentElement : null;
      if (row) row.remove();
      if (list) {
        const remaining = list.querySelectorAll("input[name='playlists']");
        if (remaining.length === 0) {
          if (list.classList.contains("js-playlist-top-checklist")) {
            list.innerHTML = '<span class="muted playlist-top-empty">Aucune playlist principale pour le moment.</span>';
          } else if (list.classList.contains("js-playlist-sub-checklist")) {
            list.innerHTML = '<span class="muted playlist-sub-empty">Aucune sous-playlist pour le moment.</span>';
          }
        }
      }
    }
    // Si c'est une playlist PRINCIPALE : son groupe de sous-playlists
    // disparaît aussi (cohérent avec la suppression en cascade côté serveur).
    const group = field.querySelector(`.js-playlist-sub-group[data-parent-id="${id}"]`);
    if (group) group.remove();
    syncPlaylistField(field);
  });
}

// Construit la structure "Ajouter à une playlist principale" / "Ajouter à
// un sous-playlist" dans un champ qui n'en avait encore aucune (aucune
// playlist n'existait au chargement de la page — voir le {% else %} de
// templates/_playlist_field.html), pour accueillir la toute première
// playlist créée pendant que la page reste ouverte.
function ensurePlaylistFieldSkeleton(field) {
  if (field.querySelector(".playlist-add-row")) return;
  Array.from(field.children).forEach(el => {
    if (el.matches("span.muted") && /Crée une playlist/.test(el.textContent)) el.remove();
  });

  const topRow = document.createElement("div");
  topRow.className = "playlist-add-row";
  const topLabel = document.createElement("span");
  topLabel.className = "playlist-add-label";
  topLabel.textContent = "Ajouter à une playlist principale";
  const topList = document.createElement("div");
  topList.className = "playlist-checkbox-list js-playlist-top-checklist";
  
  const topInline = document.createElement("div");
  topInline.className = "playlist-new-top-inline";
  const topInput = document.createElement("input");
  topInput.type = "text";
  topInput.className = "js-new-top-name";
  topInput.placeholder = "Nom de la nouvelle playlist principale";
  const topCreateBtn = document.createElement("button");
  topCreateBtn.type = "button";
  topCreateBtn.className = "playlist-add-btn js-new-top-create-btn";
  topCreateBtn.textContent = "+ Créer et ajouter";
  topInline.appendChild(topInput);
  topInline.appendChild(topCreateBtn);

  topRow.appendChild(topLabel);
  topRow.appendChild(topList);
  topRow.appendChild(topInline);

  const subRow = document.createElement("div");
  subRow.className = "playlist-add-row js-sub-add-row playlist-add-row-disabled";
  const subLabel = document.createElement("span");
  subLabel.className = "playlist-add-label";
  subLabel.textContent = "Ajouter à un sous-playlist";
  const hint = document.createElement("p");
  hint.className = "playlist-lock-hint js-sub-lock-hint";
  hint.hidden = true;
  hint.textContent = "🚫 Ajout de sous-playlist impossible : coche d'abord une playlist principale ci-dessus.";
  const subGroups = document.createElement("div");
  subGroups.className = "playlist-sub-groups js-playlist-sub-groups";
  subRow.appendChild(subLabel);
  subRow.appendChild(hint);
  subRow.appendChild(subGroups);

  field.appendChild(topRow);
  field.appendChild(subRow);
}

function addTopPlaylistEverywhere(id, name, checkedField) {
  document.querySelectorAll(".js-playlist-field").forEach(field => {
    ensurePlaylistFieldSkeleton(field);

    const topList = field.querySelector(".js-playlist-top-checklist");
    if (topList && !topList.querySelector(`input[value="${id}"]`)) {
      const emptyMsg = topList.querySelector(".playlist-top-empty");
      if (emptyMsg) emptyMsg.remove();
      const row = document.createElement("div");
      row.className = "playlist-item-row";
      const label = document.createElement("label");
      label.className = "checkbox-field playlist-checkbox playlist-checkbox-top";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.name = "playlists";
      cb.value = id;
      cb.className = "js-top-playlist-cb";
      cb.dataset.name = name;
      if (field === checkedField) cb.checked = true;
      const span = document.createElement("span");
      span.textContent = name;
      label.appendChild(cb);
      label.appendChild(span);
      row.appendChild(label);
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "playlist-item-delete icon-btn icon-btn-mini icon-btn-danger js-delete";
      delBtn.dataset.type = "playlist";
      delBtn.dataset.id = id;
      delBtn.dataset.name = name;
      delBtn.title = `Supprimer définitivement « ${name} » de la base`;
      delBtn.textContent = "🗑";
      row.appendChild(delBtn);
      topList.appendChild(row);
      if (field === checkedField) syncPlaylistField(field);
    }

    const subGroups = field.querySelector(".js-playlist-sub-groups");
    if (subGroups && !subGroups.querySelector(`.js-playlist-sub-group[data-parent-id="${id}"]`)) {
      const details = document.createElement("details");
      details.className = "playlist-sub-group js-playlist-sub-group";
      details.dataset.parentId = id;
      details.dataset.parentName = name;
      details.hidden = true;
      const summary = document.createElement("summary");
      summary.className = "playlist-sub-group-title js-playlist-sub-summary";
      summary.textContent = "Sous-playlist de " + name;
      const checklist = document.createElement("div");
      checklist.className = "playlist-checkbox-list js-playlist-sub-checklist";
      const emptyMsg = document.createElement("span");
      emptyMsg.className = "muted playlist-sub-empty";
      emptyMsg.textContent = "Aucune sous-playlist pour le moment.";
      checklist.appendChild(emptyMsg);
      const inline = document.createElement("div");
      inline.className = "playlist-new-sub-inline";
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "js-new-sub-name";
      nameInput.placeholder = "Nom de la nouvelle sous-playlist";
      const dateInput = document.createElement("input");
      dateInput.type = "date";
      dateInput.className = "js-new-sub-date";
      dateInput.title = "Date de création (obligatoire)";
      const createBtn = document.createElement("button");
      createBtn.type = "button";
      createBtn.className = "playlist-add-btn js-new-sub-create-btn";
      createBtn.dataset.parentId = id;
      createBtn.textContent = "+ Créer et ajouter";
      inline.appendChild(nameInput);
      inline.appendChild(dateInput);
      inline.appendChild(createBtn);
      details.appendChild(summary);
      details.appendChild(checklist);
      details.appendChild(inline);
      subGroups.appendChild(details);
    }
  });
}

function addSubPlaylistEverywhere(parentId, parentName, id, name, checkedField) {
  document.querySelectorAll(".js-playlist-field").forEach(field => {
    const group = field.querySelector(`.js-playlist-sub-group[data-parent-id="${parentId}"]`);
    if (!group) return;
    const checklist = group.querySelector(".js-playlist-sub-checklist");
    if (!checklist || checklist.querySelector(`input[value="${id}"]`)) return;
    const emptyMsg = checklist.querySelector(".playlist-sub-empty");
    if (emptyMsg) emptyMsg.remove();
    const row = document.createElement("div");
    row.className = "playlist-item-row playlist-sub-item-row";
    const label = document.createElement("label");
    label.className = "checkbox-field playlist-checkbox playlist-checkbox-sub";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.name = "playlists";
    cb.value = id;
    cb.dataset.name = name;
    cb.dataset.parent = parentName;
    if (field === checkedField) cb.checked = true;
    const span = document.createElement("span");
    span.textContent = name;
    label.appendChild(cb);
    label.appendChild(span);
    row.appendChild(label);
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "playlist-item-delete icon-btn icon-btn-mini icon-btn-danger js-delete";
    delBtn.dataset.type = "playlist";
    delBtn.dataset.id = id;
    delBtn.dataset.name = name;
    delBtn.title = `Supprimer définitivement la sous-playlist « ${name} » de la base`;
    delBtn.textContent = "🗑";
    row.appendChild(delBtn);
    checklist.appendChild(row);
    syncPlaylistField(field);
  });
}

// ============ Champ "Date de création" (sous-playlist) : rendu visible et
// obligatoire dès qu'une "Playlist parente" est choisie dans le formulaire
// (création ou édition d'une playlist), masqué et facultatif sinon —
// puisqu'une playlist principale n'a pas cette date. ============
function initManualDateToggle(root) {
  root = root || document;
  function wire(form) {
    const parentSelect = form.querySelector("select[name='parent_id']");
    const dateInput = form.querySelector(".js-manual-date-input");
    if (!parentSelect || !dateInput) return;
    const dateLabel = dateInput.closest(".js-manual-date-label");
    function sync() {
      const isSub = !!parentSelect.value;
      if (dateLabel) dateLabel.hidden = !isSub;
      else dateInput.hidden = !isSub;
      dateInput.required = isSub;
    }
    parentSelect.addEventListener("change", sync);
    sync();
  }
  root.querySelectorAll(".org-edit-form, .organize-new-form").forEach(wire);
}

// ============ Gestion des catégories / playlists / tags ============
// Renommer/supprimer se fait en AJAX (fetch), jamais via un vrai <form>
// soumis nativement : un submit natif recharge toute la page, ce qui
// coupait la lecture en cours d'une vidéo (et la faisait reprendre depuis
// le début) si l'action était faite depuis le panneau latéral gauche
// pendant la lecture. Le DOM (panneau latéral, en-tête de page catégorie/
// playlist, cartes "Organiser", liste des tags) est mis à jour directement
// à partir de la réponse du serveur, sans jamais recharger la page.
const ENTITY_LABELS = {
  category: { noun: "la catégorie", endpoint: "/category" },
  playlist: { noun: "la playlist", endpoint: "/playlist" },
  tag: { noun: "le tag", endpoint: "/tag" },
};

function submitEntityAjax(action, fields) {
  const data = new FormData();
  data.append("ajax", "1");
  Object.keys(fields || {}).forEach(k => data.append(k, fields[k]));
  return fetch(action, { method: "POST", body: data, cache: "no-store" })
    .then(r => r.json().then(json => ({ status: r.status, json })))
    .then(({ status, json }) => {
      if (status >= 200 && status < 300 && json && json.ok) return json;
      throw new Error((json && json.error) || `HTTP ${status}`);
    });
}

// Réordonne immédiatement, en ordre alphabétique (insensible à la casse et
// aux accents), les éléments enfants directs de `container` correspondant à
// `itemSelector`. Utilisé juste après un renommage : le serveur trie déjà
// ainsi (voir COLLATE NOCASE dans app.py/db.py), mais sans ce tri côté
// client l'élément renommé restait visuellement à son ancienne place
// jusqu'au prochain rafraîchissement (via /api/live) — ce qui, pour un
// renommage qui change la lettre de tri, pouvait donner l'impression que
// rien ne s'était mis à jour en temps réel. On se base sur data-name (déjà
// mis à jour juste avant, pour tous les boutons .js-rename concernés) plutôt
// que de relire le texte affiché, pour rester correct même si le libellé
// contient d'autres éléments (compteur, icône...).
function resortAlphaContainer(container, itemSelector) {
  if (!container) return;
  const items = Array.from(container.children).filter(el => el.matches(itemSelector));
  if (items.length < 2) return;
  const keyOf = (item) => {
    const btn = item.querySelector(".js-rename");
    return ((btn && btn.dataset.name) || "").toLocaleLowerCase("fr");
  };
  const decorated = items.map((item, idx) => ({ item, key: keyOf(item), idx }));
  decorated.sort((a, b) => a.key.localeCompare(b.key, "fr", { sensitivity: "base" }) || a.idx - b.idx);
  decorated.forEach(({ item }) => container.appendChild(item));
}

// Même principe qu'applyEntityRename, mais pour la photo de couverture :
// met à jour l'avatar affiché dans le panneau latéral pour une catégorie ou
// une playlist, dès que sa photo est modifiée depuis n'importe quel
// formulaire (page Organiser, ou page dédiée catégorie/playlist) — sans
// quoi seul le prochain rafraîchissement temps réel (/api/live) l'aurait
// mise à jour, avec un délai perceptible.
function applyEntityImageUpdate(type, id, imageUrl) {
  if (!imageUrl) return;
  document.querySelectorAll(`.side-item .js-rename[data-type="${type}"][data-id="${id}"]`).forEach(btn => {
    const li = btn.closest(".side-item");
    const avatar = li && li.querySelector(".side-item-avatar");
    if (!avatar) return;
    avatar.innerHTML = "";
    const img = document.createElement("img");
    img.src = imageUrl;
    img.alt = "";
    img.loading = "lazy";
    avatar.appendChild(img);
  });

  // Cartes génériques (ex. cases de la fenêtre "🏷 Tags") : met à jour la
  // vignette letterbox (fond flouté + image principale) si la carte en
  // affichait déjà une, sinon reconstruit la case pour faire apparaître la
  // photo à la place du placeholder — sans quoi une image ajoutée depuis la
  // page dédiée du tag resterait invisible dans la fenêtre "🏷 Tags" jusqu'au
  // prochain rafraîchissement temps réel.
  document.querySelectorAll(`.org-card-delete[data-type="${type}"][data-id="${id}"]`).forEach(btn => {
    const card = btn.closest(".org-card");
    const frame = card && card.querySelector(".org-cover-frame");
    if (!frame) return;
    const bg = frame.querySelector(".org-cover-bg");
    const main = frame.querySelector(".org-cover-main");
    if (bg && main) {
      bg.src = imageUrl;
      main.src = imageUrl;
    } else {
      frame.innerHTML =
        `<img class="org-cover-bg" src="${imageUrl}" alt="" aria-hidden="true">` +
        `<img class="org-cover-main" src="${imageUrl}" alt="">`;
    }
    card.dataset.hasPhoto = "1";
  });
}

function applyEntityRename(type, id, newName) {
  document.querySelectorAll(`.js-rename[data-type="${type}"][data-id="${id}"], .js-delete[data-type="${type}"][data-id="${id}"]`)
    .forEach(btn => { btn.dataset.name = newName; });

  // Panneau latéral gauche
  document.querySelectorAll(`.side-item .js-rename[data-type="${type}"][data-id="${id}"]`).forEach(btn => {
    const li = btn.closest(".side-item");
    const nameEl = li && li.querySelector(".side-item-name, .side-item-label");
    if (nameEl) nameEl.textContent = newName;
    if (li) resortAlphaContainer(li.parentElement, ".side-item");
  });

  // En-tête de la page dédiée (catégorie / playlist), si affichée
  if (document.querySelector(`.collection-actions .js-rename[data-type="${type}"][data-id="${id}"]`)) {
    const titleEl = document.querySelector(".collection-title");
    if (titleEl) titleEl.textContent = newName;
  }

  // Cartes de la page "Organiser" : on remet à jour le titre, puis on
  // ré-applique le tri actuellement choisi dans le menu de cette liste
  // précise (par nom, par note, par date...) — sans effet si le tri en
  // cours ne dépend pas du nom, et remet immédiatement la carte à sa bonne
  // place s'il en dépend.
  document.querySelectorAll(`.org-card-delete[data-type="${type}"][data-id="${id}"]`).forEach(btn => {
    const card = btn.closest(".org-card");
    if (!card) return;
    const titleEl = card.querySelector(".org-card-title strong");
    if (titleEl) titleEl.textContent = newName;
    card.dataset.name = newName.toLowerCase();
    const list = card.closest(".org-cards-list");
    if (list && typeof list.__resortCurrent === "function") list.__resortCurrent();
  });

  // Panneau "✎ Modifier les métadonnées" (page vidéo + popup rapide) :
  // champ "Catégorie" et cases à cocher "Playlists" — voir plus bas.
  if (type === "category") updateCategoryEverywhere(id, newName);
  if (type === "playlist") updatePlaylistEverywhere(id, newName);
}

function applyEntityRemoval(type, id) {
  document.querySelectorAll(`.js-delete[data-type="${type}"][data-id="${id}"]`).forEach(btn => {
    const li = btn.closest(".side-item");
    if (li) { li.remove(); return; }
    const card = btn.closest(".org-card");
    if (card) { card.remove(); return; }
    btn.remove();
  });

  // Panneau "✎ Modifier les métadonnées" (page vidéo + popup rapide).
  if (type === "category") removeCategoryEverywhere(id);
  if (type === "playlist") removePlaylistEverywhere(id);
  if (type === "tag") {
    removeTagEverywhere(id);
    // Badge / compteur de la fenêtre Tags
    const list = document.getElementById("tags-modal-list");
    if (list) {
      const n = list.querySelectorAll(".org-card-tag").length;
      document.querySelectorAll("#tags-modal-open .count").forEach(el => { el.textContent = String(n); });
      const badge = document.getElementById("tags-modal-count-badge");
      if (badge) badge.textContent = String(n);
      const result = document.getElementById("tags-modal-result-count");
      if (result) {
        result.textContent = n ? (n + " tag" + (n > 1 ? "s" : "")) : "";
      }
      if (n === 0 && !list.querySelector(".js-tags-modal-empty")) {
        const empty = document.createElement("div");
        empty.className = "tags-modal-empty js-tags-modal-empty";
        empty.innerHTML = '<div class="tags-modal-empty-icon" aria-hidden="true">🏷</div>' +
          '<p class="tags-modal-empty-title">Aucun tag pour le moment</p>' +
          '<p class="muted">Crée le premier ci-dessus — tu pourras lui ajouter une image ensuite.</p>';
        list.appendChild(empty);
      }
    }
  }
}

// Empêche double-clic rename/delete sur le même élément.
const _entityActionLocks = new Set();

function renameEntity(type, id, currentName) {
  const lockKey = `rename:${type}:${id}`;
  if (_entityActionLocks.has(lockKey)) return;
  const info = ENTITY_LABELS[type];
  const newName = window.prompt(`Renommer ${info.noun} :`, currentName);
  if (newName === null) return;
  const trimmed = newName.trim();
  if (!trimmed || trimmed === currentName) return;

  _entityActionLocks.add(lockKey);
  submitEntityAjax(`${info.endpoint}/${id}/rename`, { name: trimmed })
    .then(res => applyEntityRename(type, id, res.name !== undefined ? res.name : trimmed))
    .catch(err => alert(`Impossible de renommer ${info.noun} : ` + err.message))
    .finally(() => _entityActionLocks.delete(lockKey));
}

function deleteEntity(type, id, currentName) {
  const lockKey = `delete:${type}:${id}`;
  if (_entityActionLocks.has(lockKey)) return;
  const info = ENTITY_LABELS[type];
  const extra = type === "category"
    ? " Les vidéos concernées repasseront simplement « sans catégorie »."
    : type === "tag"
      ? " Il sera retiré de toutes les vidéos qui l'utilisent."
      : " Les vidéos qu'elle contient ne seront pas supprimées.";
  const confirmed = window.confirm(`Supprimer ${info.noun} « ${currentName} » ?${extra}`);
  if (!confirmed) return;

  _entityActionLocks.add(lockKey);
  submitEntityAjax(`${info.endpoint}/${id}/delete`, {})
    .then(res => {
      if (res.own_page) {
        window.location.href = "/";
        return;
      }
      applyEntityRemoval(type, id);
    })
    .catch(err => alert(`Impossible de supprimer ${info.noun} : ` + err.message))
    .finally(() => _entityActionLocks.delete(lockKey));
}

// Délégation d'événements : les noms (qui peuvent contenir des guillemets,
// apostrophes, etc.) transitent via data-attributes plutôt que par du
// onclick inline, pour rester sûrs quel que soit le contenu du nom.
document.addEventListener("click", (e) => {
  const renameBtn = e.target.closest(".js-rename");
  if (renameBtn) {
    renameEntity(renameBtn.dataset.type, renameBtn.dataset.id, renameBtn.dataset.name);
    return;
  }
  const deleteBtn = e.target.closest(".js-delete");
  if (deleteBtn) {
    deleteEntity(deleteBtn.dataset.type, deleteBtn.dataset.id, deleteBtn.dataset.name);
  }
});

// ============ Création rapide (panneau latéral gauche) : catégorie /
// playlist ============ Même souci que ci-dessus : ces deux formulaires
// étaient soumis nativement (rechargement complet de la page), ce qui
// coupait la lecture en cours d'une vidéo. Passage en AJAX + insertion
// directe du nouvel élément dans la liste correspondante, sans jamais
// recharger la page.
// Même palette que PLAYLIST_COLORS côté serveur (voir app.py) : permet de
// donner tout de suite à une playlist nouvellement créée sa couleur
// définitive dans le panneau latéral (calculée à partir de son id, stable
// pour toujours), au lieu d'afficher la couleur générique par défaut
// jusqu'au prochain rafraîchissement temps réel.
const PLAYLIST_COLORS = [
  "#e07a5f", "#3d5a80", "#8ac926", "#ff006e", "#ffb703",
  "#6a4c93", "#118ab2", "#ef476f", "#06a77d", "#f3722c",
];

function buildSideItem(type, id, name) {
  const li = document.createElement("li");
  li.className = "side-item";
  if (type === "playlist") {
    const numericId = parseInt(id, 10);
    if (!Number.isNaN(numericId)) {
      li.style.setProperty("--item-color", PLAYLIST_COLORS[numericId % PLAYLIST_COLORS.length]);
    }
  }

  const a = document.createElement("a");
  a.href = `/${type}/${id}`;

  const avatar = document.createElement("span");
  avatar.className = "side-item-avatar";
  const fallback = document.createElement("span");
  fallback.className = "side-item-avatar-fallback";
  fallback.textContent = type === "category" ? "🗂" : "📃";
  avatar.appendChild(fallback);

  const nameEl = document.createElement("span");
  nameEl.className = type === "category" ? "side-item-name" : "side-item-label";
  nameEl.textContent = name;

  const count = document.createElement("span");
  count.className = "count " + (type === "category" ? "count-rose" : "count-orange");
  count.textContent = "0";

  a.appendChild(avatar);
  a.appendChild(nameEl);
  a.appendChild(count);

  const actions = document.createElement("span");
  actions.className = "side-item-actions";
  const renameBtn = document.createElement("button");
  renameBtn.type = "button";
  renameBtn.className = "icon-btn icon-btn-mini js-rename";
  renameBtn.dataset.type = type;
  renameBtn.dataset.id = id;
  renameBtn.dataset.name = name;
  renameBtn.title = type === "category" ? "Renommer la catégorie" : "Renommer la playlist";
  renameBtn.textContent = "✎";
  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "icon-btn icon-btn-mini icon-btn-danger js-delete";
  deleteBtn.dataset.type = type;
  deleteBtn.dataset.id = id;
  deleteBtn.dataset.name = name;
  deleteBtn.title = type === "category" ? "Supprimer la catégorie" : "Supprimer la playlist";
  deleteBtn.textContent = "✕";
  actions.appendChild(renameBtn);
  actions.appendChild(deleteBtn);

  li.appendChild(a);
  li.appendChild(actions);
  return li;
}

// Ajuste en temps réel le compteur affiché à côté d'une catégorie ou d'une
// playlist — dans le panneau ☰ (sidebar) ET sur la page Organiser
// (org-card-count) — sans recharger la page, quand on assigne/retire une
// vidéo à une catégorie ou une playlist depuis "✎ Modifier les métadonnées"
// (page vidéo dédiée ou popup de modification rapide).
function adjustEntityCount(type, id, delta) {
  if (!id || !delta) return;
  document.querySelectorAll(`.js-rename[data-type="${type}"][data-id="${id}"]`).forEach(btn => {
    const sideItem = btn.closest(".side-item");
    if (sideItem) {
      const countEl = sideItem.querySelector(".count");
      if (countEl) {
        const current = parseInt(countEl.textContent, 10) || 0;
        countEl.textContent = String(Math.max(0, current + delta));
      }
    }
    const card = btn.closest(".org-card");
    if (card) {
      const countEl = card.querySelector(".org-card-count");
      if (countEl) {
        const current = parseInt(countEl.textContent, 10) || 0;
        const next = Math.max(0, current + delta);
        countEl.textContent = `${next} vidéo${next !== 1 ? "s" : ""}`;
      }
    }
  });
}

// Compare l'ancienne et la nouvelle affectation (catégorie + playlists)
// d'une vidéo après enregistrement, et répercute les deltas (+1/-1) sur
// les compteurs concernés.
function applySidebarCountDeltas(oldCategory, newCategory, oldPlaylistIds, newPlaylistIds) {
  if (oldCategory !== newCategory) {
    if (oldCategory) adjustEntityCount("category", oldCategory, -1);
    if (newCategory) adjustEntityCount("category", newCategory, +1);
  }
  oldPlaylistIds.filter(id => !newPlaylistIds.includes(id)).forEach(id => adjustEntityCount("playlist", id, -1));
  newPlaylistIds.filter(id => !oldPlaylistIds.includes(id)).forEach(id => adjustEntityCount("playlist", id, +1));
}

function initSideQuickCreateForms(root) {
  root = root || document;
  root.querySelectorAll(".side-section-categories .inline-form, .side-section-playlists .inline-form").forEach(form => {
    if (form.dataset.quickCreateReady) return;
    form.dataset.quickCreateReady = "1";
    const type = form.closest(".side-section-categories") ? "category" : "playlist";
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const input = form.querySelector("input[name='name']");
      const name = input ? input.value.trim() : "";
      if (!name) return;
      const btn = form.querySelector("button[type='submit']");
      if (btn) btn.disabled = true;
      submitEntityAjax(form.action, { name })
        .then(res => {
          const list = form.closest(".side-section").querySelector(".side-list");
          if (list) {
            const empty = list.querySelector(".side-item-empty");
            if (empty) empty.remove();
            list.appendChild(buildSideItem(type, res.id, res.name !== undefined ? res.name : name));
            resortAlphaContainer(list, ".side-item");
          }
          // Panneau "✎ Modifier les métadonnées" (page vidéo + popup
          // rapide) : rend la nouvelle catégorie/playlist immédiatement
          // disponible (liste déroulante ou case à cocher), sans attendre
          // un rechargement de page.
          const finalName = res.name !== undefined ? res.name : name;
          if (type === "category") addCategoryEverywhere(res.id, finalName);
          if (type === "playlist") addTopPlaylistEverywhere(res.id, finalName);
          if (input) input.value = "";
        })
        .catch(err => alert("Impossible de créer : " + err.message))
        .finally(() => {
          if (btn) btn.disabled = false;
          if (input) input.focus();
        });
    });
  });
}

// ============ Popup de modification rapide (depuis les grilles) ============
(function initQuickEdit() {
  const overlay = document.getElementById("quick-edit-overlay");
  if (!overlay) return;
  const form = document.getElementById("quick-edit-form");
  const loading = document.getElementById("quick-edit-loading");
  const closeBtn = document.getElementById("quick-edit-close");
  let currentVideoId = null;

  function openModal(videoId) {
    currentVideoId = videoId;
    overlay.classList.add("open");
    form.style.display = "none";
    loading.style.display = "block";
    loading.textContent = "Chargement...";
    fetch(`/api/video/${videoId}/meta`, { cache: "no-store" })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(data => {
        form.title.value = data.title || "";
        form.description.value = data.description || "";
        // Champ catégorie modernisé (select + actions) — plus de form.category texte libre.
        const catField = form.querySelector(".js-category-field");
        if (catField && typeof setCategoryFieldValue === "function") {
          setCategoryFieldValue(
            catField,
            data.category_id !== undefined && data.category_id !== null ? data.category_id : null,
            data.category || ""
          );
        } else if (form.category) {
          form.category.value = data.category || "";
        }
        // Permet au champ "Catégorie" de se corriger tout seul si la
        // catégorie affichée ici est renommée pendant que la popup reste
        // ouverte (voir updateCategoryEverywhere / removeCategoryEverywhere).
        form.dataset.initialCategory = data.category_id !== undefined && data.category_id !== null
          ? String(data.category_id)
          : "";
        const tagField = form.querySelector(".js-tag-field");
        if (tagField) setTagFieldValue(tagField, data.tags_full || []);
        form.video_date.value = data.video_date || "";
        form.comment.value = data.comment || "";
        form.views.value = data.views || 0;
        const selectedPlaylistIds = (data.playlist_ids || []).map(String);
        // Permet de calculer les deltas de compteurs (☰ / page Organiser)
        // au moment de l'enregistrement — voir applySidebarCountDeltas.
        form.dataset.initialPlaylists = selectedPlaylistIds.slice().sort().join(",");
        form.querySelectorAll("input[name='playlists']").forEach(cb => {
          cb.checked = selectedPlaylistIds.includes(cb.value);
        });
        const playlistField = form.querySelector(".js-playlist-field");
        if (playlistField) {
          playlistField.querySelectorAll(".js-new-sub-name").forEach(input => { input.value = ""; });
          playlistField.querySelectorAll(".js-new-sub-date").forEach(input => { input.value = ""; });
          syncPlaylistField(playlistField);
        }
        const hidden = form.querySelector("input[name='rating']");
        const value = data.rating || 0;
        hidden.value = value;
        form.querySelectorAll(".star-choice").forEach(s => {
          s.classList.toggle("filled", parseInt(s.dataset.star, 10) <= value);
        });
        // Données fraîches tout juste reçues du serveur : aucune
        // modification de l'utilisateur à protéger pour l'instant (voir
        // markLiveFormDirty / clearLiveFormDirty).
        clearLiveFormDirty(form);
        loading.style.display = "none";
        form.style.display = "grid";
      })
      .catch((err) => {
        loading.textContent = "Impossible de charger les métadonnées (" + err.message + ").";
      });
  }

  function closeModal() {
    overlay.classList.remove("open");
    currentVideoId = null;
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".js-quick-edit");
    if (btn) {
      e.preventDefault();
      openModal(btn.dataset.videoId);
    }
  });

  closeBtn.addEventListener("click", closeModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay.classList.contains("open")) closeModal();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!currentVideoId) return;
    const submitBtn = form.querySelector("button[type='submit']");
    const originalLabel = submitBtn ? submitBtn.textContent : null;
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Enregistrement...";
    }
    const data = new FormData(form);
    appendPendingTagImages(form, data);
    fetch(`/video/${currentVideoId}/edit`, { method: "POST", body: data, cache: "no-store" })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(res => {
        if (res && res.ok) {
          // Enregistrement confirmé : plus rien à protéger d'un
          // rafraîchissement temps réel dans ce formulaire.
          clearLiveFormDirty(form);

          // Compteurs du panneau ☰ (et de la page Organiser) mis à jour
          // tout de suite, en temps réel, sans attendre un rechargement.
          const oldCategory = form.dataset.initialCategory || "";
          const oldPlaylists = (form.dataset.initialPlaylists || "").split(",").filter(Boolean);
          const newCategory = res.category_id !== undefined && res.category_id !== null ? String(res.category_id) : "";
          const newPlaylistsArr = res.playlist_ids !== undefined ? res.playlist_ids.map(String) : oldPlaylists;
          applySidebarCountDeltas(oldCategory, newCategory, oldPlaylists, newPlaylistsArr);
          form.dataset.initialCategory = newCategory;
          form.dataset.initialPlaylists = newPlaylistsArr.slice().sort().join(",");

          // Met à jour la carte de cette vidéo directement dans la grille,
          // sans recharger toute la page : c'est immédiat, et ça évite de
          // perdre le défilement / la sélection en cours. On voit tout de
          // suite que la modification a bien été prise en compte.
          applyCardUpdate(currentVideoId, res);
          if (typeof window.syncTagsModalWithVideoTags === "function") {
            window.syncTagsModalWithVideoTags(res.tags || []);
          }
          if (typeof showToast === "function") showToast("Mise à jour effectuée");
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = originalLabel;
          }
          closeModal();
        } else {
          throw new Error((res && res.error) || "réponse inattendue du serveur");
        }
      })
      .catch((err) => {
        alert("Une erreur est survenue lors de l'enregistrement : " + err.message);
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = originalLabel;
        }
      });
  });

  // Reflète le titre / les vues / la note enregistrés sur la carte de la
  // grille correspondante, sans recharger la page. Si la vidéo est
  // affichée parce qu'elle appartenait à la catégorie ou à la playlist
  // actuellement filtrée et qu'elle n'y est plus après l'enregistrement,
  // la carte est retirée de la grille (comme l'aurait fait un rechargement
  // complet de la page).
  function applyCardUpdate(videoId, res) {
    const btn = document.querySelector(`.js-quick-edit[data-video-id="${videoId}"]`);
    const card = btn ? btn.closest(".card-wrap") : null;
    if (!card) return;

    const params = new URLSearchParams(window.location.search);
    const filterCategory = params.get("category");
    const filterPlaylist = params.get("playlist");
    const stillInCategory = !filterCategory || String(res.category_id) === filterCategory;
    const stillInPlaylist = !filterPlaylist || (res.playlist_ids || []).map(String).includes(filterPlaylist);

    if (!stillInCategory || !stillInPlaylist) {
      card.remove();
      const countEl = document.querySelector(".result-count");
      if (countEl) {
        const n = parseInt(countEl.textContent, 10);
        if (!isNaN(n) && n > 0) {
          countEl.textContent = countEl.textContent.replace(String(n), String(n - 1));
        }
      }
      return;
    }

    const titleEl = card.querySelector(".card-title");
    if (titleEl && typeof res.title === "string") titleEl.textContent = res.title;

    const metaEl = card.querySelector(".card-meta");
    if (metaEl && typeof res.views === "number") {
      const dimsMatch = metaEl.textContent.match(/\d+\s*×\s*\d+/);
      const dims = dimsMatch ? dimsMatch[0] : "";
      const viewsLabel = `${res.views} vue${res.views !== 1 ? "s" : ""}`;
      let html = viewsLabel;
      if (dims) html += ` · ${dims}`;
      if (res.rating) html += ` · <span class="stars-display">${"★".repeat(res.rating)}</span>`;
      metaEl.innerHTML = html;
    }
  }
})();

// ============ Fenêtre listant tous les tags (bouton "🏷 Tags" du panneau) :
// vignette cliquable/glissable avec enregistrement immédiat de l'image (pas
// de bouton "Enregistrer" séparé), nom modifiable en ligne (clic sur le
// crayon, sans boîte de dialogue), et création rapide d'un nouveau tag
// directement depuis la fenêtre. ============
(function initTagsModal() {
  const overlay = document.getElementById("tags-modal-overlay");
  if (!overlay) return;
  const closeBtn = document.getElementById("tags-modal-close");
  const list = document.getElementById("tags-modal-list");
  const template = document.getElementById("tag-card-template");
  const createForm = document.getElementById("tags-modal-create-form");
  const createInput = document.getElementById("tags-modal-create-input");
  const createHint = document.getElementById("tags-modal-create-hint");
  const countBadge = document.getElementById("tags-modal-count-badge");
  const resultCountEl = document.getElementById("tags-modal-result-count");
  const searchInput = document.getElementById("tags-modal-search-input");

  function openModal() {
    overlay.classList.add("open");
    updateTagsModalCount();
    updateResultCount();
    // Focus recherche (confort, style apps modernes)
    if (searchInput) {
      setTimeout(() => { try { searchInput.focus(); } catch (e) {} }, 40);
    }
  }
  function closeModal() { overlay.classList.remove("open"); }

  // Le bouton "🏷 Tags" vit dans le panneau latéral gauche, qui peut être
  // remplacé par une navigation instantanée (lecture vidéo en cours, voir
  // player-persist.js) : délégation sur document plutôt qu'un binding
  // direct, pour rester fonctionnel après un remplacement du panneau sans
  // avoir besoin de ré-initialisation.
  document.addEventListener("click", (e) => {
    if (e.target.closest("#tags-modal-open")) openModal();
  });
  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay.classList.contains("open")) closeModal();
  });

  if (!list) return;

  function setCreateHint(msg, kind) {
    if (!createHint) return;
    if (!msg) {
      createHint.hidden = true;
      createHint.textContent = "";
      createHint.classList.remove("is-error", "is-ok");
      return;
    }
    createHint.hidden = false;
    createHint.textContent = msg;
    createHint.classList.toggle("is-error", kind === "error");
    createHint.classList.toggle("is-ok", kind === "ok");
  }

  function updateResultCount() {
    if (!resultCountEl) return;
    const cards = list.querySelectorAll(".org-card-tag");
    let visible = 0;
    cards.forEach(c => {
      if (c.style.display !== "none") visible++;
    });
    const total = cards.length;
    if (total === 0) {
      resultCountEl.textContent = "";
      return;
    }
    if (visible === total) {
      resultCountEl.textContent = total + " tag" + (total > 1 ? "s" : "");
    } else {
      resultCountEl.textContent = visible + " / " + total;
    }
  }

  // Recalcule le compteur visible quand le filtre de la toolbar change
  // (initOrganizeGrids pose les listeners sur input/change).
  const toolbar = overlay.querySelector(".tags-modal-toolbar-modern");
  if (toolbar) {
    toolbar.addEventListener("input", () => setTimeout(updateResultCount, 0));
    toolbar.addEventListener("change", () => setTimeout(updateResultCount, 0));
  }

  // --- Vignette : clic ou glisser-déposer = enregistrement immédiat ---
  function setCardCoverImage(card, imageUrl) {
    const slot = card.querySelector(".js-tag-instant-upload");
    let bg = slot.querySelector(".org-cover-bg");
    let main = slot.querySelector(".org-cover-main");
    const placeholder = slot.querySelector(".tag-card-placeholder");
    const hint = slot.querySelector(".tag-card-cover-hint");
    if (!bg || !main) {
      if (placeholder) placeholder.remove();
      bg = document.createElement("img");
      bg.className = "org-cover-bg";
      bg.alt = "";
      bg.setAttribute("aria-hidden", "true");
      main = document.createElement("img");
      main.className = "org-cover-main";
      main.alt = "";
      slot.insertBefore(bg, hint);
      slot.insertBefore(main, hint);
    }
    bg.src = imageUrl;
    main.src = imageUrl;
    card.dataset.hasPhoto = "1";
  }

  function uploadTagImage(card, tagId, file) {
    if (card.classList.contains("tag-card-uploading")) return;
    if (file.size > 8 * 1024 * 1024) {
      alert("Image trop lourde (max 8 Mo).");
      return;
    }
    if (!/\.(jpe?g|png|webp|gif)$/i.test(file.name || "")) {
      alert("Formats acceptés : JPG, PNG, WEBP, GIF.");
      return;
    }
    card.classList.add("tag-card-uploading");

    const data = new FormData();
    data.append("ajax", "1");
    data.append("image1", file, file.name);
    fetch(`/tag/${tagId}/full_edit`, { method: "POST", body: data, cache: "no-store" })
      .then(r => r.json().then(json => ({ status: r.status, json })))
      .then(({ status, json }) => {
        if (status >= 200 && status < 300 && json && json.ok && json.image_url) {
          setCardCoverImage(card, json.image_url);
          applyEntityImageUpdate("tag", tagId, json.image_url);
          showToast("Image du tag mise à jour");
        } else {
          throw new Error((json && json.error) || `HTTP ${status}`);
        }
      })
      .catch(err => alert("Impossible d'enregistrer cette image : " + err.message))
      .finally(() => card.classList.remove("tag-card-uploading"));
  }

  list.addEventListener("change", (e) => {
    const input = e.target.closest(".js-tag-instant-upload input[type='file']");
    if (!input) return;
    const file = input.files && input.files[0];
    const slot = input.closest(".js-tag-instant-upload");
    const card = input.closest(".org-card-tag");
    const tagId = slot && slot.dataset.tagId;
    if (!file || !card || !tagId) return;
    uploadTagImage(card, tagId, file);
  });

  list.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".js-tag-instant-upload");
    if (!slot) return;
    e.preventDefault();
    slot.classList.add("drag-over");
  });
  list.addEventListener("dragleave", (e) => {
    const slot = e.target.closest(".js-tag-instant-upload");
    if (slot) slot.classList.remove("drag-over");
  });
  list.addEventListener("drop", (e) => {
    const slot = e.target.closest(".js-tag-instant-upload");
    if (!slot) return;
    e.preventDefault();
    slot.classList.remove("drag-over");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    const card = slot.closest(".org-card-tag");
    const tagId = slot.dataset.tagId;
    if (!file || !card || !tagId) return;
    uploadTagImage(card, tagId, file);
  });

  // --- Nom : clic sur le crayon = champ éditable en ligne (Entrée pour
  // valider, Échap pour annuler), plus de boîte de dialogue prompt(). ---
  list.addEventListener("click", (e) => {
    const btn = e.target.closest(".js-tag-inline-rename");
    if (!btn) return;
    e.preventDefault();
    const card = btn.closest(".org-card-tag");
    const nameEl = card.querySelector(".js-tag-name-display");
    if (!nameEl) return;
    const currentName = nameEl.textContent;
    const tagId = btn.dataset.id;

    const input = document.createElement("input");
    input.type = "text";
    input.className = "js-tag-rename-input tag-card-rename-input";
    input.value = currentName;
    nameEl.replaceWith(input);
    input.focus();
    input.select();

    function restore(name) {
      const fresh = document.createElement("strong");
      fresh.className = "js-tag-name-display";
      fresh.textContent = name;
      input.replaceWith(fresh);
    }

    let committed = false;
    function commit() {
      if (committed) return;
      committed = true;
      const newName = input.value.trim();
      if (!newName || newName === currentName) {
        restore(currentName);
        return;
      }
      const lower = newName.toLowerCase();
      const dup = Array.from(list.querySelectorAll(".org-card-tag")).some(c => {
        if (c === card) return false;
        return (c.dataset.name || "") === lower;
      });
      if (dup) {
        alert("Un tag porte déjà ce nom.");
        restore(currentName);
        return;
      }
      input.disabled = true;
      submitEntityAjax(`/tag/${tagId}/rename`, { name: newName })
        .then(res => {
          const finalName = res.name !== undefined ? res.name : newName;
          restore(finalName);
          applyEntityRename("tag", tagId, finalName);
          card.dataset.name = finalName.toLowerCase();
          btn.dataset.name = finalName;
          const deleteBtn = card.querySelector(".js-delete.org-card-delete");
          if (deleteBtn) deleteBtn.dataset.name = finalName;
          showToast("Tag renommé");
        })
        .catch(err => {
          alert("Impossible de renommer ce tag : " + err.message);
          restore(currentName);
        });
    }

    input.addEventListener("keydown", (e2) => {
      if (e2.key === "Enter") { e2.preventDefault(); input.blur(); }
      if (e2.key === "Escape") {
        e2.preventDefault();
        committed = true;
        restore(currentName);
      }
    });
    input.addEventListener("blur", commit);
  });

  // --- Création rapide d'un nouveau tag, sans quitter la fenêtre. ---
  function buildTagCard(id, name) {
    const frag = template.content.cloneNode(true);
    const card = frag.querySelector(".org-card-tag");
    card.dataset.name = name.toLowerCase();
    card.dataset.count = "0";
    const deleteBtn = card.querySelector(".js-delete.org-card-delete");
    deleteBtn.dataset.id = id;
    deleteBtn.dataset.name = name;
    const slot = card.querySelector(".js-tag-instant-upload");
    slot.dataset.tagId = id;
    const placeholder = card.querySelector(".tag-card-placeholder");
    if (placeholder) placeholder.textContent = "🏷 " + name;
    const link = card.querySelector(".tag-card-link");
    link.href = "/tag/" + id;
    card.querySelector(".js-tag-name-display").textContent = name;
    const renameBtn = card.querySelector(".js-tag-inline-rename");
    renameBtn.dataset.id = id;
    renameBtn.dataset.name = name;
    const badge = card.querySelector(".tag-card-count-badge");
    if (badge) badge.textContent = "0";
    return card;
  }

  // --- Compteur "🏷 Tags" du panneau (bouton d'ouverture de cette fenêtre) :
  // recalculé à partir du nombre de cartes réellement affichées, pour rester
  // synchronisé dès qu'un tag est ajouté (création rapide ci-dessous OU
  // création depuis "✎ Modifier les métadonnées", voir syncTagsModalWithVideoTags).
  function updateTagsModalCount() {
    const n = list.querySelectorAll(".org-card-tag").length;
    document.querySelectorAll("#tags-modal-open .count").forEach(el => { el.textContent = String(n); });
    if (countBadge) countBadge.textContent = String(n);
    updateResultCount();
  }

  function findTagCardById(id) {
    const delBtn = list.querySelector(`.js-delete.org-card-delete[data-type="tag"][data-id="${id}"]`);
    return delBtn ? delBtn.closest(".org-card-tag") : null;
  }

  function findTagCardByName(name) {
    const lower = (name || "").trim().toLowerCase();
    if (!lower) return null;
    return list.querySelector(`.org-card-tag[data-name="${CSS.escape(lower)}"]`);
  }

  // Appelée après l'enregistrement du panneau "✎ Modifier les métadonnées"
  // (voir static/js/main.js plus haut, submit de #quick-edit-form et de
  // #video-edit-form) : ajoute immédiatement à cette fenêtre tout tag
  // nouvellement créé depuis ce panneau (mot inédit tapé dans le champ
  // Tags), sans attendre un rechargement de la page. Les tags déjà connus
  // (déjà présents ici) sont ignorés, cette fonction ne fait qu'ajouter ce
  // qui manque. Met aussi à jour, pour la même raison, la liste globale
  // window.__ALL_TAGS__ (résolution d'image par nom dans initTagField) et
  // les datalists d'autocomplétion "Tags" de tous les panneaux d'édition
  // actuellement dans la page, pour qu'un tag tout juste créé soit
  // immédiatement proposé/reconnu partout, sans recharger la page.
  window.syncTagsModalWithVideoTags = function (tags) {
    if (!Array.isArray(tags) || !tags.length) return;
    let added = false;
    if (!Array.isArray(window.__ALL_TAGS__)) window.__ALL_TAGS__ = [];
    tags.forEach(t => {
      if (!t || t.id === undefined || t.id === null) return;

      if (list && !findTagCardById(t.id)) {
        const empty = list.querySelector(".js-tags-modal-empty");
        if (empty) empty.remove();
        const card = buildTagCard(t.id, t.name);
        card.classList.add("tag-card-just-added");
        list.prepend(card);
        added = true;
      }

      const idStr = String(t.id);
      if (!window.__ALL_TAGS__.some(known => String(known.id) === idStr)) {
        window.__ALL_TAGS__.push({
          id: t.id, name: t.name,
          image_url: t.image_url || null,
          count: t.count != null ? t.count : 0
        });
      }

      document.querySelectorAll('datalist[id^="tag-datalist-"]').forEach(dl => {
        const exists = Array.from(dl.options).some(o => o.value.toLowerCase() === t.name.toLowerCase());
        if (!exists) {
          const opt = document.createElement("option");
          opt.value = t.name;
          dl.appendChild(opt);
        }
      });
    });
    if (added) updateTagsModalCount();
  };

  if (createForm) {
    createForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const name = createInput.value.trim();
      if (!name) {
        setCreateHint("Entre un nom pour le tag.", "error");
        createInput.focus();
        return;
      }
      // Doublon : on ne recrée pas, on met en évidence la carte existante
      const existing = findTagCardByName(name);
      if (existing) {
        setCreateHint("Ce tag existe déjà — carte mise en évidence.", "error");
        existing.scrollIntoView({ block: "nearest", behavior: "smooth" });
        existing.classList.add("tag-card-just-added");
        setTimeout(() => existing.classList.remove("tag-card-just-added"), 600);
        createInput.select();
        return;
      }
      setCreateHint("");
      const btn = createForm.querySelector("button[type='submit']");
      if (btn) btn.disabled = true;
      submitEntityAjax("/api/tag/quick_create", { name })
        .then(res => {
          createInput.value = "";
          const empty = list.querySelector(".js-tags-modal-empty");
          if (empty) empty.remove();
          const card = buildTagCard(res.id, res.name);
          card.classList.add("tag-card-just-added");
          list.prepend(card);
          updateTagsModalCount();
          showToast("Tag « " + res.name + " » créé");
          createInput.focus();
        })
        .catch(err => {
          setCreateHint("Impossible de créer ce tag : " + err.message, "error");
        })
        .finally(() => { if (btn) btn.disabled = false; });
    });
    if (createInput) {
      createInput.addEventListener("input", () => setCreateHint(""));
    }
  }

  // Compteurs initiaux
  updateTagsModalCount();
})();

// ============ Ouvrir dans le lecteur externe / emplacement du fichier ============
function openExternal(videoId) {
  fetch(`/open_external/${videoId}`, { method: "POST" })
    .then(r => r.json())
    .then(data => {
      if (!data.ok) {
        alert("Impossible d'ouvrir le fichier : " + (data.error || "erreur inconnue"));
      }
    })
    .catch(() => alert("Impossible de contacter le serveur local."));
}

function openFolder(videoId) {
  fetch(`/open_folder/${videoId}`, { method: "POST" })
    .then(r => r.json())
    .then(data => {
      if (!data.ok) {
        alert("Impossible d'ouvrir l'emplacement du fichier : " + (data.error || "erreur inconnue"));
      }
    })
    .catch(() => alert("Impossible de contacter le serveur local."));
}

// ============ Notification rouge et blanc en haut de l'écran ============
// Utilisée pour la suppression physique du fichier (bouton 🗑, mode lecture)
// ET pour la confirmation "mise à jour effectuée" après l'enregistrement des
// métadonnées en mode lecture (voir plus bas). Le cadre reste blanc/rouge
// (isError=false) ou rouge/blanc (isError=true), et disparaît seul après
// quelques secondes.
function showToast(message, isError) {
  const toast = document.createElement("div");
  toast.className = "delete-toast" + (isError ? " delete-toast-error" : "");
  toast.textContent = message;
  document.body.appendChild(toast);
  // Forcer un reflow avant d'ajouter la classe d'apparition, pour que la
  // transition CSS se joue bien à chaque nouvel appel.
  void toast.offsetWidth;
  toast.classList.add("show");
  setTimeout(() => {
    toast.classList.remove("show");
    setTimeout(() => toast.remove(), 400);
  }, 3200);
}
// Alias conservé pour compatibilité avec le reste du code existant.
function showDeleteToast(message, isError) {
  showToast(message, isError);
}

function advanceAfterDelete(root) {
  const nav = root.querySelector("#player-nav-buttons");
  const nextEl = nav ? nav.querySelector(".player-nav-next") : null;
  if (nextEl && nextEl.tagName === "A") {
    // Réutilise exactement la logique du bouton "Suivant" (respecte le mode
    // 🔀 Aléatoire s'il est actif, sinon suit l'ordre du contexte courant).
    nextEl.click();
  } else {
    // Pas de vidéo suivante : on retourne à l'accueil. Le lecteur (toujours
    // présent dans le DOM, en pause) contient encore la vidéo qu'on vient de
    // supprimer physiquement — sans ce nettoyage, la navigation douce vers
    // "/" le relocaliserait par réflexe dans le mini-lecteur flottant
    // (player-persist.js), qui proposerait alors de relire un fichier qui
    // n'existe plus. On le détruit donc explicitement avant de partir.
    if (window.__destroyPersistedPlayer) window.__destroyPersistedPlayer();
    if (window.__softNavigate) {
      window.__softNavigate("/");
    } else {
      window.location.href = "/";
    }
  }
}

function initVideoDeleteButton(root) {
  root = root || document;
  const btn = root.querySelector("#video-delete-btn");
  if (!btn || btn.dataset.deleteReady) return;
  btn.dataset.deleteReady = "1";

  btn.addEventListener("click", () => {
    const videoId = btn.dataset.videoId;
    if (!videoId) return;
    if (!confirm("⚠ SUPPRESSION DU FICHIER\n\nLe fichier vidéo sera déplacé dans la corbeille Windows.\nCette action est DIFFICILE à annuler.\n\nConfirmer ?")) {
      return;
    }
    btn.disabled = true;

    // Met le lecteur en pause AVANT la suppression, pour que le navigateur
    // arrête d'émettre des requêtes réseau vers le fichier en cours de
    // streaming (utile côté Windows pour éviter le code d'erreur 32,
    // ERROR_SHARING_VIOLATION, si l'OS tente de déplacer le fichier vers la
    // corbeille alors qu'une requête est encore active).
    //
    // Important : on NE vide PAS la source du lecteur ici (pas de
    // removeAttribute("src") / .load() sur une source vide). Ça déclenchait
    // un événement "error" intercepté par le watchdog anti-plantage
    // (static/js/player-smooth.js), qui tentait alors de "récupérer" en
    // rechargeant le lecteur — juste au moment où on l'enchaîne vers la
    // vidéo suivante, causant le bug de lecture après suppression. Le
    // changement de source est intégralement délégué à la navigation
    // "Suivant" ci-dessous (advanceAfterDelete), qui sait déjà le faire
    // proprement (même mécanisme que le bouton "Suivant" normal).
    const player = root.querySelector("#main-player") || document.getElementById("main-player");
    if (player) {
      // Empêche le gardien anti-plantage (player-smooth.js) de tenter une
      // "récupération" si le flux de la vidéo qu'on est en train de
      // supprimer se coupe en cours de route côté serveur (déplacement
      // physique vers la corbeille pendant que le navigateur a encore une
      // requête réseau ouverte dessus) : sans ce drapeau, une erreur
      // réseau tardive pouvait faire recharger l'ANCIENNE vidéo (supprimée)
      // par-dessus la nouvelle, juste après l'enchaînement — c'est ce qui
      // causait le bug de lecture après suppression. Réinitialisé
      // automatiquement dès que la vidéo suivante s'initialise.
      player.dataset.suppressRecovery = "1";
      try { player.pause(); } catch (e) { /* ignore */ }
    }

    setTimeout(() => {
      const body = new FormData();
      body.append("confirm", "1");
      fetch(`/video/${videoId}/delete_file`, { method: "POST", body, cache: "no-store" })
        .then(r => r.json())
        .then(data => {
          if (!data.ok) {
            btn.disabled = false;
            showDeleteToast("Suppression impossible : " + (data.error || "erreur inconnue"), true);
            return;
          }
          showDeleteToast("🗑 Vidéo placée dans la corbeille");
          advanceAfterDelete(root);
        })
        .catch(() => {
          btn.disabled = false;
          showDeleteToast("Impossible de contacter le serveur local.", true);
        });
    }, 300);
  });
}

// ============ "Dossiers de la bibliothèque" (Paramètres) : bouton
// "Parcourir…" → ouvre le sélecteur de dossier natif de Windows côté
// serveur (voir /browse_folder dans app.py) et colle le chemin choisi
// dans le champ texte existant, sans jamais le remplacer par autre chose
// que ce que Windows renvoie. Windows uniquement : sur les autres
// systèmes, le serveur répond une erreur claire et le champ texte reste
// utilisable normalement (rien de cassé). ============
function initFolderBrowse(root) {
  root = root || document;
  const btn = root.querySelector("#browse-folder-btn");
  const input = root.querySelector("#add-folder-input");
  const status = root.querySelector("#browse-folder-status");
  if (!btn || !input || btn.dataset.browseReady) return;
  btn.dataset.browseReady = "1";

  const originalLabel = btn.textContent;

  function setStatus(msg, isError) {
    if (!status) return;
    if (!msg) {
      status.hidden = true;
      status.textContent = "";
      status.classList.remove("browse-folder-status-error");
      return;
    }
    status.textContent = msg;
    status.hidden = false;
    status.classList.toggle("browse-folder-status-error", !!isError);
  }

  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "⏳ Choix du dossier…";
    setStatus("Fenêtre Windows ouverte — clique sur le dossier voulu, puis sur \u00abSélectionner un dossier\u00bb.", false);

    const body = new FormData();
    body.append("start", input.value || "");

    fetch("/browse_folder", { method: "POST", body, cache: "no-store" })
      .then(r => r.json())
      .then(data => {
        if (data.cancelled) {
          setStatus("", false);
          return;
        }
        if (!data.ok) {
          setStatus(data.error || "Impossible d'ouvrir la fenêtre Windows.", true);
          return;
        }
        input.value = data.path;
        input.focus();
        setStatus("", false);
      })
      .catch(() => setStatus("Impossible de contacter le serveur local.", true))
      .finally(() => {
        btn.disabled = false;
        btn.textContent = originalLabel;
      });
  });
}

// ============ Widget de notation par étoiles (1 à 5) ============
function initStarRatingInputs(root) {
  root = root || document;
  root.querySelectorAll(".star-rating-input").forEach(widget => {
    if (widget.dataset.starReady) return;
    widget.dataset.starReady = "1";
    const hidden = widget.querySelector("input[type='hidden']");
    const stars = Array.from(widget.querySelectorAll(".star-choice"));
    const clearBtn = widget.querySelector(".star-clear");
    const valueLabel = widget.querySelector(".js-star-value");

    function paint(value) {
      stars.forEach(s => {
        s.classList.toggle("filled", parseInt(s.dataset.star, 10) <= value);
      });
      if (valueLabel) valueLabel.textContent = value > 0 ? `${value}/5` : "—";
    }

    paint(parseInt(hidden.value, 10) || 0);

    stars.forEach(star => {
      star.addEventListener("click", () => {
        const value = parseInt(star.dataset.star, 10);
        hidden.value = value;
        paint(value);
        hidden.dispatchEvent(new Event("change", { bubbles: true }));
      });
      star.addEventListener("mouseenter", () => paint(parseInt(star.dataset.star, 10)));
    });
    widget.addEventListener("mouseleave", () => paint(parseInt(hidden.value, 10) || 0));
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        hidden.value = 0;
        paint(0);
        hidden.dispatchEvent(new Event("change", { bubbles: true }));
      });
      clearBtn.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          hidden.value = 0;
          paint(0);
          hidden.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    }
  });
}

// ============ Filtrage / tri des grilles Catégories & Playlists (page Organiser) ============
function initOrganizeGrids(root) {
  root = root || document;
  root.querySelectorAll(".organize-toolbar").forEach(toolbar => {
    if (toolbar.dataset.orgToolbarReady) return;
    toolbar.dataset.orgToolbarReady = "1";
    const filterInput = toolbar.querySelector(".org-filter-input");
    const ratingFilter = toolbar.querySelector(".org-rating-filter");
    const sortSelect = toolbar.querySelector(".org-sort-select");
    // Champs optionnels (2ème case de filtre) : date de création, présents
    // uniquement quand le macro toolbar() a été appelé avec show_created=true
    // (ex : liste des sous-playlists). Absents ailleurs, donc sans effet.
    const createdFromFilter = toolbar.querySelector(".org-created-filter-from");
    const createdToFilter = toolbar.querySelector(".org-created-filter-to");
    // 2ème case de filtre (photo / commentaire) et 2ème case de tri (date de
    // création dédiée) : présentes uniquement pour la liste des sous-playlists
    // (macro toolbar() appelé avec show_created=true). Absentes ailleurs,
    // donc sans effet sur les autres listes (catégories, playlists).
    const photoFilter = toolbar.querySelector(".org-photo-filter");
    const commentFilter = toolbar.querySelector(".org-comment-filter");
    const createdSortSelect = toolbar.querySelector(".org-created-sort-select");
    const listId = (filterInput || ratingFilter || sortSelect || {}).dataset
      ? (filterInput || ratingFilter || sortSelect).dataset.list
      : null;
    const list = listId ? root.querySelector("#" + CSS.escape(listId)) : null;
    if (!list) return;

    // Mémorisation (localStorage, par liste) du filtre nom/note et du tri
    // déjà choisis, pour qu'un aller-retour sur la page (ou une nouvelle
    // visite) les retrouve tels quels au lieu de repartir à zéro.
    const storageKey = "mediatheque_toolbar_" + listId;

    function loadSavedState() {
      try {
        return JSON.parse(localStorage.getItem(storageKey)) || {};
      } catch (e) {
        return {};
      }
    }

    function saveState() {
      try {
        localStorage.setItem(storageKey, JSON.stringify({
          name: filterInput ? filterInput.value : "",
          rating: ratingFilter ? ratingFilter.value : "0",
          sort: sortSelect ? sortSelect.value : "",
          createdFrom: createdFromFilter ? createdFromFilter.value : "",
          createdTo: createdToFilter ? createdToFilter.value : "",
          photo: photoFilter ? photoFilter.value : "0",
          comment: commentFilter ? commentFilter.value : "0",
          createdSort: createdSortSelect ? createdSortSelect.value : "",
        }));
      } catch (e) { /* localStorage indisponible : on continue sans mémoriser */ }
    }

    // Le filtre texte (nom), le filtre par note et le filtre par date de
    // création (quand présent) s'appliquent ensemble, indépendamment du tri
    // qui ne fait que réordonner les cartes visibles.
    function applyFilters() {
      const term = filterInput ? filterInput.value.trim().toLowerCase() : "";
      const ratingValue = ratingFilter ? ratingFilter.value : "0";
      const createdFrom = createdFromFilter ? createdFromFilter.value : "";
      const createdTo = createdToFilter ? createdToFilter.value : "";
      const photoValue = photoFilter ? photoFilter.value : "0";
      const commentValue = commentFilter ? commentFilter.value : "0";
      list.querySelectorAll(".org-card").forEach(card => {
        const matchName = !term || card.dataset.name.includes(term);
        const cardRating = parseInt(card.dataset.rating, 10) || 0;
        let matchRating = true;
        if (ratingValue === "none") matchRating = cardRating === 0;
        else if (ratingValue !== "0") matchRating = cardRating >= parseInt(ratingValue, 10);
        let matchCreated = true;
        if (createdFrom || createdTo) {
          const cardCreated = (card.dataset.created || "").slice(0, 10);
          if (!cardCreated) {
            matchCreated = false;
          } else {
            if (createdFrom && cardCreated < createdFrom) matchCreated = false;
            if (createdTo && cardCreated > createdTo) matchCreated = false;
          }
        }
        let matchPhoto = true;
        if (photoValue === "yes") matchPhoto = card.dataset.hasPhoto === "1";
        else if (photoValue === "no") matchPhoto = card.dataset.hasPhoto === "0";
        let matchComment = true;
        if (commentValue === "yes") matchComment = card.dataset.hasComment === "1";
        else if (commentValue === "no") matchComment = card.dataset.hasComment === "0";
        card.style.display = (matchName && matchRating && matchCreated && matchPhoto && matchComment) ? "" : "none";
      });
    }

    function applySort(sortValue) {
      if (!sortValue) return;
      const [key, dir] = sortValue.split("-");
      const cards = Array.from(list.querySelectorAll(".org-card"));
      cards.sort((a, b) => {
        let va, vb;
        if (key === "name" || key === "created") {
          va = key === "created" ? (a.dataset.created || "") : a.dataset.name;
          vb = key === "created" ? (b.dataset.created || "") : b.dataset.name;
        } else {
          va = parseFloat(a.dataset[key]);
          vb = parseFloat(b.dataset[key]);
        }
        if (va < vb) return dir === "asc" ? -1 : 1;
        if (va > vb) return dir === "asc" ? 1 : -1;
        return 0;
      });
      cards.forEach(card => list.appendChild(card));
    }

    // Permet à applyEntityRename() de redemander ce même tri juste après un
    // renommage (voir plus haut), pour que la carte reprenne sa place tout
    // de suite si le tri actif dépend du nom — sans effet sinon.
    list.__resortCurrent = function () {
      if (sortSelect && sortSelect.value) applySort(sortSelect.value);
    };

    // Restauration au chargement de la page des derniers filtres/tri
    // mémorisés pour cette liste précise.
    const saved = loadSavedState();
    if (filterInput && saved.name) filterInput.value = saved.name;
    if (ratingFilter && saved.rating) ratingFilter.value = saved.rating;
    if (sortSelect && saved.sort) sortSelect.value = saved.sort;
    if (createdFromFilter && saved.createdFrom) createdFromFilter.value = saved.createdFrom;
    if (createdToFilter && saved.createdTo) createdToFilter.value = saved.createdTo;
    if (photoFilter && saved.photo) photoFilter.value = saved.photo;
    if (commentFilter && saved.comment) commentFilter.value = saved.comment;
    if (createdSortSelect && saved.createdSort) createdSortSelect.value = saved.createdSort;
    if (saved.name || (saved.rating && saved.rating !== "0") || saved.createdFrom || saved.createdTo
      || (saved.photo && saved.photo !== "0") || (saved.comment && saved.comment !== "0")) applyFilters();
    if (saved.sort) applySort(saved.sort);
    if (saved.createdSort) applySort(saved.createdSort);

    if (filterInput) filterInput.addEventListener("input", () => { applyFilters(); saveState(); });
    if (ratingFilter) ratingFilter.addEventListener("change", () => { applyFilters(); saveState(); });
    if (createdFromFilter) createdFromFilter.addEventListener("change", () => { applyFilters(); saveState(); });
    if (createdToFilter) createdToFilter.addEventListener("change", () => { applyFilters(); saveState(); });
    if (photoFilter) photoFilter.addEventListener("change", () => { applyFilters(); saveState(); });
    if (commentFilter) commentFilter.addEventListener("change", () => { applyFilters(); saveState(); });
    if (sortSelect) {
      sortSelect.addEventListener("change", () => {
        applySort(sortSelect.value);
        saveState();
      });
    }
    if (createdSortSelect) {
      createdSortSelect.addEventListener("change", () => {
        if (createdSortSelect.value) applySort(createdSortSelect.value);
        saveState();
      });
    }
  });
}

// ============ Aperçu au survol (images qui défilent) ============
// Un seul aperçu actif à la fois + intervalle plus lent = pas de tempête
// de GET /thumb/..._pN.jpg pendant la lecture (cf. logs).
let __activeHoverPreview = null;
function initHoverPreviews(root) {
  root = root || document;
  const INTERVAL_MS = 700;
  root.querySelectorAll(".thumb-wrap[data-preview-base]").forEach(wrap => {
    if (wrap.dataset.hoverPreviewReady) return;
    wrap.dataset.hoverPreviewReady = "1";
    const base = wrap.dataset.previewBase;
    const count = parseInt(wrap.dataset.previewCount, 10) || 0;
    const img = wrap.querySelector("img.thumb-img");
    if (!img || count < 1) return;

    const originalSrc = img.src;
    let frameIndex = 0;
    let timer = null;

    function stop() {
      if (timer) clearInterval(timer);
      timer = null;
      if (__activeHoverPreview === wrap) __activeHoverPreview = null;
      img.src = originalSrc;
    }

    function nextFrame() {
      if (window.__softNavBusy) { stop(); return; }
      frameIndex = (frameIndex % count) + 1;
      img.src = `/thumb/${base}_p${frameIndex}.jpg`;
    }

    wrap.addEventListener("mouseenter", () => {
      if (window.__softNavBusy) return;
      // Stoppe tout autre aperçu en cours (1 seul flux de miniatures)
      if (__activeHoverPreview && __activeHoverPreview !== wrap) {
        try { __activeHoverPreview.dispatchEvent(new Event("mouseleave")); } catch (e) {}
      }
      __activeHoverPreview = wrap;
      frameIndex = 0;
      if (timer) clearInterval(timer);
      timer = setInterval(nextFrame, INTERVAL_MS);
    });
    wrap.addEventListener("mouseleave", stop);
  });
}

// ============ Robustesse des miniatures "À découvrir aussi" ============
// Complément du forçage ci-dessus (chargement immédiat, non "lazy") : si
// malgré tout une miniature ne se charge pas (aléas réseau/cache), on
// retente une fois avec une URL "cache-bustée", puis, en dernier recours,
// on bascule proprement sur le pictogramme de remplacement plutôt que de
// laisser une case vide/cassée à l'écran.
function initRelatedThumbFallback(root) {
  root = root || document;
  root.querySelectorAll(".related-thumb img.thumb-img").forEach(img => {
    if (img.dataset.thumbFallbackReady) return;
    img.dataset.thumbFallbackReady = "1";
    let retried = false;
    img.addEventListener("error", () => {
      if (!retried) {
        retried = true;
        const url = new URL(img.src, window.location.href);
        url.searchParams.set("_retry", Date.now());
        img.src = url.toString();
        return;
      }
      const wrap = img.closest(".thumb-wrap");
      if (!wrap) return;
      img.remove();
      const placeholder = document.createElement("div");
      placeholder.className = "thumb-placeholder";
      placeholder.textContent = "🎞";
      wrap.prepend(placeholder);
    });
  });
}

// ============ Onglets Catégories / Playlists du panneau ☰ : une seule
// section affichée à la fois (jamais côte à côte), le choix est mémorisé
// (localStorage) et réappliqué à la réouverture. Rattaché à
// initDynamicPage (voir plus bas) : le panneau ☰ fait partie de ".layout"
// et est donc entièrement recréé à chaque rafraîchissement en temps réel
// (édition de métadonnées, scan...) — sans ce rattachement, les onglets
// cessaient de répondre au clic après le premier rafraîchissement. ============
function initSidebarTabs(root) {
  const wrap = document.getElementById("sidebar-sections");
  const tabs = document.getElementById("sidebar-tabs");
  if (!wrap || !tabs) return;

  const STORAGE_KEY = "mediatheque_sidebar_panel";
  const buttons = Array.from(tabs.querySelectorAll(".sidebar-tab-btn"));

  function setPanel(panel) {
    if (panel !== "category" && panel !== "playlist") panel = "category";
    wrap.setAttribute("data-active-panel", panel);
    buttons.forEach((b) => b.classList.toggle("active", b.dataset.panel === panel));
    try { localStorage.setItem(STORAGE_KEY, panel); } catch (e) { /* localStorage indisponible : le choix ne sera pas mémorisé */ }
  }

  buttons.forEach((b) => {
    b.addEventListener("click", () => setPanel(b.dataset.panel));
  });

  let saved = null;
  try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) { /* ignore */ }
  setPanel(saved || wrap.getAttribute("data-active-panel") || "category");
}

// ============ Thème clair/sombre ============
(function initSidebarToggle() {
  const btn = document.getElementById("sidebar-toggle-btn");
  if (!btn) return;

  function isOpen() { return document.body.classList.contains("sidebar-open"); }
  function setOpen(open) {
    document.body.classList.toggle("sidebar-open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  btn.addEventListener("click", () => setOpen(!isOpen()));
  // Le fond assombri (#sidebar-backdrop) vit dans .layout, qui peut être
  // remplacé par une navigation instantanée (lecture vidéo en cours, voir
  // player-persist.js) : délégation sur document plutôt qu'un binding
  // direct, pour rester fonctionnel après un remplacement du panneau.
  document.addEventListener("click", (e) => {
    if (e.target && e.target.id === "sidebar-backdrop") setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) setOpen(false);
  });
})();

(function initThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;

  // 3 thèmes disponibles, dans l'ordre de bascule au clic. "light" (thème
  // clair par défaut, bleu marine + blanc) n'a pas d'attribut data-theme
  // (voir :root dans style.css) ; "dark" et "turquoise" en ont un.
  const THEMES = ["light", "dark", "turquoise"];
  const LABELS = { light: "🌙 Thème", dark: "🌊 Thème", turquoise: "☀ Thème" };

  function current() {
    return document.documentElement.getAttribute("data-theme") || "light";
  }
  function applyLabel() {
    btn.textContent = LABELS[current()] || "🌙 Thème";
  }
  applyLabel();
  btn.addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(current()) + 1) % THEMES.length];
    if (next === "light") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", next);
    }
    try { localStorage.setItem("mediatheque_theme", next); } catch (e) { /* localStorage indisponible : le thème reste actif pour cette page seulement */ }
    applyLabel();
  });
})();

// Densité UI compacte (gaps/paddings uniquement — ne touche pas aux
// minmax des grilles vidéo ni au nombre de colonnes / ratio des
// sous-playlists). Mémorisée dans localStorage.
(function initCompactToggle() {
  const btn = document.getElementById("compact-toggle");
  if (!btn) return;
  const KEY = "mediatheque_ui_compact";

  function isCompact() {
    return document.body.classList.contains("ui-compact");
  }
  function applyLabel() {
    const on = isCompact();
    btn.textContent = on ? "⊟ Normal" : "⊞ Dense";
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.title = on
      ? "Repasser en densité normale"
      : "Densité compacte (moins d'espaces, cases inchangées)";
  }
  applyLabel();
  btn.addEventListener("click", () => {
    const next = !isCompact();
    document.body.classList.toggle("ui-compact", next);
    try {
      localStorage.setItem(KEY, next ? "1" : "0");
    } catch (e) { /* localStorage indisponible : l'état reste actif pour cette page seulement */ }
    applyLabel();
  });
})();

// Bascule d'affichage page playlist principale : sous-playlists / vidéos /
// les deux. Mémorisée dans localStorage. CSS : body[data-playlist-view=...].
(function initPlaylistViewToggle() {
  const nav = document.getElementById("playlist-view-toggle");
  if (!nav) return;
  const KEY = "mediatheque_playlist_view";
  const VALID = { subs: 1, videos: 1, both: 1 };
  const buttons = Array.from(nav.querySelectorAll("button[data-view]"));

  function apply(mode) {
    if (!VALID[mode]) mode = "both";
    document.body.setAttribute("data-playlist-view", mode);
    buttons.forEach((btn) => {
      const on = btn.getAttribute("data-view") === mode;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  let saved = "both";
  try {
    const v = localStorage.getItem(KEY);
    if (v && VALID[v]) saved = v;
  } catch (e) { /* ignore */ }
  apply(saved);

  nav.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-view]");
    if (!btn || !nav.contains(btn)) return;
    const mode = btn.getAttribute("data-view");
    if (!VALID[mode]) return;
    apply(mode);
    try {
      localStorage.setItem(KEY, mode);
    } catch (e) { /* localStorage indisponible */ }
  });
})();

// ============ Suggestions de recherche façon Google ============
// Fonction générique (ré-utilisée pour la barre de recherche du bandeau ET
// pour le champ "Recherche" du panneau "Filtrer par...") : chaque appel
// gère sa propre paire input/box, indépendamment des autres.
function setupSearchSuggestions(input, box, signal) {
  if (!input || !box) return;
  var listenerOpts = signal ? { signal: signal } : undefined;

  let debounceTimer = null;
  let activeIndex = -1;
  let items = [];

  // Ordre d'affichage des groupes, et libellé/icône de secours (utilisée
  // seulement quand l'entrée n'a pas de photo de couverture, ex. vidéo,
  // ou catégorie/playlist sans image).
  const TYPE_META = {
    video:       { icon: "🎬", label: "Vidéos" },
    category:    { icon: "🗂", label: "Catégories" },
    playlist:    { icon: "📁", label: "Playlists" },
    subplaylist: { icon: "📁", label: "Sous-playlists" },
  };
  const GROUP_ORDER = ["video", "category", "playlist", "subplaylist"];

  function render(results) {
    box.innerHTML = "";
    activeIndex = -1;
    if (results.length === 0) {
      items = [];
      box.classList.remove("visible");
      return;
    }

    // Regroupe par type puis affiche chaque groupe sous son propre titre.
    // "ordered" reconstruit la liste dans l'ORDRE D'AFFICHAGE réel : c'est
    // cette liste (et non "results" telle que reçue) qui doit servir à la
    // navigation clavier, pour que l'index d'un élément DOM corresponde
    // toujours au bon élément de "items".
    const groups = {};
    results.forEach(r => { (groups[r.type] = groups[r.type] || []).push(r); });
    const ordered = [];

    GROUP_ORDER.filter(type => groups[type] && groups[type].length).forEach(type => {
      const meta = TYPE_META[type];
      const heading = document.createElement("div");
      heading.className = "suggestion-group-heading";
      heading.textContent = meta.label;
      box.appendChild(heading);

      groups[type].forEach(r => {
        const index = ordered.length;
        ordered.push(r);

        const el = document.createElement("a");
        el.href = r.url;
        el.className = "suggestion-item";
        if (r.image_url) {
          const img = document.createElement("img");
          img.className = "suggestion-item-thumb";
          img.src = r.image_url;
          img.alt = "";
          el.appendChild(img);
        } else {
          const icon = document.createElement("span");
          icon.className = "suggestion-item-icon";
          icon.textContent = meta.icon;
          el.appendChild(icon);
        }
        const label = document.createElement("span");
        label.className = "suggestion-item-label";
        label.textContent = r.title;
        el.appendChild(label);
        el.addEventListener("mouseenter", () => setActive(index));
        box.appendChild(el);
      });
    });

    items = ordered;
    box.classList.add("visible");
  }

  function setActive(i) {
    const els = box.querySelectorAll(".suggestion-item");
    els.forEach(el => el.classList.remove("active"));
    if (i >= 0 && i < els.length) {
      els[i].classList.add("active");
      activeIndex = i;
    }
  }

  let suggestAbort = null;
  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < 2) {
      box.classList.remove("visible");
      if (suggestAbort) { try { suggestAbort.abort(); } catch (e) {} suggestAbort = null; }
      return;
    }
    debounceTimer = setTimeout(() => {
      if (suggestAbort) { try { suggestAbort.abort(); } catch (e) {} }
      suggestAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
      const opts = { cache: "no-store" };
      if (suggestAbort) opts.signal = suggestAbort.signal;
      fetch(`/api/search_suggestions?q=${encodeURIComponent(q)}`, opts)
        .then(r => r.json())
        .then(render)
        .catch(err => {
          if (err && err.name === "AbortError") return;
        });
    }, 250);
  }, listenerOpts);

  input.addEventListener("keydown", (e) => {
    const els = box.querySelectorAll(".suggestion-item");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(activeIndex + 1, els.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(activeIndex - 1, 0));
    } else if (e.key === "Enter" && activeIndex >= 0 && items[activeIndex]) {
      e.preventDefault();
      window.location.href = items[activeIndex].url;
    } else if (e.key === "Escape") {
      box.classList.remove("visible");
    }
  }, listenerOpts);

  // IMPORTANT : ce listener est posé sur "document" (pour détecter un clic
  // n'importe où en dehors de la boîte de suggestions), et non sur "input"
  // ou "box" eux-mêmes. Sans le signal d'annulation ci-dessus, il
  // s'accumulerait sans jamais être retiré : voir initFilterSearchSuggestions
  // ci-dessous, appelée à CHAQUE navigation instantanée (précédent/suivant,
  // changement de page...), qui recrée un "input"/"box" tout neufs à chaque
  // fois (le garde-fou "dataset.suggestReady" ne protège que contre un
  // double-appel sur le MÊME élément, jamais contre l'accumulation d'un
  // nouveau listener global à chaque navigation). Résultat sans ce signal :
  // chaque clic sur "Précédent"/"Suivant" pendant une lecture vidéo en
  // cours ajoutait un listener "document" supplémentaire référençant une
  // ancienne boîte de suggestions déjà détachée du DOM — après quelques
  // pages parcourues, l'application accumulait des dizaines de listeners
  // fantômes exécutés à chaque clic, ce qui la rendait de plus en plus
  // lente puis la faisait sembler "planter". Le signal permet d'annuler
  // proprement l'ancien listener avant d'en poser un nouveau.
  document.addEventListener("click", (e) => {
    if (!box.contains(e.target) && e.target !== input) {
      box.classList.remove("visible");
    }
  }, listenerOpts);
}

// La barre de recherche du bandeau vit en dehors de .layout : elle ne
// change jamais après une navigation instantanée, une seule initialisation
// suffit pour toute la durée de vie de la page.
setupSearchSuggestions(
  document.getElementById("search-input"),
  document.getElementById("search-suggestions")
);

// Le champ "Recherche" du panneau "Filtrer par..." (pages Toutes les
// vidéos / catégorie / playlist), lui, vit dans .content et doit être
// ré-initialisé après chaque navigation instantanée.
//
// Contrairement au champ de recherche du bandeau du haut (setupSearchSuggestions
// appelée une seule fois, plus haut, pour toute la durée de vie de la page),
// cette fonction est réexécutée à CHAQUE navigation instantanée (précédent/
// suivant, changement de page, de tri, de filtre...) avec un "input"/"box"
// tout neufs à chaque fois. setupSearchSuggestions pose un listener de clic
// sur "document" (pour fermer la boîte de suggestions au clic extérieur) :
// sans annulation explicite de l'ancien avant d'en poser un nouveau, ces
// listeners s'accumulaient indéfiniment au fil des navigations (le
// garde-fou "dataset.suggestReady" ne protège que le MÊME élément, jamais
// les navigations suivantes qui en recréent un autre) — c'était la cause
// du plantage/ralentissement progressif en cliquant plusieurs fois de
// suite sur "Précédent"/"Suivant" dans la grille de vidéos.
let __filterSuggestAbort = new AbortController();
function initFilterSearchSuggestions(root) {
  root = root || document;
  const input = root.querySelector("#filter-search-input");
  const box = root.querySelector("#filter-search-suggestions");
  if (!input) return;
  __filterSuggestAbort.abort();
  __filterSuggestAbort = new AbortController();
  setupSearchSuggestions(input, box, __filterSuggestAbort.signal);
}

// ============ Filtrage dynamique (recherche, catégorie, tag, playlist,
// note, durée, résolution, dates, commentaire) : le formulaire se soumet
// automatiquement dès qu'un filtre change, sans avoir à cliquer sur
// "Filtrer". Le bouton reste disponible mais n'est plus nécessaire. ============
function initDynamicFilters(root) {
  root = root || document;
  const form = root.querySelector(".advanced-fields");
  if (!form || form.dataset.dynFiltersReady) return;
  form.dataset.dynFiltersReady = "1";

  // Délai avant soumission après la dernière frappe. Volontairement long
  // pour "Recherche" (texte libre, souvent plusieurs mots) afin de ne
  // jamais couper l'utilisateur en pleine saisie ; plus court pour les
  // champs numériques (durée min/max), généralement saisis d'un coup.
  const TEXT_DEBOUNCE_MS = 1200;
  const NUMBER_DEBOUNCE_MS = 700;
  const FOCUS_KEY = "mediatheque_filter_focus";
  let debounceTimer = null;

  // Si la page vient de se recharger suite à une soumission auto pendant
  // la frappe, on rend immédiatement le focus (et la position du curseur)
  // au champ concerné : la frappe peut reprendre exactement là où elle
  // s'est arrêtée, sans avoir à recliquer dans le champ.
  try {
    const saved = JSON.parse(sessionStorage.getItem(FOCUS_KEY) || "null");
    if (saved && saved.name) {
      const el = form.querySelector(`[name="${saved.name}"]`);
      if (el) {
        el.focus();
        if (typeof saved.pos === "number" && el.setSelectionRange) {
          el.setSelectionRange(saved.pos, saved.pos);
        }
      }
    }
  } catch (e) { /* sessionStorage indisponible : tant pis, pas bloquant */ }
  sessionStorage.removeItem(FOCUS_KEY);

  function submitNow(activeEl) {
    clearTimeout(debounceTimer);
    if (activeEl) {
      try {
        sessionStorage.setItem(FOCUS_KEY, JSON.stringify({ name: activeEl.name, pos: activeEl.selectionStart }));
      } catch (e) { /* pas bloquant si indisponible */ }
    }
    form.submit();
  }

  function submitDebounced(el, delay) {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => submitNow(el), delay);
  }

  form.querySelectorAll("select, input[type='checkbox'], input[type='date']").forEach(el => {
    el.addEventListener("change", () => submitNow());
  });

  form.querySelectorAll("input[type='text']").forEach(el => {
    el.addEventListener("input", () => submitDebounced(el, TEXT_DEBOUNCE_MS));
  });

  form.querySelectorAll("input[type='number']").forEach(el => {
    el.addEventListener("input", () => submitDebounced(el, NUMBER_DEBOUNCE_MS));
  });
}

// ============ 2ème case de filtre / 2ème case de tri : date de la vidéo
// (page "Toutes les vidéos", catégorie, playlist, tag) ============
// Appliqué CÔTÉ SERVEUR sur toute la liste (pas seulement les cartes de
// la page courante) : pagination et ordre globaux corrects. Voir
// initVideoDateQuickTools() plus bas.
// ============ Repli par défaut + mémorisation (localStorage) des volets
// "filtre/tri/recherche" ============
// Concerne le volet de filtre avancé (.advanced-search — vues catégorie,
// tag, playlist principale) et le volet de filtre/tri rapide par date
// (#video-date-quickbar-details — présent soit seul (.quickbar-details),
// soit fusionné avec le filtre avancé en mode "barre d'outils unifiée",
// voir unified_toolbar/.content-toolbar-filters dans _video_grid.html).
// Ces volets restent repliés par défaut (comme avant, aucun attribut
// "open" n'est posé côté template), mais l'état ouvert/fermé choisi par
// l'utilisateur est désormais mémorisé par TYPE de page (accueil /
// catégorie / playlist / tag), sur le même principe que le reste du
// projet (voir initSidebarTabs, initVideoDateQuickTools plus bas) :
// aucun champ, id, classe ni comportement existant n'est modifié, seul
// l'état initial ouvert/fermé du <details> peut changer au chargement.
function initFilterPanelsCollapseMemory(root) {
  root = root || document;

  function pageScope() {
    if (location.pathname.startsWith("/playlist/")) return "playlist";
    if (location.pathname.startsWith("/category/")) return "category";
    if (location.pathname.startsWith("/tag/")) return "tag";
    return "index";
  }
  const scope = pageScope();

  function attach(el, storageKey) {
    if (!el || el.dataset.collapseMemoryReady === "1") return;
    el.dataset.collapseMemoryReady = "1";
    let saved = null;
    try { saved = localStorage.getItem(storageKey); } catch (e) { /* ignore */ }
    // Repliée par défaut : on ne l'ouvre que si l'utilisateur l'avait
    // explicitement laissée ouverte lors de sa dernière visite sur ce
    // type de vue.
    if (saved === "1") el.open = true;
    else if (saved === "0") el.open = false;
    el.addEventListener("toggle", () => {
      try { localStorage.setItem(storageKey, el.open ? "1" : "0"); } catch (e) { /* localStorage indisponible : le choix ne sera pas mémorisé */ }
    });
  }

  attach(root.querySelector(".advanced-search"), "mediatheque_filters_advanced_open_" + scope);
  attach(root.querySelector("#video-date-quickbar-details"), "mediatheque_filters_quickbar_open_" + scope);
}

function initVideoDateQuickTools(root) {
  root = root || document;
  const quickbar = root.querySelector("#video-date-quickbar");
  if (!quickbar || quickbar.dataset.quickToolsReady) return;
  quickbar.dataset.quickToolsReady = "1";

  const fromInput = root.querySelector("#video-date-filter-from");
  const toInput = root.querySelector("#video-date-filter-to");
  const quickSort = root.querySelector("#video-date-quick-sort");

  // Mémorisation locale (complément à la session serveur) par type de page.
  function pageScope() {
    if (location.pathname.startsWith("/playlist/")) return "playlist";
    if (location.pathname.startsWith("/category/")) return "category";
    if (location.pathname.startsWith("/tag/")) return "tag";
    return "index";
  }
  const storageKey = "mediatheque_video_date_quickbar_" + pageScope();

  function saveState() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({
        from: fromInput ? fromInput.value : "",
        to: toInput ? toInput.value : "",
        sortDir: quickSort ? quickSort.value : "",
      }));
    } catch (e) { /* localStorage indisponible */ }
  }

  if (new URLSearchParams(location.search).get("reset")) {
    try { localStorage.removeItem(storageKey); } catch (e) { /* pas bloquant */ }
  }

  // Synchronise le sélecteur de tri rapide avec le tri serveur actuel
  // (déjà injecté en value/selected dans le HTML, on complète depuis l'URL).
  const urlParams = new URLSearchParams(location.search);
  const currentSort = urlParams.get("sort") || quickbar.dataset.currentSort || "";
  if (quickSort) {
    if (currentSort === "video_date") quickSort.value = "desc";
    else if (currentSort === "video_date_asc") quickSort.value = "asc";
  }

  /**
   * Navigation serveur : applique date_from / date_to / sort sur TOUTE la
   * liste, repart à la page 1. Préserve les autres filtres (q, category…).
   * Utilise la navigation douce si un lecteur est actif, sinon rechargement.
   */
  function navigateWithQuickParams(opts) {
    const params = new URLSearchParams(location.search);
    // Toujours repartir à la page 1 : le tri/filtre change l'ensemble
    // ordonné, garder page=3 n'aurait aucun sens.
    params.delete("page");
    params.set("page", "1");
    params.delete("reset");

    if (opts.from !== undefined) {
      if (opts.from) params.set("date_from", opts.from);
      else params.delete("date_from");
    }
    if (opts.to !== undefined) {
      if (opts.to) params.set("date_to", opts.to);
      else params.delete("date_to");
    }
    if (opts.sortDir !== undefined) {
      if (opts.sortDir === "desc") params.set("sort", "video_date");
      else if (opts.sortDir === "asc") params.set("sort", "video_date_asc");
      else if (opts.sortDir === "") {
        // Désactivation du tri rapide : si le tri courant était une date
        // vidéo, on revient à "recent" (ou position sur playlist).
        const s = params.get("sort") || currentSort;
        if (s === "video_date" || s === "video_date_asc") {
          if (location.pathname.startsWith("/playlist/")) params.set("sort", "position");
          else params.set("sort", "recent");
        }
      }
    }

    const qs = params.toString();
    const url = location.pathname + (qs ? "?" + qs : "");
    saveState();

    if (window.__softNavigate) {
      window.__softNavigate(url);
    } else {
      window.location.href = url;
    }
  }

  if (fromInput) {
    fromInput.addEventListener("change", () => {
      navigateWithQuickParams({
        from: fromInput.value,
        to: toInput ? toInput.value : undefined,
      });
    });
  }
  if (toInput) {
    toInput.addEventListener("change", () => {
      navigateWithQuickParams({
        from: fromInput ? fromInput.value : undefined,
        to: toInput.value,
      });
    });
  }
  if (quickSort) {
    quickSort.addEventListener("change", () => {
      navigateWithQuickParams({ sortDir: quickSort.value });
    });
  }
}

// ============ Sélection multiple + édition en masse ============
function initBulkSelection(root) {
  root = root || document;
  const grid = root.querySelector("#video-grid");
  const selectBtn = root.querySelector("#select-mode-btn");
  const bulkBar = root.querySelector("#bulk-bar");
  const cancelBtn = root.querySelector("#bulk-cancel");
  const countLabel = root.querySelector("#bulk-count-num");
  if (!grid || !selectBtn || !bulkBar || selectBtn.dataset.bulkReady) return;
  selectBtn.dataset.bulkReady = "1";

  let selectMode = false;

  function updateCount() {
    const checked = grid.querySelectorAll(".select-checkbox:checked");
    countLabel.textContent = checked.length;
    bulkBar.classList.toggle("visible", checked.length > 0);
  }

  selectBtn.addEventListener("click", () => {
    selectMode = !selectMode;
    grid.classList.toggle("select-mode", selectMode);
    selectBtn.classList.toggle("active", selectMode);
    if (!selectMode) {
      grid.querySelectorAll(".select-checkbox").forEach(cb => cb.checked = false);
      updateCount();
    }
  });

  grid.addEventListener("change", (e) => {
    if (e.target.classList.contains("select-checkbox")) updateCount();
  });

  cancelBtn?.addEventListener("click", () => {
    grid.querySelectorAll(".select-checkbox").forEach(cb => cb.checked = false);
    updateCount();
  });

  bulkBar.addEventListener("submit", (e) => {
    // Injecte les vidéos cochées comme champs cachés avant l'envoi
    bulkBar.querySelectorAll("input[name='video_id']").forEach(el => el.remove());
    const checked = grid.querySelectorAll(".select-checkbox:checked");
    if (checked.length === 0) {
      e.preventDefault();
      return;
    }
    checked.forEach(cb => {
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "video_id";
      hidden.value = cb.dataset.videoId;
      bulkBar.appendChild(hidden);
    });
  });
}

// ============ Vitesse de lecture ============
function initSpeedControls(root) {
  root = root || document;
  const player = root.querySelector("#main-player");
  const buttons = root.querySelectorAll(".speed-btn");
  if (!player || buttons.length === 0) return;

  buttons.forEach(btn => {
    if (btn.dataset.speedReady) return;
    btn.dataset.speedReady = "1";
    btn.addEventListener("click", () => {
      const speed = parseFloat(btn.dataset.speed);
      player.playbackRate = speed;
      buttons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });
}

// ============ Aperçu instantané de la photo de couverture ============
// Concerne les 2 types d'entités (catégorie ET playlist), sur la page
// "Organiser" comme sur les pages dédiées catégorie/playlist : avant cette
// correction, choisir une nouvelle photo ne changeait rien à l'écran (l'ancienne
// image, ou le placeholder, restait affiché·e) tant que le formulaire n'était
// pas enregistré puis la page rechargée. On ajoute un aperçu immédiat côté
// client (FileReader), plus un glisser-déposer, sans toucher au reste.
function initCoverImagePreview(root) {
  root = root || document;
  const MAX_PREVIEW_BYTES = 15 * 1024 * 1024; // garde-fou, cohérent avec accept="image/*"

  function applyPreview(slot, file) {
    if (!file || !file.type || !file.type.startsWith("image/")) return;
    if (file.size > MAX_PREVIEW_BYTES) return;

    const reader = new FileReader();
    reader.onload = () => {
      let img = slot.querySelector("img");
      const placeholder = slot.querySelector(".org-image-placeholder, .org-cover-placeholder");
      if (!img) {
        img = document.createElement("img");
        img.alt = "";
        slot.insertBefore(img, placeholder || slot.firstChild);
      }
      img.src = reader.result;
      if (placeholder) placeholder.remove();
      slot.classList.add("has-pending-preview");
    };
    reader.readAsDataURL(file);
  }

  root.querySelectorAll(".org-image-slot").forEach(slot => {
    const input = slot.querySelector('input[type="file"]');
    if (!input || slot.dataset.coverPreviewReady) return;
    slot.dataset.coverPreviewReady = "1";

    input.addEventListener("change", () => {
      if (input.files && input.files[0]) applyPreview(slot, input.files[0]);
    });

    // Glisser-déposer moderne directement sur la vignette
    slot.addEventListener("dragover", (e) => {
      e.preventDefault();
      slot.classList.add("drag-over");
    });
    slot.addEventListener("dragleave", () => slot.classList.remove("drag-over"));
    slot.addEventListener("drop", (e) => {
      e.preventDefault();
      slot.classList.remove("drag-over");
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (!file) return;
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      applyPreview(slot, file);
    });
  });
}

// ============ Champ "Tags" de "✎ Modifier les métadonnées" (popup rapide +
// page vidéo) : chaque tag associé à la vidéo est une puce (mot + photo,
// glisser/cliquer une image dessus pour lui en donner une). Une même puce
// permet donc de gérer le mot ET la photo du tag, en un seul endroit — les
// photos ne sont réellement envoyées qu'au moment de l'enregistrement du
// formulaire englobant (voir appendPendingTagImages plus bas), jamais
// séparément, pour rester cohérent avec le principe "un seul bouton
// Enregistrer" déjà utilisé partout ailleurs dans l'appli. ============
function initTagField(root) {
  root = root || document;

  function findExistingTag(name) {
    const key = name.trim().toLowerCase();
    if (!key || !window.__ALL_TAGS__) return null;
    return window.__ALL_TAGS__.find(t => t.name.toLowerCase() === key) || null;
  }

  // Distance de similarité simple (Levenshtein bornée) pour repérer les
  // quasi-doublons ("nature" / "natures", "voyage" / "voyages") avant création.
  function editDistance(a, b) {
    if (a === b) return 0;
    const la = a.length, lb = b.length;
    if (!la) return lb;
    if (!lb) return la;
    if (Math.abs(la - lb) > 3) return 99;
    const prev = new Array(lb + 1);
    const cur = new Array(lb + 1);
    for (let j = 0; j <= lb; j++) prev[j] = j;
    for (let i = 1; i <= la; i++) {
      cur[0] = i;
      for (let j = 1; j <= lb; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      }
      for (let j = 0; j <= lb; j++) prev[j] = cur[j];
    }
    return prev[lb];
  }

  function scoreTagMatch(tagName, key) {
    const n = tagName.toLowerCase();
    if (n === key) return 1000;
    if (n.startsWith(key)) return 800 - Math.min(n.length - key.length, 50);
    if (n.includes(key)) return 500 - n.indexOf(key);
    const dist = editDistance(n, key);
    if (dist <= 2 && key.length >= 3) return 300 - dist * 40;
    return -1;
  }

  function syncHiddenValue(field) {
    const hidden = field.querySelector(".js-tag-hidden-value");
    const names = Array.from(field.querySelectorAll(".tag-chip")).map(c => c.dataset.tagName);
    hidden.value = names.join(", ");
    const empty = field.querySelector(".tag-chips-empty");
    if (empty) empty.hidden = names.length > 0;
  }

  function buildChip(field, name, tagId, imageUrl) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.dataset.tagId = tagId || "";
    chip.dataset.tagName = name;

    const slot = document.createElement("label");
    slot.className = "org-image-slot tag-chip-img-slot";
    slot.title = "Cliquer ou glisser une image pour ce tag";
    if (imageUrl) {
      const img = document.createElement("img");
      img.src = imageUrl;
      img.alt = "";
      slot.appendChild(img);
    } else {
      const ph = document.createElement("span");
      ph.className = "org-image-placeholder tag-chip-img-placeholder";
      ph.textContent = "🖼";
      slot.appendChild(ph);
    }
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/*";
    fileInput.hidden = true;
    slot.appendChild(fileInput);

    const nameEl = document.createElement("span");
    nameEl.className = "tag-chip-name";
    nameEl.textContent = name;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "tag-chip-remove js-tag-chip-remove";
    removeBtn.title = "Retirer ce tag de cette vidéo";
    removeBtn.textContent = "✕";

    chip.appendChild(slot);
    chip.appendChild(nameEl);
    chip.appendChild(removeBtn);

    if (tagId) {
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "tag-chip-delete icon-btn icon-btn-mini icon-btn-danger js-delete";
      deleteBtn.dataset.type = "tag";
      deleteBtn.dataset.id = tagId;
      deleteBtn.dataset.name = name;
      deleteBtn.title = `Supprimer définitivement le tag « ${name} » de la base`;
      deleteBtn.textContent = "🗑";
      chip.appendChild(deleteBtn);
    }

    return chip;
  }

  function addTag(field, name, tagId, imageUrl) {
    name = name.trim();
    if (!name) return false;
    const chipsBox = field.querySelector(".js-tag-chips");
    const already = Array.from(chipsBox.querySelectorAll(".tag-chip"))
      .some(c => c.dataset.tagName.toLowerCase() === name.toLowerCase());
    if (already) {
      if (typeof showToast === "function") showToast(`Tag « ${name} » déjà associé`);
      return false;
    }
    if (tagId === undefined) {
      const known = findExistingTag(name);
      tagId = known ? known.id : "";
      imageUrl = known ? known.image_url : null;
    }
    const chip = buildChip(field, name, tagId, imageUrl);
    chipsBox.appendChild(chip);
    initCoverImagePreview(chip);
    syncHiddenValue(field);
    return true;
  }

  root.querySelectorAll(".js-tag-field").forEach(field => {
    if (field.dataset.tagFieldReady) return;
    field.dataset.tagFieldReady = "1";
    syncHiddenValue(field);
    initCoverImagePreview(field);

    const input = field.querySelector(".js-tag-input");
    const suggestBox = field.querySelector(".js-tag-suggest-box");

    function commitInput() {
      const raw = input.value;
      raw.split(",").forEach(part => addTag(field, part));
      input.value = "";
      hideSuggestions();
    }

    // Suggestions enrichies : tri (exact > préfixe > contient > fuzzy),
    // miniatures, compteur d'usage, badge « proche », option créer.
    let suggestActiveIndex = -1;

    function hideSuggestions() {
      suggestBox.hidden = true;
      suggestBox.innerHTML = "";
      suggestActiveIndex = -1;
      input.removeAttribute("aria-activedescendant");
      input.setAttribute("aria-expanded", "false");
    }

    function buildSuggestItem(t, key) {
      const item = document.createElement("div");
      item.className = "tag-suggest-item";
      item.dataset.tagName = t.name;
      item.dataset.tagId = t.id;
      item.dataset.tagImage = t.image_url || "";
      item.setAttribute("role", "option");
      item.id = `tag-suggest-${field.dataset.fieldId || "x"}-${t.id}`;

      if (t.image_url) {
        const img = document.createElement("img");
        img.className = "tag-suggest-item-thumb";
        img.src = t.image_url;
        img.alt = "";
        item.appendChild(img);
      } else {
        const ph = document.createElement("span");
        ph.className = "tag-suggest-item-thumb-ph";
        ph.textContent = (t.name || "?").charAt(0).toUpperCase();
        item.appendChild(ph);
      }

      const label = document.createElement("span");
      label.className = "tag-suggest-item-label";
      label.textContent = t.name;
      item.appendChild(label);

      const meta = document.createElement("span");
      meta.className = "tag-suggest-item-meta";
      const nLower = t.name.toLowerCase();
      const isNear = !nLower.includes(key) && editDistance(nLower, key) <= 2;
      if (isNear) {
        const near = document.createElement("span");
        near.className = "tag-suggest-item-near";
        near.textContent = "proche";
        meta.appendChild(near);
      }
      if (t.count != null && t.count > 0) {
        const count = document.createElement("span");
        count.className = "tag-suggest-item-count";
        count.textContent = t.count === 1 ? "1 vidéo" : `${t.count} vidéos`;
        meta.appendChild(count);
      }
      item.appendChild(meta);
      return item;
    }

    function renderSuggestions() {
      const raw = input.value;
      const query = raw.split(",").pop().trim();
      if (!query || !window.__ALL_TAGS__) {
        hideSuggestions();
        return;
      }
      const already = new Set(
        Array.from(field.querySelectorAll(".tag-chip")).map(c => c.dataset.tagName.toLowerCase())
      );
      const key = query.toLowerCase();

      const scored = window.__ALL_TAGS__
        .filter(t => !already.has(t.name.toLowerCase()))
        .map(t => ({ t, score: scoreTagMatch(t.name, key) }))
        .filter(x => x.score >= 0)
        .sort((a, b) => {
          if (b.score !== a.score) return b.score - a.score;
          const ca = a.t.count || 0, cb = b.t.count || 0;
          if (cb !== ca) return cb - ca;
          return a.t.name.localeCompare(b.t.name, "fr", { sensitivity: "base" });
        })
        .slice(0, 8);

      const exact = scored.some(x => x.t.name.toLowerCase() === key);

      suggestBox.innerHTML = "";
      scored.forEach(({ t }) => suggestBox.appendChild(buildSuggestItem(t, key)));

      if (!exact) {
        const item = document.createElement("div");
        item.className = "tag-suggest-item tag-suggest-item-create";
        item.dataset.createValue = query;
        item.setAttribute("role", "option");
        const ph = document.createElement("span");
        ph.className = "tag-suggest-item-thumb-ph";
        ph.textContent = "+";
        item.appendChild(ph);
        const label = document.createElement("span");
        label.className = "tag-suggest-item-label";
        label.textContent = `Créer le tag « ${query} »`;
        item.appendChild(label);
        suggestBox.appendChild(item);

        const nearExisting = window.__ALL_TAGS__.find(t => {
          const n = t.name.toLowerCase();
          return !already.has(n) && n !== key && editDistance(n, key) <= 2 && key.length >= 3;
        });
        if (nearExisting) {
          const hint = document.createElement("div");
          hint.className = "tag-suggest-hint";
          hint.textContent = `Tag proche déjà existant : « ${nearExisting.name} » — préfère le réutiliser pour éviter un doublon.`;
          suggestBox.appendChild(hint);
        }
      }

      suggestBox.hidden = suggestBox.querySelectorAll(".tag-suggest-item").length === 0;
      suggestActiveIndex = -1;
      input.setAttribute("aria-expanded", suggestBox.hidden ? "false" : "true");
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
    }

    function commitSuggestion(item) {
      if (!item || item.classList.contains("tag-suggest-hint")) return;
      if (item.dataset.createValue !== undefined) {
        addTag(field, item.dataset.createValue);
      } else {
        addTag(field, item.dataset.tagName, item.dataset.tagId, item.dataset.tagImage || null);
      }
      const parts = input.value.split(",");
      parts.pop();
      input.value = parts.length ? parts.join(",") + ", " : "";
      hideSuggestions();
      input.focus();
    }

    input.addEventListener("input", renderSuggestions);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        const items = Array.from(suggestBox.querySelectorAll(".tag-suggest-item"));
        if (!suggestBox.hidden && suggestActiveIndex >= 0 && items[suggestActiveIndex]) {
          commitSuggestion(items[suggestActiveIndex]);
        } else {
          commitInput();
        }
        return;
      }
      if (suggestBox.hidden) return;
      const items = Array.from(suggestBox.querySelectorAll(".tag-suggest-item"));
      if (!items.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        suggestActiveIndex = e.key === "ArrowDown"
          ? (suggestActiveIndex + 1) % items.length
          : (suggestActiveIndex - 1 + items.length) % items.length;
        items.forEach((it, i) => it.classList.toggle("active", i === suggestActiveIndex));
        if (items[suggestActiveIndex]) {
          input.setAttribute("aria-activedescendant", items[suggestActiveIndex].id || "");
          items[suggestActiveIndex].scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "Escape") {
        hideSuggestions();
      }
    });
    suggestBox.addEventListener("mousedown", (e) => {
      const item = e.target.closest(".tag-suggest-item");
      if (item) {
        e.preventDefault();
        commitSuggestion(item);
      }
    });
    input.addEventListener("blur", () => {
      setTimeout(() => {
        if (input.value.trim()) commitInput();
        else hideSuggestions();
      }, 150);
    });

    field.addEventListener("click", (e) => {
      const removeBtn = e.target.closest(".js-tag-chip-remove");
      if (removeBtn) {
        removeBtn.closest(".tag-chip").remove();
        syncHiddenValue(field);
      }
    });
  });
}

function removeTagEverywhere(id, name) {
  document.querySelectorAll(`.tag-chip[data-tag-id="${id}"]`).forEach(chip => {
    const field = chip.closest(".js-tag-field");
    chip.remove();
    if (field) {
      const hidden = field.querySelector(".js-tag-hidden-value");
      const names = Array.from(field.querySelectorAll(".tag-chip")).map(c => c.dataset.tagName);
      if (hidden) hidden.value = names.join(", ");
      const empty = field.querySelector(".tag-chips-empty");
      if (empty) empty.hidden = names.length > 0;
    }
  });
  if (Array.isArray(window.__ALL_TAGS__)) {
    window.__ALL_TAGS__ = window.__ALL_TAGS__.filter(t => String(t.id) !== String(id));
  }
  document.querySelectorAll('datalist[id^="tag-datalist-"]').forEach(dl => {
    Array.from(dl.options).forEach(opt => {
      if (opt.value.toLowerCase() === (name || "").toLowerCase()) opt.remove();
    });
  });
}

// ============ Champ "Catégorie" unifié (page vidéo + popup rapide) ============
// Actions clairement séparées (plus d'option "__new__" dans le select) :
//   • select        → choisir une catégorie existante (ou « Aucune »)
//   • Retirer       → enlève la catégorie de CETTE vidéo seulement
//   • + Nouvelle    → ouvre le formulaire de création
//   • Créer         → crée en base et assigne à la vidéo
//   • 🗑 Supprimer  → efface la catégorie de toute la base (confirmation)
// La valeur soumise reste name="category" (input caché) = nom ou "".
function initCategoryField(root) {
  root = root || document;
  root.querySelectorAll(".js-category-field").forEach(field => {
    if (field.dataset.categoryFieldReady) return;
    field.dataset.categoryFieldReady = "1";

    const hidden = field.querySelector(".js-category-hidden");
    const select = field.querySelector(".js-category-select");
    const newInput = field.querySelector(".js-category-new-input");
    const deleteBtn = field.querySelector(".js-category-delete-btn");
    const clearBtn = field.querySelector(".js-category-clear-btn");
    const newBtn = field.querySelector(".js-category-new-btn");
    const toggleCreateBtn = field.querySelector(".js-category-toggle-create");
    const cancelCreateBtn = field.querySelector(".js-category-cancel-create");
    const filterInput = field.querySelector(".js-category-filter");
    const newBox = field.querySelector(".js-category-new-box");
    const hint = field.querySelector(".js-category-hint");

    function showNewBox(show) {
      if (!newBox) return;
      if (show) {
        newBox.hidden = false;
        newBox.style.display = "flex";
        if (toggleCreateBtn) toggleCreateBtn.hidden = true;
      } else {
        newBox.hidden = true;
        newBox.style.display = "none";
        if (toggleCreateBtn) toggleCreateBtn.hidden = false;
        if (newInput) newInput.value = "";
      }
    }

    function currentSelection() {
      const opt = select && select.options[select.selectedIndex];
      if (!opt || !opt.value) return { id: null, name: "" };
      return {
        id: opt.dataset.catId || null,
        name: opt.value || opt.textContent || "",
      };
    }

    function syncActions() {
      const { id, name } = currentSelection();
      const hasCat = !!(id && name);

      if (clearBtn) clearBtn.hidden = !hasCat;
      if (deleteBtn) {
        deleteBtn.hidden = !hasCat;
        deleteBtn.dataset.id = id || "";
        deleteBtn.dataset.name = name || "";
        deleteBtn.dataset.type = "category";
        deleteBtn.title = hasCat
          ? `Supprimer définitivement « ${name} » de la base (toutes les vidéos concernées repasseront sans catégorie)`
          : "Supprimer définitivement cette catégorie de la base";
      }

      if (hint) {
        const hChoose = hint.querySelector(".cat-hint-choose");
        const hAssigned = hint.querySelector(".cat-hint-assigned");
        const hCreate = hint.querySelector(".cat-hint-create");
        const creating = newBox && !newBox.hidden && newBox.style.display !== "none";
        if (hChoose) hChoose.hidden = hasCat || creating;
        if (hAssigned) {
          hAssigned.hidden = !hasCat || creating;
          const nameEl = hAssigned.querySelector(".js-category-hint-name");
          if (nameEl) nameEl.textContent = name;
        }
        if (hCreate) hCreate.hidden = !creating;
      }
    }

    function applySelect() {
      // Plus d'option __new__ : le select ne fait que choisir / vider.
      if (select.value === "__new__") {
        // Compat anciennes versions : bascule en mode création.
        select.value = "";
        showNewBox(true);
        if (newInput) newInput.focus();
      } else {
        showNewBox(false);
        hidden.value = select.value;
      }
      syncActions();
      // Marque le formulaire parent comme modifié (live dirty).
      const form = field.closest("form");
      if (form && typeof markLiveFormDirty === "function") markLiveFormDirty(form);
    }

    function applyFilter() {
      if (!filterInput || !select) return;
      const q = filterInput.value.trim().toLowerCase();
      Array.from(select.options).forEach(opt => {
        if (!opt.value) {
          opt.hidden = false;
          return;
        }
        opt.hidden = q ? !opt.textContent.toLowerCase().includes(q) : false;
      });
    }

    if (select) select.addEventListener("change", applySelect);

    if (filterInput) {
      filterInput.addEventListener("input", applyFilter);
      filterInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") e.preventDefault();
      });
    }

    // Retirer = désassigner de cette vidéo uniquement (ne touche pas la base).
    if (clearBtn) {
      clearBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (select) select.value = "";
        if (hidden) hidden.value = "";
        showNewBox(false);
        syncActions();
        const form = field.closest("form");
        if (form && typeof markLiveFormDirty === "function") markLiveFormDirty(form);
      });
    }

    // 🗑 = suppression définitive en base (confirmation).
    if (deleteBtn) {
      deleteBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const catId = deleteBtn.dataset.id;
        const catName = deleteBtn.dataset.name;
        if (!catId) return;
        if (typeof deleteEntity === "function") {
          deleteEntity("category", catId, catName || "");
        }
      });
    }

    if (toggleCreateBtn) {
      toggleCreateBtn.addEventListener("click", (e) => {
        e.preventDefault();
        showNewBox(true);
        syncActions();
        if (newInput) newInput.focus();
      });
    }

    if (cancelCreateBtn) {
      cancelCreateBtn.addEventListener("click", (e) => {
        e.preventDefault();
        showNewBox(false);
        // Restaure la valeur du select dans le hidden.
        if (hidden && select) hidden.value = select.value;
        syncActions();
      });
    }

    if (newInput) {
      newInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          if (newBtn) newBtn.click();
        }
        if (e.key === "Escape") {
          e.preventDefault();
          if (cancelCreateBtn) cancelCreateBtn.click();
        }
      });
    }

    if (newBtn) {
      newBtn.addEventListener("click", () => {
        const name = newInput ? newInput.value.trim() : "";
        if (!name) {
          if (newInput) newInput.focus();
          return;
        }
        newBtn.disabled = true;
        const origText = newBtn.textContent;
        newBtn.textContent = "…";
        submitEntityAjax("/category/new", { name })
          .then(res => {
            const finalName = res.name !== undefined ? res.name : name;
            addCategoryEverywhere(res.id, finalName);
            if (select) {
              select.value = finalName;
              if (select.value !== finalName) {
                const opt = select.querySelector(`option[data-cat-id="${res.id}"]`);
                if (opt) select.value = opt.value;
              }
            }
            if (hidden) hidden.value = select ? select.value : finalName;
            if (newInput) newInput.value = "";
            showNewBox(false);
            if (filterInput) {
              filterInput.value = "";
              applyFilter();
            }
            syncActions();
            if (typeof showToast === "function") {
              showToast(`Catégorie « ${finalName} » créée et assignée`);
            }
            const form = field.closest("form");
            if (form && typeof markLiveFormDirty === "function") markLiveFormDirty(form);
          })
          .catch(err => alert("Impossible de créer la catégorie : " + err.message))
          .finally(() => {
            newBtn.disabled = false;
            newBtn.textContent = origText;
          });
      });
    }

    // État initial
    showNewBox(false);
    if (hidden && select && hidden.value && !Array.from(select.options).some(o => o.value === hidden.value)) {
      // Nom inconnu (ex. saisie libre héritée) : propose la création préremplie.
      if (newInput) newInput.value = hidden.value;
      showNewBox(true);
    }
    syncActions();
  });
}

// Applique une catégorie (id + nom) sur un champ .js-category-field,
// utilisé à l'ouverture de la popup d'édition rapide.
function setCategoryFieldValue(field, categoryId, categoryName) {
  if (!field) return;
  const hidden = field.querySelector(".js-category-hidden");
  const select = field.querySelector(".js-category-select");
  const name = categoryName || "";
  if (hidden) hidden.value = name;
  if (select) {
    if (categoryId) {
      const opt = select.querySelector(`option[data-cat-id="${categoryId}"]`);
      if (opt) select.value = opt.value;
      else if (name) select.value = name;
      else select.value = "";
    } else {
      select.value = "";
    }
  }
  // Resync boutons / hint si le champ est déjà initialisé.
  const clearBtn = field.querySelector(".js-category-clear-btn");
  const deleteBtn = field.querySelector(".js-category-delete-btn");
  const hasCat = !!(categoryId && name);
  if (clearBtn) clearBtn.hidden = !hasCat;
  if (deleteBtn) {
    deleteBtn.hidden = !hasCat;
    deleteBtn.dataset.id = categoryId ? String(categoryId) : "";
    deleteBtn.dataset.name = name;
    deleteBtn.dataset.type = "category";
  }
  const hint = field.querySelector(".js-category-hint");
  if (hint) {
    const hChoose = hint.querySelector(".cat-hint-choose");
    const hAssigned = hint.querySelector(".cat-hint-assigned");
    const hCreate = hint.querySelector(".cat-hint-create");
    if (hChoose) hChoose.hidden = hasCat;
    if (hAssigned) {
      hAssigned.hidden = !hasCat;
      const nameEl = hAssigned.querySelector(".js-category-hint-name");
      if (nameEl) nameEl.textContent = name;
    }
    if (hCreate) hCreate.hidden = true;
  }
  const newBox = field.querySelector(".js-category-new-box");
  if (newBox) {
    newBox.hidden = true;
    newBox.style.display = "none";
  }
  const toggleCreateBtn = field.querySelector(".js-category-toggle-create");
  if (toggleCreateBtn) toggleCreateBtn.hidden = false;
}

// Rassemble, pour un formulaire donné, les photos en attente sur les puces
// de son champ "Tags" (fichier choisi ou déposé, pas encore enregistré) et
// les ajoute à la requête sous forme de deux listes appariées (même ordre :
// tag_image_name[i] correspond toujours à tag_image_file[i]) — voir
// app.py/video_edit, qui résout chaque nom en identifiant de tag et associe
// le fichier reçu, que le tag soit déjà existant ou flambant neuf.
function appendPendingTagImages(form, data) {
  form.querySelectorAll(".js-tag-field .tag-chip").forEach(chip => {
    const fileInput = chip.querySelector(".tag-chip-img-slot input[type='file']");
    const file = fileInput && fileInput.files && fileInput.files[0];
    if (!file) return;
    data.append("tag_image_name", chip.dataset.tagName);
    data.append("tag_image_file", file, file.name);
  });
}

// Reconstruit entièrement les puces d'un champ "Tags" à partir d'une liste
// fraîche {id, name, image_url} — utilisé à l'ouverture de la popup rapide
// (données chargées depuis /api/video/<id>/meta) et juste après un
// enregistrement réussi (données renvoyées par la réponse elle-même), pour
// que les tags flambant neufs affichent aussitôt leur identifiant réel et
// que les photos tout juste envoyées apparaissent bien comme enregistrées
// (et non plus "en attente").
function setTagFieldValue(field, tagsFull) {
  const chipsBox = field.querySelector(".js-tag-chips");
  chipsBox.querySelectorAll(".tag-chip").forEach(c => c.remove());
  (tagsFull || []).forEach(t => {
    const chip = field.querySelector(".js-tag-chips");
    chip.appendChild((function () {
      const el = document.createElement("span");
      el.className = "tag-chip";
      el.dataset.tagId = t.id;
      el.dataset.tagName = t.name;
      const slot = document.createElement("label");
      slot.className = "org-image-slot tag-chip-img-slot";
      slot.title = "Cliquer ou glisser une image pour ce tag";
      if (t.image_url) {
        const img = document.createElement("img");
        img.src = t.image_url;
        img.alt = "";
        slot.appendChild(img);
      } else {
        const ph = document.createElement("span");
        ph.className = "org-image-placeholder tag-chip-img-placeholder";
        ph.textContent = "🖼";
        slot.appendChild(ph);
      }
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = "image/*";
      fileInput.hidden = true;
      slot.appendChild(fileInput);
      const nameEl = document.createElement("span");
      nameEl.className = "tag-chip-name";
      nameEl.textContent = t.name;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "tag-chip-remove js-tag-chip-remove";
      removeBtn.title = "Retirer ce tag de cette vidéo";
      removeBtn.textContent = "✕";
      el.appendChild(slot);
      el.appendChild(nameEl);
      el.appendChild(removeBtn);

      if (t.id) {
        const deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "tag-chip-delete icon-btn icon-btn-mini icon-btn-danger js-delete";
        deleteBtn.dataset.type = "tag";
        deleteBtn.dataset.id = t.id;
        deleteBtn.dataset.name = t.name;
        deleteBtn.title = `Supprimer définitivement le tag « ${t.name} » de la base`;
        deleteBtn.textContent = "🗑";
        el.appendChild(deleteBtn);
      }

      return el;
    })());
    initCoverImagePreview(chipsBox);
  });
  const hidden = field.querySelector(".js-tag-hidden-value");
  hidden.value = (tagsFull || []).map(t => t.name).join(", ");
  const empty = field.querySelector(".tag-chips-empty");
  if (empty) empty.hidden = (tagsFull || []).length > 0;
}

// ============ Enregistrement en temps réel des formulaires de métadonnées
// (catégories / playlists / sous-playlists sur la page Organiser et les
// pages dédiées, + page vidéo dédiée) ============
// Même principe que la popup rapide plus haut (fetch avec ajax=1, réponse
// JSON, mise à jour ciblée du DOM) : plus de rechargement de page complet
// après un enregistrement, donc plus de lenteur ni de perte de défilement.
function initRealtimeEditForms(root) {
  root = root || document;
  function submitAjaxForm(form, onSuccess) {
    const submitBtn = form.querySelector("button[type='submit']");
    const originalLabel = submitBtn ? submitBtn.textContent : null;
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Enregistrement...";
    }
    const data = new FormData(form);
    appendPendingTagImages(form, data);
    fetch(form.action, { method: "POST", body: data, cache: "no-store" })
      .then(r => r.json().then(json => ({ status: r.status, json })))
      .then(({ status, json }) => {
        if (status >= 200 && status < 300 && json && json.ok) {
          // Enregistrement confirmé : le formulaire n'a plus de
          // modifications "en danger" à protéger d'un rafraîchissement
          // temps réel (voir markLiveFormDirty / clearLiveFormDirty).
          clearLiveFormDirty(form);
          onSuccess(json);
        } else {
          throw new Error((json && json.error) || `HTTP ${status}`);
        }
      })
      .catch(err => {
        alert("Une erreur est survenue lors de l'enregistrement : " + err.message);
        // En cas d'erreur, réactive le bouton selon l'état dirty.
        if (submitBtn && form.id === "video-edit-form") {
          const dirty = form.dataset.liveDirty === "1";
          submitBtn.disabled = !dirty;
          submitBtn.textContent = submitBtn.dataset.defaultLabel || originalLabel || "✓ Enregistrer";
          submitBtn.classList.remove("is-saved");
        }
      })
      .finally(() => {
        if (!submitBtn) return;
        // Page vidéo : l'état du bouton est géré par markClean / is-saved
        // dans le callback onSuccess — ne pas écraser ici.
        if (form.id === "video-edit-form") return;
        submitBtn.disabled = false;
        submitBtn.textContent = originalLabel;
      });
  }

  function starsHtml(rating) {
    return rating
      ? "★".repeat(rating) + "☆".repeat(5 - rating)
      : '<span class="muted">Sans note</span>';
  }

  // escapeHtml est défini en tête de main.js (anti-XSS global).

  // Reconstruit le HTML d'une carte contextuelle (Playlist / Sous-playlist /
  // Catégorie) sous le lecteur vidéo, à partir des données JSON renvoyées
  // par le serveur (id/name/image_url/url) — identique au rendu généré côté
  // serveur par video.html, pour permettre une mise à jour sans recharger
  // la page après l'enregistrement des métadonnées.
  function buildContextCard(entity, kicker, cardClass, placeholderIcon) {
    if (!entity) return "";
    const cover = entity.image_url
      ? `<img src="${escapeHtml(entity.image_url)}" alt="">`
      : `<div class="context-card-placeholder">${placeholderIcon}</div>`;
    return `<a class="context-card ${cardClass}" href="${escapeHtml(entity.url)}">
        <div class="context-card-cover">${cover}</div>
        <div class="context-card-info">
          <div class="context-card-kicker">${kicker}</div>
          <div class="context-card-title">${escapeHtml(entity.name)}</div>
        </div>
      </a>`;
  }

  // Reconstruit la case "playlist/sous-playlist/catégorie en cours de
  // lecture" (au-dessus de l'écran vidéo) — identique au rendu généré côté
  // serveur par video.html (repli sous-playlist -> playlist -> catégorie),
  // pour permettre une mise à jour sans recharger la page après
  // l'enregistrement des métadonnées.
  function buildNowPlayingCard(entity, kind) {
    if (!entity) return "";
    const icon = kind === "category" ? "🗂" : "📃";
    const kindLabel = kind === "category" ? "Catégorie" : (kind === "playlist" ? "Playlist" : "Sous-playlist");
    const cover = entity.image_url
      ? `<img src="${escapeHtml(entity.image_url)}" alt="">`
      : `<span class="now-playing-badge-cover-fallback">${icon}</span>`;
    return `<a class="now-playing-badge-bar" id="now-playing-subplaylist-card" href="${escapeHtml(entity.url)}" title="${kindLabel} en cours de lecture : ${escapeHtml(entity.name)}">
        <span class="now-playing-badge-cover">${cover}</span>
        <span class="now-playing-badge-text">
          <span class="now-playing-badge-kicker"><span class="now-playing-dot"></span>Lecture maintenant</span>
          <span class="now-playing-badge-name">${escapeHtml(entity.name)}</span>
        </span>
      </a>`;
  }

  // --- Cartes catégories / playlists / sous-playlists (page Organiser,
  // et sous-playlists de la page playlist dédiée) ---
  root.querySelectorAll(".org-card .org-edit-form").forEach(form => {
    if (form.dataset.realtimeReady) return;
    form.dataset.realtimeReady = "1";
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const card = form.closest(".org-card");
      submitAjaxForm(form, (res) => {
        // Une playlist déplacée vers une autre playlist parente change de
        // section (ou de page) : un rafraîchissement reste nécessaire pour
        // ce cas précis, mais reste rapide puisqu'on vient de sauvegarder.
        if (res.parent_changed) {
          window.location.reload();
          return;
        }
        if (!card) return;
        const titleEl = card.querySelector(".org-card-title strong");
        if (titleEl && res.name !== undefined) titleEl.textContent = res.name;
        const starsEl = card.querySelector(".org-stars-display");
        if (starsEl && res.rating !== undefined) starsEl.innerHTML = starsHtml(res.rating);
        if (res.name !== undefined) card.dataset.name = res.name.toLowerCase();
        if (res.rating !== undefined) card.dataset.rating = res.rating;
        const deleteBtn = card.querySelector(".js-delete.org-card-delete");
        if (deleteBtn && res.name !== undefined) deleteBtn.dataset.name = res.name;

        // Panneau latéral (+ en-tête dédiée, liste des tags...) : cette
        // carte n'est qu'une des façons de modifier une catégorie/playlist ;
        // on répercute donc le nom et la photo partout ailleurs tout de
        // suite, exactement comme le fait déjà le bouton ✎ du panneau
        // latéral, plutôt que d'attendre le prochain rafraîchissement
        // temps réel (/api/live) pour que le panneau se mette à jour.
        const entityType = deleteBtn ? deleteBtn.dataset.type : null;
        const entityId = deleteBtn ? deleteBtn.dataset.id : null;
        if (entityType && entityId) {
          if (res.name !== undefined) applyEntityRename(entityType, entityId, res.name);
          if (res.image_url) applyEntityImageUpdate(entityType, entityId, res.image_url);
        }

        let commentEl = card.querySelector(".org-comment-preview");
        if (res.comment) {
          if (!commentEl) {
            commentEl = document.createElement("p");
            commentEl.className = "org-comment-preview";
            const link = card.querySelector(".org-card-link");
            if (link) link.insertAdjacentElement("afterend", commentEl);
          }
          commentEl.textContent = "💬 " + res.comment;
        } else if (commentEl) {
          commentEl.remove();
        }

        // Date de création (saisie manuelle) : uniquement affichée sur les
        // cartes de sous-playlists (le champ n'existe pas ailleurs, donc
        // res.manual_creation_date_display reste undefined pour les autres).
        if (res.manual_creation_date_display !== undefined) {
          let manualDateEl = card.querySelector(".org-card-manual-date");
          if (res.manual_creation_date_display) {
            if (!manualDateEl) {
              manualDateEl = document.createElement("div");
              manualDateEl.className = "org-card-manual-date muted";
              const anchor = commentEl || card.querySelector(".org-card-link");
              if (anchor) anchor.insertAdjacentElement("afterend", manualDateEl);
            }
            manualDateEl.textContent = "📅 Créée le " + res.manual_creation_date_display;
          } else if (manualDateEl) {
            manualDateEl.remove();
          }
        }

        if (res.image_url) {
          const bg = card.querySelector(".org-cover-bg");
          const main = card.querySelector(".org-cover-main");
          if (bg && main) {
            bg.src = res.image_url;
            main.src = res.image_url;
          } else {
            // Pas encore de photo affichée jusqu'ici (placeholder) : la
            // structure de la vignette doit changer, un rafraîchissement
            // reste le plus sûr pour ce cas précis (première photo ajoutée).
            window.location.reload();
            return;
          }
        }

        const details = form.closest("details.org-card-edit");
        if (details) details.open = false;
      });
    });
  });

  // --- En-tête catégorie / playlist (pages dédiées) ---
  root.querySelectorAll(".collection-info > details .org-edit-form").forEach(form => {
    if (form.dataset.realtimeReady) return;
    form.dataset.realtimeReady = "1";
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      submitAjaxForm(form, (res) => {
        if (res.parent_changed) {
          window.location.reload();
          return;
        }
        const titleEl = document.querySelector(".collection-title");
        if (titleEl && res.name !== undefined) titleEl.textContent = res.name;
        const starsEl = document.querySelector(".collection-stars");
        if (starsEl && res.rating !== undefined) starsEl.innerHTML = starsHtml(res.rating);

        let commentEl = document.querySelector(".collection-comment");
        if (res.comment) {
          if (!commentEl) {
            commentEl = document.createElement("p");
            commentEl.className = "collection-comment";
            if (starsEl) starsEl.insertAdjacentElement("afterend", commentEl);
          }
          commentEl.textContent = res.comment;
        } else if (commentEl) {
          commentEl.remove();
        }

        // Date de création (saisie manuelle) : uniquement présente sur la
        // page d'une sous-playlist (le span existe déjà dans le HTML,
        // vide ou non ; res.manual_creation_date_display reste undefined
        // pour une playlist principale, dans quel cas on ne touche à rien).
        if (res.manual_creation_date_display !== undefined) {
          const manualDateEl = document.querySelector(".collection-manual-date");
          if (manualDateEl) {
            manualDateEl.textContent = res.manual_creation_date_display
              ? " · 📅 Créée le " + res.manual_creation_date_display
              : "";
          }
        }

        if (res.image_url) {
          // .collection-cover peut contenir une seule photo (catégorie /
          // playlist : taille naturelle) OU deux images superposées, façon
          // letterbox (tag : fond flouté + image réelle, voir tag.html) —
          // on met donc à jour TOUTES les <img> trouvées à l'intérieur.
          const imgs = document.querySelectorAll(".collection-cover img");
          if (imgs.length) {
            imgs.forEach(img => { img.src = res.image_url; });
          } else {
            window.location.reload();
            return;
          }
        }

        // Panneau latéral (+ autres cartes affichant la même entité) :
        // on identifie précisément la catégorie/playlist concernée à partir
        // de l'URL du formulaire (ex. /playlist/12/full_edit), plutôt que de
        // chercher "le premier bouton .js-rename catégorie/playlist trouvé
        // dans la page" — un tel bouton générique existe aussi dans le
        // panneau latéral pour TOUTE AUTRE catégorie/playlist affichée, et
        // s'y fier aurait pu mettre à jour le mauvais élément par erreur.
        // Cette identification précise permet aussi de répercuter tout de
        // suite le nom et la photo dans le panneau latéral, au lieu d'
        // attendre le prochain rafraîchissement temps réel (/api/live).
        const urlMatch = /\/(category|playlist|tag)\/(\d+)\/full_edit/.exec(form.action);
        const entityType = urlMatch ? urlMatch[1] : null;
        const entityId = urlMatch ? urlMatch[2] : null;
        if (entityType && entityId) {
          if (res.name !== undefined) applyEntityRename(entityType, entityId, res.name);
          if (res.image_url) applyEntityImageUpdate(entityType, entityId, res.image_url);
        }

        const details = form.closest("details.collection-edit-panel");
        if (details) details.open = false;
      });
    });
  });

  // --- Page vidéo dédiée ("✎ Modifier les métadonnées") ---
  const videoForm = root.querySelector("#video-edit-form");
  if (videoForm && !videoForm.dataset.realtimeReady) {
    videoForm.dataset.realtimeReady = "1";

    // Mémorise si la vidéo était en train de jouer au moment où l'édition a
    // commencé, pour ne relancer la lecture après enregistrement que si elle
    // avait bien été interrompue pour l'occasion (et pas si l'utilisateur
    // l'avait déjà mise en pause lui-même avant).
    let wasPlayingBeforeEdit = null;
    const saveBtn = videoForm.querySelector(".js-edit-save") || videoForm.querySelector('button[type="submit"]');
    const cancelBtn = videoForm.querySelector(".js-edit-cancel");

    function snapshotForm() {
      const data = new FormData(videoForm);
      const obj = {};
      data.forEach((v, k) => {
        if (obj[k] !== undefined) {
          if (!Array.isArray(obj[k])) obj[k] = [obj[k]];
          obj[k].push(v);
        } else {
          obj[k] = v;
        }
      });
      // Tags : valeur cachée + présence des puces (ordre).
      const tagField = videoForm.querySelector(".js-tag-field");
      if (tagField) {
        obj.__tagNames = Array.from(tagField.querySelectorAll(".tag-chip")).map(c => ({
          name: c.dataset.tagName,
          id: c.dataset.tagId || "",
          image: (c.querySelector(".tag-chip-img-slot img") || {}).src || null
        }));
      }
      // Playlists : cases cochées.
      obj.__playlists = Array.from(videoForm.querySelectorAll('input[name="playlists"]:checked')).map(cb => cb.value);
      videoForm._metaSnapshot = obj;
    }

    function syncDirtyUi() {
      const dirty = videoForm.dataset.liveDirty === "1";
      if (saveBtn) {
        saveBtn.disabled = !dirty;
        saveBtn.classList.remove("is-saved");
        if (!dirty && saveBtn.dataset.savedLabel) {
          saveBtn.textContent = saveBtn.dataset.defaultLabel || "✓ Enregistrer";
        }
      }
      // Annuler reste toujours visible en mode édition (permet de quitter même sans dirty).
      if (cancelBtn) cancelBtn.hidden = false;
    }

    function markClean() {
      clearLiveFormDirty(videoForm);
      syncDirtyUi();
      snapshotForm();
    }

    const editBlock = root.querySelector("#edit-block") || videoForm.closest(".edit-block");
    const editPanel = root.querySelector("#edit-panel") || (editBlock && editBlock.querySelector(".edit-panel"));
    const editTitle = root.querySelector("#edit-block-title");
    const playerLayout = root.querySelector(".player-layout") || document.querySelector(".player-layout");
    const editHint = root.querySelector("#edit-block-hint");

    function enterEditMode() {
      if (!editBlock || editBlock.classList.contains("is-editing")) return;

      // Déverrouille les champs (inaccessibles en mode lecture).
      videoForm.classList.remove("edit-form-locked");
      videoForm.removeAttribute("inert");

      // Active le mode édition : panneau visible + sidebar « À découvrir » masqué.
      editBlock.classList.add("is-editing");
      if (playerLayout) playerLayout.classList.add("is-editing");
      if (editPanel) editPanel.hidden = false;
      if (editTitle) editTitle.setAttribute("aria-expanded", "true");
      if (editHint) editHint.textContent = "Mode édition";

      // Met la vidéo en pause pour se concentrer sur l'édition.
      const video = document.getElementById("main-player");
      if (video) {
        if (wasPlayingBeforeEdit === null) {
          wasPlayingBeforeEdit = !video.paused;
        }
        if (!video.paused) video.pause();
      }

      // Focus sur le premier champ utile.
      const firstInput = videoForm.querySelector('input[name="title"]');
      if (firstInput) {
        try { firstInput.focus({ preventScroll: false }); } catch (_) { firstInput.focus(); }
      }
    }

    function exitEditMode() {
      if (!editBlock || !editBlock.classList.contains("is-editing")) return;

      editBlock.classList.remove("is-editing");
      if (playerLayout) playerLayout.classList.remove("is-editing");
      if (editPanel) editPanel.hidden = true;
      if (editTitle) editTitle.setAttribute("aria-expanded", "false");
      if (editHint) editHint.textContent = "Mode lecture · cliquer pour éditer";

      // Re-verrouille le formulaire (sécurité si on le ré-affiche un jour sans enter).
      videoForm.classList.add("edit-form-locked");
      videoForm.setAttribute("inert", "");

      // Reprend la lecture seulement si elle tournait avant d'entrer en édition.
      const video = document.getElementById("main-player");
      if (video && wasPlayingBeforeEdit) {
        video.play().catch(() => {});
      }
      wasPlayingBeforeEdit = null;
    }

    // Clic (ou Entrée/Espace) sur le titre → passage en mode édition.
    if (editTitle) {
      editTitle.addEventListener("click", enterEditMode);
      editTitle.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          enterEditMode();
        }
      });
    }

    if (saveBtn) {
      saveBtn.dataset.defaultLabel = saveBtn.textContent.trim();
      saveBtn.disabled = true;
    }
    snapshotForm();
    syncDirtyUi();

    // Observation locale : tout input/change déjà posé sur document marque
    // data-live-dirty ; on synchronise l'UI après coup.
    const dirtyObserver = new MutationObserver(() => syncDirtyUi());
    dirtyObserver.observe(videoForm, { attributes: true, attributeFilter: ["data-live-dirty"] });

    if (cancelBtn) {
      cancelBtn.addEventListener("click", () => {
        const snap = videoForm._metaSnapshot;
        const isDirty = videoForm.dataset.liveDirty === "1";
        if (isDirty && snap) {
          // Restaure les valeurs d’origine uniquement s’il y a des modifications.
          ["title", "description", "comment", "video_date", "views", "rating"].forEach(name => {
            const el = videoForm.querySelector(`[name="${name}"]`);
            if (!el) return;
            const val = snap[name] !== undefined ? snap[name] : "";
            el.value = Array.isArray(val) ? val[0] : val;
            if (name === "rating") {
              const widget = el.closest(".star-rating-input");
              if (widget) {
                const v = parseInt(el.value, 10) || 0;
                widget.querySelectorAll(".star-choice").forEach(s => {
                  s.classList.toggle("filled", parseInt(s.dataset.star, 10) <= v);
                });
                const lab = widget.querySelector(".js-star-value");
                if (lab) lab.textContent = v > 0 ? `${v}/5` : "—";
              }
            }
          });
          // Catégorie — restaure via le helper unifié (boutons + hint inclus).
          const catField = videoForm.querySelector(".js-category-field");
          if (catField && snap.category !== undefined) {
            const catVal = Array.isArray(snap.category) ? snap.category[0] : snap.category;
            const catSelect = catField.querySelector(".js-category-select");
            let catId = null;
            if (catSelect && catVal) {
              const opt = Array.from(catSelect.options).find(o => o.value === catVal);
              catId = opt && opt.dataset ? opt.dataset.catId : null;
            }
            if (typeof setCategoryFieldValue === "function") {
              setCategoryFieldValue(catField, catId, catVal || "");
            } else {
              const catHidden = catField.querySelector(".js-category-hidden");
              if (catHidden) catHidden.value = catVal || "";
              if (catSelect) catSelect.value = catVal || "";
            }
          }
          // Tags
          const tagField = videoForm.querySelector(".js-tag-field");
          if (tagField && snap.__tagNames) {
            setTagFieldValue(tagField, snap.__tagNames.map(t => ({
              id: t.id, name: t.name, image_url: t.image
            })));
          }
          // Playlists
          if (snap.__playlists) {
            const wanted = new Set(snap.__playlists.map(String));
            videoForm.querySelectorAll('input[name="playlists"]').forEach(cb => {
              cb.checked = wanted.has(String(cb.value));
              cb.dispatchEvent(new Event("change", { bubbles: true }));
            });
          }
          markClean();
          if (typeof showToast === "function") showToast("Modifications annulées");
        }
        // Dans tous les cas on quitte le mode édition (sidebar réapparaît).
        exitEditMode();
      });
    }

    // Ctrl/Cmd+S → enregistrer si dirty
    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey) || (e.key !== "s" && e.key !== "S")) return;
      if (!document.body.contains(videoForm)) return;
      if (videoForm.dataset.liveDirty !== "1") return;
      // Ne capture que si le focus est dans le panneau ou nulle part « spécial ».
      const ae = document.activeElement;
      if (ae && !videoForm.contains(ae) && ae !== document.body && ae !== document.documentElement) {
        // Autorise quand même si on est sur la page vidéo et le form dirty.
        if (!document.body.classList.contains("video-page")) return;
      }
      e.preventDefault();
      if (typeof videoForm.requestSubmit === "function") videoForm.requestSubmit();
      else videoForm.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    });

    // Échap → quitter le mode édition (avec restauration si dirty, comme Annuler).
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!document.body.contains(videoForm)) return;
      if (!editBlock || !editBlock.classList.contains("is-editing")) return;
      // Ne pas intercepter si un autre overlay/modal a déjà géré Escape.
      if (e.defaultPrevented) return;
      e.preventDefault();
      if (cancelBtn) cancelBtn.click();
      else exitEditMode();
    });

    videoForm.addEventListener("submit", (e) => {
      e.preventDefault();
      // Filet de sécurité : s'assurer d'être en mode édition (pause + déverrouillage).
      if (!editBlock || !editBlock.classList.contains("is-editing")) {
        enterEditMode();
      }
      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.dataset.savedLabel = "…";
        saveBtn.textContent = "…";
      }
      submitAjaxForm(videoForm, (res) => {
        // Défense en profondeur : si la page a entre-temps été remplacée
        // (navigation vers une autre vidéo pendant l'envoi, par exemple),
        // ce formulaire n'est plus dans le document — inutile (et
        // potentiellement trompeur) de continuer à le manipuler, la
        // nouvelle page affiche de toute façon déjà les bonnes données.
        if (!document.body.contains(videoForm)) return;

        // Reconstruit les puces du champ "Tags" à partir de la réponse
        // fraîche du serveur (id définitif pour tout nouveau tag, photo
        // bien enregistrée) — remplace tout état "en attente" local.
        const tagField = videoForm.querySelector(".js-tag-field");
        if (tagField && res.tags !== undefined) setTagFieldValue(tagField, res.tags);

        // Si la catégorie ou les playlists ont changé, les cases "Playlist /
        // Sous-playlist / Catégorie" sous le lecteur (titre + image) doivent
        // être mises à jour : on les reconstruit directement à partir des
        // valeurs renvoyées par le serveur, SANS recharger la page — un
        // rechargement couperait la lecture en cours et la ferait reprendre
        // depuis le début.
        const oldCategory = videoForm.dataset.initialCategory || "";
        const oldPlaylists = (videoForm.dataset.initialPlaylists || "").split(",").filter(Boolean);
        const newCategory = res.category_id !== undefined && res.category_id !== null ? String(res.category_id) : "";
        const newPlaylistsArr = res.playlist_ids !== undefined ? res.playlist_ids.map(String) : oldPlaylists;
        const newPlaylists = res.playlist_ids !== undefined ? res.playlist_ids.slice().sort((a, b) => a - b).join(",") : (videoForm.dataset.initialPlaylists || "");
        videoForm.dataset.initialCategory = newCategory;
        videoForm.dataset.initialPlaylists = newPlaylists;

        // Compteurs du panneau ☰ (et de la page Organiser) mis à jour tout
        // de suite, en temps réel, sans attendre un rechargement complet.
        applySidebarCountDeltas(oldCategory, newCategory, oldPlaylists, newPlaylistsArr);

        // Une vidéo peut appartenir à PLUSIEURS playlists/sous-playlists à
        // la fois : res.display_playlists / res.display_sub_playlists sont
        // désormais des LISTES (une carte par élément), reconstruites en
        // cascade comme côté serveur (voir video.html). res.display_category
        // reste un objet unique (une vidéo n'a qu'une seule catégorie).
        const cardsEl = document.getElementById("video-context-cards");
        if (cardsEl && (res.display_category !== undefined || res.display_playlists !== undefined || res.display_sub_playlists !== undefined)) {
          const subCards = (res.display_sub_playlists || []).map(
            (e) => buildContextCard(e, "Sous-playlist", "context-card-subplaylist", "📃")
          ).join("");
          const playlistCards = (res.display_playlists || []).map(
            (e) => buildContextCard(e, "Playlist", "context-card-playlist", "📃")
          ).join("");
          cardsEl.innerHTML =
            subCards +
            playlistCards +
            buildContextCard(res.display_category, "Catégorie", "context-card-category", "🗂");
        }

        // Bandeau "Lecture maintenant" : juste au-dessus de l'écran vidéo,
        // PAS superposé dessus (#player-overlay-top), et pas dans
        // #video-context-cards ni .video-actions — ne s'affiche QUE si la
        // vidéo est lue depuis un clic "lire" explicite sur une playlist /
        // sous-playlist / catégorie précise (res.now_playing_kind /
        // res.now_playing_entity, calculés côté serveur par
        // compute_now_playing_context — indépendant des cartes
        // d'appartenance ci-dessus, qui elles s'affichent toujours).
        const overlayTopEl = document.getElementById("player-overlay-top");
        if (overlayTopEl && res.now_playing_entity !== undefined) {
          overlayTopEl.innerHTML = buildNowPlayingCard(res.now_playing_entity, res.now_playing_kind);
        }

        const titleEl = document.getElementById("video-title-display");
        if (titleEl && res.title !== undefined) titleEl.textContent = res.title;
        document.title = res.title !== undefined ? res.title : document.title;

        const viewsEl = document.getElementById("video-views-display");
        if (viewsEl && res.views !== undefined) viewsEl.textContent = res.views;
        const ratingEl = document.getElementById("video-rating-display");
        if (ratingEl && res.rating !== undefined) {
          ratingEl.innerHTML = res.rating
            ? ` · <span class="stars-display">${starsHtml(res.rating)}</span>`
            : "";
        }

        const dateEl = document.getElementById("video-date-display");
        if (dateEl && res.video_date_display !== undefined) {
          dateEl.textContent = res.video_date_display ? " · " + res.video_date_display : "";
          dateEl.style.display = res.video_date_display ? "" : "none";
        }

        const descEl = document.getElementById("video-description-display");
        if (descEl && res.description !== undefined) {
          descEl.textContent = res.description;
          descEl.style.display = res.description ? "" : "none";
        }

        const commentEl = document.getElementById("video-comment-display");
        if (commentEl && res.comment !== undefined) {
          commentEl.textContent = res.comment ? "💬 " + res.comment : "";
          commentEl.style.display = res.comment ? "" : "none";
        }

        const tagsEl = document.getElementById("video-tags-display");
        if (tagsEl && res.tags !== undefined) {
          tagsEl.innerHTML = "";
          res.tags.forEach(t => {
            const a = document.createElement("a");
            a.href = "/tag/" + t.id;
            a.className = "tag-pill" + (t.image_url ? " tag-pill-with-image" : "");
            if (t.image_url) {
              const img = document.createElement("img");
              img.src = t.image_url;
              img.alt = "";
              img.loading = "lazy";
              img.className = "tag-pill-img";
              a.appendChild(img);
            }
            a.appendChild(document.createTextNode(t.name));
            tagsEl.appendChild(a);
          });
          tagsEl.style.display = res.tags.length ? "" : "none";
        }

        if (typeof window.syncTagsModalWithVideoTags === "function") {
          window.syncTagsModalWithVideoTags(res.tags || []);
        }

        // Métadonnées bien enregistrées en base : confirmation + retour
        // automatique en mode lecture (sidebar « À découvrir » réapparaît,
        // lecture reprise si elle tournait avant l'édition).
        markClean();
        if (saveBtn) {
          saveBtn.classList.add("is-saved");
          saveBtn.textContent = "✓ Enregistré";
          saveBtn.disabled = true;
          setTimeout(() => {
            if (!document.body.contains(saveBtn)) return;
            saveBtn.classList.remove("is-saved");
            saveBtn.textContent = saveBtn.dataset.defaultLabel || "✓ Enregistrer";
            syncDirtyUi();
          }, 1800);
        }
        showToast("Mise à jour effectuée");
        exitEditMode();
      });
    });
  }
}

// Les boutons de navigation du lecteur doivent rester utilisables même
// lorsque le lecteur est déplacé par le mini-lecteur ou qu'un autre module
// intercepte les liens internes. Le gestionnaire est attaché au conteneur
// (avant la délégation document de player-persist.js) et conserve la
// navigation douce sans dépendre d'un clic sur une zone précise de l'overlay.

// ============ Portail codec lourd (CPU 100 % évité) ============
// Si data-heavy-gate="1" : aucune source n'est chargée, pas d'autoplay,
// pas de transcodage. L'utilisateur choisit VLC/MPV, conversion, ou forcer.
function initHeavyCodecGate(root, signal) {
  root = root || document;
  const player = root.querySelector("#main-player");
  const gate = root.querySelector("#heavy-codec-gate");
  if (!player || !gate) return;
  if (gate.dataset.ready === "1") return;
  gate.dataset.ready = "1";

  const frame = root.querySelector("#video-frame") || player.closest(".video-frame");
  if (frame) frame.classList.add("heavy-gated");

  // Sécurité : arrêt total — aucune source, pas d'autoCompat résiduel
  try { delete player.dataset.autoCompat; } catch (e0) {}
  try { player.pause(); } catch (e) {}
  try {
    while (player.firstChild) player.removeChild(player.firstChild);
    player.removeAttribute("src");
    player.preload = "none";
    player.load();
  } catch (e) {}
  if (window.__guaranteeTimers && window.__guaranteeTimers.length) {
    window.__guaranteeTimers.forEach(function (t) { clearTimeout(t); });
    window.__guaranteeTimers = [];
  }

  // CORRECTIF gel post-scan : prépare discrètement la version compatible
  // après un court délai (laisse le scan / CPU se stabiliser). Si
  // l'utilisateur clique « Mode compatible » ou force puis gèle, la
  // conversion est déjà en cours ou prête. Annulé si le portail est
  // fermé avant (VLC choisi, navigation).
  var preTranscodeTimer = setTimeout(function () {
    if (!document.getElementById("heavy-codec-gate")) return;
    try {
      var startUrl = player.dataset.transcodeStartUrl;
      if (startUrl) {
        fetch(startUrl, {
          method: "POST",
          cache: "no-store",
          headers: window.__withCsrfHeaders ? window.__withCsrfHeaders({}) : {},
        }).catch(function () {});
      }
    } catch (ePre) {}
  }, 1800);
  if (signal) {
    signal.addEventListener("abort", function () { clearTimeout(preTranscodeTimer); });
  }

  const opts = signal ? { signal } : undefined;

  function dismissGate() {
    gate.remove();
    if (frame) frame.classList.remove("heavy-gated");
  }

  function loadAndPlay(url) {
    dismissGate();
    // Retire d'éventuelles <source> vides puis pose la vraie URL
    while (player.firstChild) player.removeChild(player.firstChild);
    const source = document.createElement("source");
    source.src = url;
    player.appendChild(source);
    player.preload = "auto";
    try { player.load(); } catch (e) {}
    const p = player.play();
    if (p && typeof p.catch === "function") p.catch(function () {});
  }

  const vlcBtn = gate.querySelector("#heavy-gate-vlc, .heavy-gate-external");
  if (vlcBtn) {
    vlcBtn.addEventListener("click", function () {
      // Arrêt total côté navigateur AVANT d'ouvrir VLC : aucune source
      // ne doit rester active (sinon double décodage = CPU qui grimpe).
      try { player.pause(); } catch (e) {}
      while (player.firstChild) player.removeChild(player.firstChild);
      try { player.removeAttribute("src"); player.load(); } catch (e) {}
      if (window.__guaranteeTimers && window.__guaranteeTimers.length) {
        window.__guaranteeTimers.forEach(function (t) { clearTimeout(t); });
        window.__guaranteeTimers = [];
      }
      if (window.__abortPlayerListeners) try { window.__abortPlayerListeners(); } catch (e) {}
      // Signaler au serveur : plus de lecture navigateur (stop heartbeat + kill transcode)
      try {
        fetch("/api/playback/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active: false }),
          keepalive: true
        }).catch(function () {});
      } catch (e) {}
      const id = vlcBtn.dataset.videoId || player.dataset.videoId;
      if (typeof openExternal === "function" && id) openExternal(id);
      else if (id) fetch("/open_external/" + id, { method: "POST" }).catch(function () {});
      showDeleteToast("Ouverture dans VLC / MPV — lecture navigateur arrêtée.");
      // Portail conservé : l'utilisateur peut encore forcer ou convertir.
      const title = gate.querySelector("h2");
      if (title) title.textContent = "Ouvert dans VLC / MPV";
      const hint = gate.querySelector(".heavy-codec-gate-hint, p");
      // ne pas casser le layout
    }, opts);
  }

  const compatBtn = gate.querySelector("#heavy-gate-compat, .heavy-gate-compat");
  if (compatBtn) {
    compatBtn.addEventListener("click", function () {
      compatBtn.disabled = true;
      compatBtn.textContent = "Conversion en cours…";
      dismissGate();
      // Mode compatible uniquement sur action utilisateur
      player.dataset.autoCompat = "1";
      if (typeof window.__tryTranscodeFallback === "function") {
        window.__tryTranscodeFallback();
      } else {
        const startUrl = player.dataset.transcodeStartUrl;
        if (startUrl) fetch(startUrl, { method: "POST" }).catch(function () {});
        showDeleteToast("Conversion lancée — le lecteur basculera quand ce sera prêt.");
      }
    }, opts);
  }

  const forceBtn = gate.querySelector("#heavy-gate-force, .heavy-gate-force");
  if (forceBtn) {
    forceBtn.addEventListener("click", function () {
      const url = player.dataset.streamUrl;
      if (!url) {
        showDeleteToast("URL de flux introuvable.", true);
        return;
      }
      // CORRECTIF gel codecs lourds : même en "forcer", on active le
      // basculement automatique vers le mode compatible dès le moindre
      // gel (~1,5 s grâce aux seuils accélérés de player-smooth.js).
      // Sans ça, le décodage logiciel pouvait saturer le CPU plusieurs
      // secondes avant qu'une intervention ne soit proposée.
      player.dataset.autoCompat = "1";
      loadAndPlay(url);
    }, opts);
  }
}

function initPlayerNavigation(root, signal) {
  const nav = (root || document).querySelector("#player-nav-buttons");
  if (!nav || nav.dataset.navigationReady) return;
  nav.dataset.navigationReady = "1";
  const listenerOpts = signal ? { signal } : undefined;
  nav.addEventListener("click", (event) => {
    if (event.defaultPrevented) return;
    const link = event.target.closest("a.player-nav-prev, a.player-nav-next");
    if (!link || !link.href) return;
    event.preventDefault();
    event.stopPropagation();
    // Anti double-clic pendant un soft-nav (surtout codec lourd)
    if (nav.dataset.navBusy === "1") return;
    nav.dataset.navBusy = "1";
    setTimeout(function () { try { delete nav.dataset.navBusy; } catch (e) {} }, 1200);
    if (typeof window.__softNavigateToVideo === "function") {
      window.__softNavigateToVideo(new URL(link.href, location.href).pathname +
        new URL(link.href, location.href).search);
    } else {
      window.location.assign(link.href);
    }
  }, listenerOpts);
}

// Reprise de lecture locale, sans requête ni écriture en base à chaque
// seconde. La position est propre à chaque fichier et est supprimée quand la
// vidéo est réellement terminée.
function initPlaybackResume(root, signal) {
  const player = (root || document).querySelector("#main-player");
  if (!player) return;
  const id = player.dataset.videoId;
  if (!id) return;
  // Lecteur réutilisé : ré-init si l'id vidéo a changé (next/prev soft-nav)
  if (player.dataset.resumeReady === "1" && player.dataset.resumeForId === String(id)) return;
  player.dataset.resumeReady = "1";
  player.dataset.resumeForId = String(id);
  const key = "mediatheque_resume_" + id;
  let lastSaved = 0;
  const opts = signal ? { signal } : undefined;
  const save = () => {
    if (!player.duration || !isFinite(player.duration) || player.currentTime < 2) return;
    const now = Date.now();
    if (now - lastSaved < 2000) return;
    lastSaved = now;
    try {
      localStorage.setItem(key, JSON.stringify({
        time: player.currentTime,
        duration: player.duration,
        savedAt: Date.now()
      }));
    } catch (e) {}
  };
  const restore = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "null");
      if (!saved || !isFinite(saved.time) || !isFinite(player.duration)) return;
      // Une position devenue incohérente après remplacement du fichier ne
      // doit jamais provoquer un seek hors limites.
      if (saved.time > 5 && saved.time < player.duration - 15 &&
          (!saved.duration || Math.abs(saved.duration - player.duration) < 3)) {
        player.currentTime = Math.min(player.duration - 1, saved.time);
      }
    } catch (e) {}
  };
  player.addEventListener("loadedmetadata", restore, opts);
  player.addEventListener("timeupdate", save, opts);
  player.addEventListener("pause", save, opts);
  player.addEventListener("ended", () => {
    try { localStorage.removeItem(key); } catch (e) {}
  }, opts);
  if (player.readyState >= 1) restore();
}

// ============ Upload photo inline sur les cases Organiser (catégorie /
// playlist) : clic ou glisser-déposer sur la couverture, comme pour les tags.
// Envoie uniquement image1 + ajax=1 ; le serveur conserve nom/note/commentaire.
(function initOrgInstantCoverUpload() {
  function setCover(card, imageUrl) {
    const slot = card.querySelector(".js-org-instant-upload");
    if (!slot) return;
    let bg = slot.querySelector(".org-cover-bg");
    let main = slot.querySelector(".org-cover-main");
    const placeholder = slot.querySelector(".org-cover-placeholder");
    const hint = slot.querySelector(".org-cover-upload-hint");
    if (!bg || !main) {
      if (placeholder) placeholder.remove();
      bg = document.createElement("img");
      bg.className = "org-cover-bg";
      bg.alt = "";
      bg.setAttribute("aria-hidden", "true");
      main = document.createElement("img");
      main.className = "org-cover-main";
      main.alt = "";
      const anchor = hint || slot.firstChild;
      slot.insertBefore(bg, anchor);
      slot.insertBefore(main, anchor);
    }
    bg.src = imageUrl;
    main.src = imageUrl;
    card.dataset.hasPhoto = "1";
    // Aperçu dans le formulaire d'édition ouvert éventuel
    const formSlot = card.querySelector(".org-image-slot-single");
    if (formSlot) {
      let img = formSlot.querySelector("img");
      if (!img) {
        formSlot.querySelectorAll(".org-image-placeholder").forEach(el => el.remove());
        img = document.createElement("img");
        img.alt = "";
        formSlot.insertBefore(img, formSlot.querySelector("input"));
      }
      img.src = imageUrl;
    }
  }

  function upload(card, type, id, file) {
    if (!file || !type || !id) return;
    if (card.classList.contains("org-card-uploading")) return; // 1 upload à la fois
    if (file.size > 8 * 1024 * 1024) {
      alert("Image trop lourde (max 8 Mo).");
      return;
    }
    const allowed = /\.(jpe?g|png|webp|gif)$/i;
    if (!allowed.test(file.name || "")) {
      alert("Formats acceptés : JPG, PNG, WEBP, GIF.");
      return;
    }
    card.classList.add("org-card-uploading");
    const data = new FormData();
    data.append("ajax", "1");
    data.append("image1", file, file.name);
    const url = type === "category"
      ? `/category/${id}/full_edit`
      : `/playlist/${id}/full_edit`;
    fetch(url, { method: "POST", body: data, cache: "no-store" })
      .then(r => r.json().then(json => ({ status: r.status, json })))
      .then(({ status, json }) => {
        if (status >= 200 && status < 300 && json && json.ok && json.image_url) {
          setCover(card, json.image_url);
          if (typeof applyEntityImageUpdate === "function") {
            applyEntityImageUpdate(type, id, json.image_url);
          }
          if (typeof showToast === "function") {
            showToast("Photo mise à jour");
          }
        } else {
          throw new Error((json && json.error) || `HTTP ${status}`);
        }
      })
      .catch(err => alert("Impossible d'enregistrer cette image : " + err.message))
      .finally(() => card.classList.remove("org-card-uploading"));
  }

  // Clic sur la couverture (hors bouton play) → ouvre le sélecteur de fichier.
  // Nécessaire car le label + overlay play peut empêcher le comportement natif.
  document.addEventListener("click", (e) => {
    if (e.target.closest(".org-play-overlay, .org-play-icon")) return;
    const slot = e.target.closest(".js-org-instant-upload");
    if (!slot) return;
    const input = slot.querySelector("input[type='file']");
    if (!input) return;
    // Laisser le label natif agir si possible ; forcer sinon.
    if (e.target === input) return;
    e.preventDefault();
    e.stopPropagation();
    input.click();
  }, true);

  document.addEventListener("change", (e) => {
    const input = e.target.closest(".js-org-instant-upload input[type='file']");
    if (!input) return;
    const file = input.files && input.files[0];
    const slot = input.closest(".js-org-instant-upload");
    const card = input.closest(".org-card");
    if (!file || !slot || !card) return;
    upload(card, slot.dataset.entityType, slot.dataset.entityId, file);
    input.value = "";
  });

  document.addEventListener("dragover", (e) => {
    const slot = e.target.closest(".js-org-instant-upload");
    if (!slot) return;
    e.preventDefault();
    slot.classList.add("drag-over");
  });
  document.addEventListener("dragleave", (e) => {
    const slot = e.target.closest(".js-org-instant-upload");
    if (slot) slot.classList.remove("drag-over");
  });
  document.addEventListener("drop", (e) => {
    const slot = e.target.closest(".js-org-instant-upload");
    if (!slot) return;
    e.preventDefault();
    slot.classList.remove("drag-over");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    const card = slot.closest(".org-card");
    if (!file || !card) return;
    if (!file.type || !file.type.startsWith("image/")) {
      alert("Choisis un fichier image.");
      return;
    }
    upload(card, slot.dataset.entityType, slot.dataset.entityId, file);
  });
})();

// ============ Cercle "play" au survol des cases catégorie / playlist /
// sous-playlist (page "Organiser" et sous-playlists de la page playlist) :
// clique dessus pour lire tout de suite la première vidéo, au lieu d'ouvrir
// la page de la catégorie/playlist. On intercepte le clic avant qu'il ne
// remonte au lien "org-card-link" qui englobe toute la case. ============
document.addEventListener("click", (e) => {
  const playBtn = e.target.closest(".org-play-overlay");
  if (!playBtn) return;
  e.preventDefault();
  e.stopPropagation();
  const url = playBtn.dataset.playUrl;
  if (url) window.location.href = url;
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const playBtn = e.target.closest(".org-play-overlay");
  if (!playBtn) return;
  e.preventDefault();
  e.stopPropagation();
  const url = playBtn.dataset.playUrl;
  if (url) window.location.href = url;
});

// ============ Bouton favori/♥ (cartes de grille + page vidéo) ============
// Écouteur délégué au document : fonctionne aussi bien au chargement
// initial que sur les cartes injectées après coup (scan en direct via
// live-sync.js, navigation instantanée via player-persist.js), sans
// dépendre d'un ré-init par page. Bascule côté serveur en AJAX (voir
// /video/<id>/favorite/toggle dans app.py) et met à jour l'icône
// immédiatement, sans recharger la page.
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".js-fav-toggle");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  if (btn.classList.contains("fav-toggling")) return;
  const videoId = btn.dataset.videoId;
  if (!videoId) return;

  btn.classList.add("fav-toggling");
  fetch(`/video/${videoId}/favorite/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "ajax=1",
  })
    .then(r => r.json())
    .then(res => {
      if (!res || !res.ok) return;
      // Toutes les cartes/boutons de CETTE vidéo présents sur la page
      // (grille + éventuel bouton de la page détail) sont synchronisés
      // ensemble, au cas où la même vidéo apparaîtrait plusieurs fois
      // (ex. section "À découvrir aussi").
      document.querySelectorAll(`.js-fav-toggle[data-video-id="${videoId}"]`).forEach(b => {
        b.classList.toggle("is-fav", !!res.favorite);
        b.setAttribute("aria-pressed", res.favorite ? "true" : "false");
        b.title = res.favorite ? "Retirer des favoris" : "Ajouter aux favoris";
      });
    })
    .catch(() => {
      // Échec réseau : on ne change rien à l'affichage, l'état réel côté
      // serveur reste celui d'avant le clic.
    })
    .finally(() => {
      btn.classList.remove("fav-toggling");
    });
});

// ============ Cadrage fin du lecteur vidéo (écran de lecture en cours) ============
// Le CSS existant limite la hauteur de la vidéo avec des unités "vh" figées.
// Problème constaté : selon le format de la vidéo, la résolution d'écran, le
// zoom du navigateur ou la barre des tâches Windows (qui réduit la zone
// réellement visible, notamment en auto-masquage ou sur config multi-écrans),
// un bord de la vidéo peut se retrouver masqué. On calcule donc ici, en JS,
// la hauteur réellement disponible entre le haut de la vidéo et le bas de
// l'écran, recalculée à chaque redimensionnement, puis on l'applique en style
// inline (qui prime sur les "max-height" du CSS, mais reste sans effet si le
// script ne s'exécute pas : le comportement CSS actuel reste alors le
// fallback). Un bouton "Plein écran" natif est aussi ajouté : c'est la seule
// garantie absolue qu'aucun bord ne soit jamais masqué (le plein écran
// navigateur recouvre entièrement l'écran, barre des tâches Windows incluse).
//
// CORRECTIF (écran + infos figés, sans scrollbar parasite) : sur la page
// vidéo (body.video-page), #player-sticky-col (l'écran) et tout ce qui suit
// juste en dessous (titre, ligne d'infos, bloc "Modifier les métadonnées",
// chemin du fichier) vivent ensemble dans .player-main, qui est un simple
// bloc "position: sticky" — SANS plafond ni scrollbar à lui (voir le bloc
// CSS "Écran de lecture + infos + bloc métadonnées FIGÉS..." dans
// static/css/style.css et son historique des essais précédents : plafonner
// .player-main lui-même faisait apparaître une scrollbar verticale collée à
// l'écran dès que vidéo + infos dépassaient la hauteur de la fenêtre — ce
// qui arrivait presque toujours vu la hauteur permise à la vidéo).
// Pour que ce bloc tienne entièrement dans la fenêtre SANS le moindre
// scroll (ni scrollbar propre à .player-main, ni scroll de la page), on
// mesure ici la place RÉELLEMENT prise par tout ce qui suit l'écran dans
// .player-main, et on retire cette place de la hauteur allouée à la vidéo.
// Ainsi, écran + infos restent toujours visibles ensemble d'un seul bloc,
// et rien à gauche ne peut jamais défiler quoi que ce soit fasse la colonne
// de droite (.player-related, elle, reste seule à défiler pour son propre
// compte). Sans .player-main (autres contextes éventuels), on retombe sur
// le calcul plein-fenêtre d'origine.
function initPlayerFineFit(root, signal) {
  root = root || document;
  const player = root.querySelector("#main-player");
  if (!player) return;

  function fitPlayer() {
    if (document.fullscreenElement) return; // le mode plein écran gère sa propre taille
    const rect = player.getBoundingClientRect();
    const topOffset = Math.max(rect.top, 0);
    // Marge de sécurité réduite au minimum utile (hauteur maximale
    // possible) : le calcul de "belowHeight" ci-dessous mesure maintenant
    // le bord réel (marges/espacements compris, plus seulement la somme
    // des hauteurs de boîte), donc plus besoin d'une grosse marge pour
    // absorber les écarts entre blocs — elle ne sert plus qu'à éviter de
    // coller pile au bord de la fenêtre / à la barre des tâches Windows.
    const safetyMargin = 10;

    let belowHeight = 0;
    const stickyCol = player.closest(".player-sticky-col");
    const mainPane = stickyCol ? stickyCol.closest(".player-main") : null;
    if (mainPane && stickyCol) {
      // La "vue d'ensemble sans scroll" ne concerne plus que l'écran + le
      // bloc titre/infos juste en dessous (#player-fit-group : titre, ligne
      // "durée · résolution · poids · vues · date", "ⓘ Informations de
      // lecture") — voir video.html. Les cases playlist/sous-playlist/
      // catégorie rattachées, les actions ("Ouvrir avec…", "Modifier les
      // métadonnées"…), la description/les tags et le panneau d'édition,
      // plus bas dans le DOM, n'entrent plus dans ce calcul : ils peuvent
      // désormais dépasser la hauteur de la fenêtre sans réduire la taille
      // de l'écran, la page défilant alors normalement pour les atteindre
      // (l'écran restant collé en haut via #player-sticky-col pendant ce
      // défilement). Mesuré du bas réel de #player-fit-group (donc marges/
      // espacements compris) jusqu'au bas de l'écran vidéo, plutôt qu'en
      // additionnant des hauteurs une par une (qui ignorerait les marges).
      const fitGroup = mainPane.querySelector("#player-fit-group");
      if (fitGroup && window.getComputedStyle(fitGroup).display !== "none") {
        const stickyBottom = stickyCol.getBoundingClientRect().bottom;
        const groupBottom = fitGroup.getBoundingClientRect().bottom;
        belowHeight = Math.max(0, groupBottom - stickyBottom);
      }
    }

    // Sans .player-main (contexte différent), pas d'infos à réserver sous
    // la vidéo : comportement plein-fenêtre d'origine.
    const available = window.innerHeight - topOffset - belowHeight - safetyMargin;
    const clamped = Math.max(200, Math.floor(available));
    // Hauteur exacte (et non max-height) : la boîte du lecteur a alors sa
    // propre taille indépendante du ratio naturel de la vidéo, ce qui
    // permet à object-fit: cover (voir style.css, mode "ADAPTE" façon
    // MX Player) de remplir entièrement l'écran en recadrant l'image
    // plutôt que de laisser apparaître des bandes noires.
    player.style.height = clamped + "px";
  }

  let pending = null;
  function scheduleFit() {
    if (pending) cancelAnimationFrame(pending);
    pending = requestAnimationFrame(fitPlayer);
  }

  const listenerOpts = signal ? { signal } : undefined;
  window.addEventListener("resize", scheduleFit, listenerOpts);
  window.addEventListener("orientationchange", scheduleFit, listenerOpts);
  window.addEventListener("load", scheduleFit, listenerOpts);
  player.addEventListener("loadedmetadata", scheduleFit, listenerOpts);
  document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement) {
      player.style.height = ""; // laisse le CSS :fullscreen prendre le relais
    } else {
      scheduleFit();
    }
  }, listenerOpts);
  scheduleFit();

  // ---- Bouton plein écran natif ----
  const speedControls = root.querySelector(".speed-controls");
  if (speedControls && !speedControls.querySelector("#player-fullscreen-btn")) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "player-fullscreen-btn";
    btn.className = "speed-btn player-fullscreen-btn";
    btn.title = "Plein écran (garantit que la vidéo entière soit visible)";
    btn.textContent = "⛶ Plein écran";
    btn.addEventListener("click", () => {
      if (document.fullscreenElement) {
        if (document.exitFullscreen) document.exitFullscreen();
        return;
      }
      // On met en plein écran le CADRE (.video-frame), pas le <video> seul :
      // la timeline et les boutons personnalisés (lecture/pause, précédent/
      // suivant, aléatoire, volume...) sont des éléments frères de la vidéo à
      // l'intérieur de ce cadre, et doivent donc en faire partie pour rester
      // visibles par-dessus la vidéo en plein écran (voir style.css).
      const frame = root.querySelector("#video-frame") || player.closest(".video-frame") || player;
      if (frame.requestFullscreen) {
        frame.requestFullscreen();
      } else if (frame.webkitRequestFullscreen) {
        frame.webkitRequestFullscreen();
      } else if (player.webkitEnterFullscreen) {
        // Safari iOS : pas d'API plein écran générique disponible pour un
        // conteneur, seul le plein écran natif du lecteur vidéo l'est.
        player.webkitEnterFullscreen();
      }
    });
    speedControls.appendChild(btn);
  }
}

// ============ Lecture immédiate garantie (page vidéo) : l'attribut HTML
// "autoplay" suffit la plupart du temps, mais certains navigateurs bloquent
// le démarrage automatique AVEC le son (politique d'autoplay), même après un
// clic explicite de l'utilisateur sur la carte vidéo. Dans ce cas la vidéo
// reste simplement en pause sans rien signaler. Ce bloc tente une lecture
// normale, et si elle est refusée, retente en muet (toujours autorisé) puis
// coupe le mute juste après le démarrage effectif (autorisé une fois la
// lecture commencée). Purement additif : n'interfère pas avec l'attribut
// autoplay existant ni avec l'enchaînement automatique en fin de vidéo. ============
function initGuaranteedAutoplay(root, signal) {
  root = root || document;
  const player = root.querySelector("#main-player");
  if (!player) return;
  // Codec lourd : ne jamais forcer play() (décodage logiciel = CPU 100 %).
  if (player.getAttribute("data-heavy-gate") === "1" || player.dataset.heavyGate === "1") return;
  if (document.getElementById("heavy-codec-gate")) return;
  // Pause volontaire de l'utilisateur : ne pas relancer automatiquement.
  if (window.__userPausedPlayback) return;

  function tryUnmutedThenFallback() {
    if (window.__userPausedPlayback) return;
    const attempt = player.play();
    if (!attempt || typeof attempt.then !== "function") return;
    attempt.catch(() => {
      if (window.__userPausedPlayback) return;
      // Lecture avec son refusée par le navigateur : on démarre en muet
      // (toujours permis), puis on retire le mute dès que possible.
      player.muted = true;
      player.play().then(() => {
        if (window.__userPausedPlayback) {
          try { player.pause(); } catch (e) { /* ignore */ }
          return;
        }
        const unmute = () => {
          player.muted = false;
          player.removeEventListener("playing", unmute);
        };
        player.addEventListener("playing", unmute, signal ? { signal } : undefined);
      }).catch(() => { /* aucune lecture possible (format, etc.) : rien à faire de plus ici */ });
    });
  }

  if (player.readyState >= 2) {
    tryUnmutedThenFallback();
  } else {
    const opts = { once: true };
    if (signal) opts.signal = signal;
    player.addEventListener("loadeddata", tryUnmutedThenFallback, opts);
  }
}

// ============ Bouton "lecture aléatoire 🔀" (page vidéo) : quand actif, les
// boutons "Précédent"/"Suivant" piochent une vidéo au hasard parmi celles du
// même contexte (playlist/sous-playlist, catégorie, ou toutes les vidéos)
// au lieu de suivre l'ordre habituel. L'état du bouton est mémorisé
// globalement (localStorage), et un historique de la "session aléatoire"
// est mémorisé par contexte (sessionStorage) pour que "Précédent" revienne
// sur les vidéos déjà tirées au hasard, comme un vrai historique de
// lecture, plutôt que de re-tirer une vidéo différente à chaque clic. ============
function initShufflePlayback(root, signal) {
  root = root || document;
  const listenerOpts = signal ? { signal } : undefined;
  const nav = root.querySelector("#player-nav-buttons");
  const toggleBtn = root.querySelector("#shuffle-toggle-btn");
  if (!nav || !toggleBtn) return;

  const SHUFFLE_ACTIVE_KEY = "mediatheque_shuffle_active";
  const videoId = parseInt(nav.dataset.videoId, 10);
  const playlistCtx = nav.dataset.playlistCtx || "";
  const categoryCtx = nav.dataset.categoryCtx || "";
  const ctxKey = playlistCtx ? `pl:${playlistCtx}` : (categoryCtx ? `cat:${categoryCtx}` : "all");
  const histKey = "mediatheque_shuffle_hist_" + ctxKey;

  function isActive() { return localStorage.getItem(SHUFFLE_ACTIVE_KEY) === "1"; }
  function setActive(v) {
    try { localStorage.setItem(SHUFFLE_ACTIVE_KEY, v ? "1" : "0"); } catch (e) { /* stockage indisponible : le bouton reste "actif" pour l'onglet en cours seulement */ }
  }

  function loadHist() {
    try {
      const raw = sessionStorage.getItem(histKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.list) && typeof parsed.pointer === "number") return parsed;
      }
    } catch (e) { /* stockage indisponible : on repart d'un historique vide */ }
    return { list: [], pointer: -1 };
  }
  function saveHist(h) {
    try { sessionStorage.setItem(histKey, JSON.stringify(h)); } catch (e) { /* tant pis, l'historique ne sera pas mémorisé */ }
  }

  // Enregistre la vidéo actuelle dans l'historique de ce contexte si elle n'y
  // est pas déjà à la position courante (arrivée normale, contexte
  // fraîchement créé...) : garantit qu'un historique existe toujours, même
  // si l'utilisateur active le mode aléatoire en cours de route.
  const hist = loadHist();
  if (hist.pointer < 0 || hist.list[hist.pointer] !== videoId) {
    hist.list = hist.list.slice(0, hist.pointer + 1);
    hist.list.push(videoId);
    hist.pointer = hist.list.length - 1;
    saveHist(hist);
  }

  function applyButtonState() {
    const active = isActive();
    toggleBtn.classList.toggle("active", active);
    nav.classList.toggle("shuffle-mode", active);
    const overlay = nav.closest("#player-overlay-bottom");
    if (overlay) overlay.classList.toggle("shuffle-mode", active);
  }
  applyButtonState();

  function goTo(id) {
    const params = new URLSearchParams();
    if (playlistCtx) params.set("playlist", playlistCtx);
    if (categoryCtx) params.set("category", categoryCtx);
    const qs = params.toString();
    const url = `/video/${id}${qs ? "?" + qs : ""}`;
    if (window.__softNavigateToVideo) {
      window.__softNavigateToVideo(url);
    } else {
      window.location.href = url;
    }
  }

  toggleBtn.addEventListener("click", () => {
    setActive(!isActive());
    applyButtonState();
  }, listenerOpts);

  const nextEl = nav.querySelector(".player-nav-next");
  const prevEl = nav.querySelector(".player-nav-prev");

  if (nextEl) {
    nextEl.addEventListener("click", (e) => {
      if (!isActive()) return;
      e.preventDefault();
      if (hist.pointer < hist.list.length - 1) {
        hist.pointer += 1;
        saveHist(hist);
        goTo(hist.list[hist.pointer]);
        return;
      }
      const params = new URLSearchParams();
      if (playlistCtx) params.set("playlist", playlistCtx);
      if (categoryCtx) params.set("category", categoryCtx);
      fetch(`/api/shuffle_video/${videoId}?${params.toString()}`, { cache: "no-store" })
        .then(r => r.json())
        .then(data => {
          if (data && data.id) {
            hist.list = hist.list.slice(0, hist.pointer + 1);
            hist.list.push(data.id);
            hist.pointer = hist.list.length - 1;
            saveHist(hist);
            goTo(data.id);
          }
        })
        .catch(() => { /* pas de vidéo disponible : on reste sur la vidéo actuelle */ });
    }, listenerOpts);
  }

  if (prevEl) {
    prevEl.addEventListener("click", (e) => {
      if (!isActive()) return;
      e.preventDefault();
      if (hist.pointer > 0) {
        hist.pointer -= 1;
        saveHist(hist);
        goTo(hist.list[hist.pointer]);
      }
      // Sinon : aucune vidéo antérieure connue dans cette session aléatoire,
      // on reste simplement sur la vidéo actuelle.
    }, listenerOpts);
  }

  // ---- Enchaînement automatique en fin de lecture (façon YouTube) ----
  // À la fin de la vidéo, on déclenche un clic simulé sur le bouton
  // "Suivant" : celui-ci applique déjà la bonne logique selon le contexte
  // du bouton 🔀 Aléatoire (vidéo aléatoire du contexte si actif, sinon
  // vidéo suivante normale). Si aucune vidéo suivante n'existe et que le
  // mode aléatoire est inactif, le bouton est désactivé et ne fait rien :
  // la lecture s'arrête simplement, comme attendu.
  //
  // Le lecteur (#main-player) peut être le MÊME nœud DOM que celui d'une
  // vidéo précédente (changement de vidéo sans rechargement de page, voir
  // player-persist.js) : le paramètre "signal" garantit qu'à chaque nouvel
  // appel, les écouteurs de la vidéo précédente sont proprement coupés
  // avant d'en attacher de nouveaux (pas de cumul, pas de double
  // déclenchement de "Suivant" en fin de lecture).
  const player = root.querySelector("#main-player");
  if (player && nextEl) {
    player.addEventListener("ended", () => {
      nextEl.click();
    }, listenerOpts);
  }
}

// ============================================================================
// Point d'entrée unique de (ré)initialisation.
//
// Toutes les fonctions ci-dessus qui ciblent des éléments situés dans
// .layout (panneau latéral + contenu) ont été rendues "root-scoped" et
// rappelables sans effet de bord (gardes d'idempotence sur les éléments qui
// persistent, ou re-liaison sur des éléments neufs après un remplacement de
// contenu). Le bandeau du haut et les fenêtres modales de base.html, eux, ne
// changent jamais et gardent leur initialisation unique habituelle, plus
// haut dans ce fichier.
//
// Cette fonction est appelée une première fois ci-dessous, exactement comme
// au chargement normal de la page jusqu'ici. Elle est aussi appelée par
// static/js/player-persist.js après chaque navigation instantanée (clic
// dans le panneau latéral, recherche, changement de vidéo, etc.), une fois
// le nouveau contenu inséré dans le DOM, pour que tout redevienne pleinement
// fonctionnel sur la page affichée — sans jamais interrompre une lecture
// vidéo en cours.
// ============================================================================
let __playerListenersAbort = new AbortController();
function __resetPlayerListeners() {
  __playerListenersAbort.abort();
  __playerListenersAbort = new AbortController();
  return __playerListenersAbort.signal;
}
// Coupe immédiatement tout ce qui était accroché au lecteur pour la vidéo
// PRÉCÉDENTE (écouteurs, minuteurs, sondages de conversion, préchauffage en
// cours...) SANS rien réattacher. Appelé par player-persist.js juste AVANT
// de changer la source du lecteur réutilisé : sans cette coupure préalable,
// les mécanismes de secours de la vidéo précédente pouvaient encore réagir
// au .load() de la nouvelle vidéo et la laisser figée.
window.__abortPlayerListeners = function () {
  __playerListenersAbort.abort();
  __playerListenersAbort = new AbortController();
};

// ---------------------------------------------------------------------------
// CORRECTIF FIABILITÉ : ISOLATION DES MODULES D'INITIALISATION DE PAGE
// ---------------------------------------------------------------------------
// initDynamicPage() enchaîne une vingtaine de modules indépendants (champ de
// tags, champ de catégorie, note en étoiles, filtres, lecteur vidéo...).
// Auparavant, ils étaient appelés en séquence SANS filet : la moindre
// exception non prévue dans UN SEUL de ces modules (ex. un cas de données
// particulier sur une vidéo précise) interrompait immédiatement toute la
// fonction — tous les modules suivants dans la liste n'étaient alors JAMAIS
// initialisés. Comme le lecteur vidéo (bouton lecture/pause, précédent/
// suivant, volume, timeline) et le formulaire "Modifier les métadonnées"
// sont attachés en toute fin de liste, un problème survenu dans n'importe
// quel module cité AVANT eux (champ de tags, catégorie, notes, filtres...)
// pouvait à lui seul rendre TOUT le bas du lecteur inerte (pause sans
// effet, précédent/suivant morts) ET empêcher l'enregistrement des
// métadonnées de fonctionner, sans lien apparent entre les deux symptômes
// pour qui ne voit que le résultat.
//
// runInit() isole chaque module : une exception dans l'un d'eux est
// consignée dans la console (pour diagnostic) mais n'empêche plus JAMAIS
// les modules suivants — en particulier ceux du lecteur vidéo — de
// s'initialiser normalement. Comportement strictement identique quand tout
// se passe bien (aucun changement visible) ; seul le cas d'erreur devient
// robuste au lieu de tout paralyser en cascade.
function runInit(label, fn) {
  try {
    fn();
  } catch (err) {
    console.error("[médiathèque] échec du module d'initialisation \"" + label + "\" (le reste de la page continue normalement) :", err);
  }
}

function initDynamicPage(root) {
  root = root || document;
  runInit("sidebarTabs", () => initSidebarTabs(root));
  runInit("playlistField", () => root.querySelectorAll(".js-playlist-field").forEach(initPlaylistField));
  runInit("sideQuickCreateForms", () => initSideQuickCreateForms(root));
  runInit("manualDateToggle", () => initManualDateToggle(root));
  runInit("starRatingInputs", () => initStarRatingInputs(root));
  runInit("organizeGrids", () => initOrganizeGrids(root));
  runInit("hoverPreviews", () => initHoverPreviews(root));
  runInit("relatedThumbFallback", () => initRelatedThumbFallback(root));
  runInit("filterSearchSuggestions", () => initFilterSearchSuggestions(root));
  runInit("dynamicFilters", () => initDynamicFilters(root));
  runInit("filterPanelsCollapseMemory", () => initFilterPanelsCollapseMemory(root));
  runInit("videoDateQuickTools", () => initVideoDateQuickTools(root));
  runInit("bulkSelection", () => initBulkSelection(root));
  runInit("coverImagePreview", () => initCoverImagePreview(root));
  runInit("tagField", () => initTagField(root));
  runInit("categoryField", () => initCategoryField(root));
  runInit("realtimeEditForms", () => initRealtimeEditForms(root));
  runInit("videoDeleteButton", () => initVideoDeleteButton(root));
  runInit("folderBrowse", () => initFolderBrowse(root));

  // Fonctions liées au lecteur vidéo : le nœud #main-player peut être le
  // MÊME élément DOM persistant d'un appel à l'autre (changement de vidéo
  // sans rechargement de page). On coupe donc systématiquement les
  // écouteurs de l'appel précédent avant d'en attacher de nouveaux, pour
  // éviter tout cumul/double déclenchement.
  const signal = __resetPlayerListeners();
  runInit("speedControls", () => initSpeedControls(root));
  runInit("heavyCodecGate", () => initHeavyCodecGate(root, signal));
  runInit("playerFineFit", () => initPlayerFineFit(root, signal));
  runInit("guaranteedAutoplay", () => initGuaranteedAutoplay(root, signal));
  runInit("shufflePlayback", () => initShufflePlayback(root, signal));
  // Fiabilité (récupération auto en cas de plantage/gel) + glissage fluide
  // de la timeline : module additif, voir static/js/player-smooth.js.
  runInit("smoothPlayer", () => { if (window.__initSmoothPlayer) window.__initSmoothPlayer(root, signal); });
  // Contrôle de volume personnalisé façon YouTube (voir
  // static/js/player-controls.js) : additif, remplace uniquement
  // l'affichage du volume natif, jamais les autres contrôles natifs.
  runInit("playerVolume", () => { if (window.__initPlayerVolume) window.__initPlayerVolume(root, signal); });
  // Bouton lecture/pause personnalisé + masquage auto de la timeline/des
  // boutons en plein écran (voir static/js/player-ui.js). Placé en tout
  // dernier mais désormais protégé par runInit() comme tout le reste : il
  // s'initialise toujours, même si un module précédent a échoué.
  runInit("playerUI", () => { if (window.__initPlayerUI) window.__initPlayerUI(root, signal); });
  runInit("playerNavigation", () => initPlayerNavigation(root, signal));
  runInit("playbackResume", () => initPlaybackResume(root, signal));

  // Signal générique pour tout module additif (non listé ci-dessus) qui a
  // besoin de se relier au DOM fraîchement (re)inséré à chaque navigation
  // douce / rafraîchissement temps réel — voir static/js/player-next-preload.js,
  // qui en dépend pour ne pas rester accroché à des boutons ⏮/⏭ devenus
  // fantômes après un remplacement de page.
  document.dispatchEvent(new CustomEvent("mediatheque:dynamic-page-init", { detail: { root } }));
}
window.__initDynamicPage = initDynamicPage;

initDynamicPage(document);

// ============================================================================
// Masquage des boutons "✎ Modifier les métadonnées" des cartes de la grille
// PENDANT qu'un scan des disques est en cours.
//
// Un scan en cours peut faire apparaître de nouvelles cartes vidéo en temps
// réel (voir live-sync.js + live_updates.py) avant que leurs métadonnées
// (durée, vignette, résolution...) ne soient complètement stabilisées :
// on masque donc ce bouton sur toutes les cartes tant que le scan tourne,
// et on le réaffiche automatiquement dès qu'il est terminé — voir la règle
// CSS "body.scan-in-progress .js-quick-edit" dans style.css. Ce sondage est
// volontairement indépendant de la page affichée (fonctionne partout : "/",
// catégorie, playlist, recherche...) et de initDynamicPage/la navigation
// douce, puisqu'il ne dépend que de l'état global du scan côté serveur.
// ============================================================================
// Disques actifs — délégation persistante (survit à la navigation douce).
// Le script inline de settings.html ne s'exécute PAS après soft-nav
// (innerHTML / DOMParser n'exécute pas les <script>). D'où l'absence de
// POST /api/disk-filter dans les logs quand on arrive via le menu.
// ============================================================================
(function () {
  "use strict";
  var debounceTimer = null;
  var generation = 0;

  function collectDisabled(form) {
    var disabled = [];
    var boxes = form.querySelectorAll("input[type=checkbox][name=enabled_folder]");
    boxes.forEach(function (cb) {
      if (!cb.checked) {
        var folder = cb.getAttribute("data-folder") || cb.dataset.folder || cb.value;
        if (folder) disabled.push(folder);
      }
    });
    return disabled;
  }

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

  function send(form) {
    var myGeneration = ++generation;
    var status = document.getElementById("disk-filter-status");
    var disabled = collectDisabled(form);
    var videoId = currentVideoId();
    if (status) status.textContent = "Application...";

    var headers = { "Content-Type": "application/json" };
    if (window.__withCsrfHeaders) headers = window.__withCsrfHeaders(headers);

    fetch("/api/disk-filter", {
      method: "POST",
      body: JSON.stringify({
        disabled_folders: disabled,
        video_id: videoId,
      }),
      cache: "no-store",
      headers: headers,
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
        if (status) {
          var n = (typeof res.videos_off === "number") ? res.videos_off : null;
          status.textContent = n !== null
            ? ("✅ Mis à jour — " + n + " vidéo(s) masquée(s).")
            : "✅ Mis à jour.";
          setTimeout(function () {
            if (myGeneration === generation && status) status.textContent = "";
          }, 3000);
        }
      })
      .catch(function (err) {
        if (myGeneration !== generation) return;
        if (status) status.textContent = "❌ Échec (" + (err.message || "réseau") + ").";
      });
  }

  // Délégation : fonctionne même si le formulaire a été injecté par soft-nav
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || t.type !== "checkbox" || t.name !== "enabled_folder") return;
    var form = t.closest("#disk-filter-form");
    if (!form) return;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () { send(form); }, 120);
  }, true);

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.id !== "disk-filter-form") return;
    e.preventDefault();
    clearTimeout(debounceTimer);
    send(form);
  }, true);
})();
