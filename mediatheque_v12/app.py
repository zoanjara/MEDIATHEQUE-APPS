# -*- coding: utf-8 -*-
"""
Médiathèque locale — application Flask 100% hors-ligne.
Point d'entrée : lancer avec `python app.py` puis ouvrir http://127.0.0.1:5000
"""
import math
import mimetypes
import os
import platform
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import webbrowser
import threading
import queue
import json
import io
import zipfile
from datetime import timedelta

from flask import (
    Flask, render_template, request, redirect, url_for, send_file,
    send_from_directory, jsonify, flash, session, Response, stream_with_context,
    after_this_request
)

import config
import db
import scanner
import live_updates

app = Flask(__name__)

# Clé secrète persistante (générée une fois, stockée dans data/) — évite
# une clé hardcodée forgeable si l'appli est exposée sur le LAN.
_SECRET_KEY_PATH = os.path.join(config.DATA_DIR, "secret_key")
def _load_or_create_secret_key():
    try:
        if os.path.exists(_SECRET_KEY_PATH):
            with open(_SECRET_KEY_PATH, "rb") as f:
                key = f.read().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = os.urandom(48)
    try:
        with open(_SECRET_KEY_PATH, "wb") as f:
            f.write(key)
        try:
            os.chmod(_SECRET_KEY_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return key
app.secret_key = _load_or_create_secret_key()

# Session permanente (cookie longue durée, ~10 ans) : sans ça, Flask utilise
# un cookie de session "navigateur" qui disparaît à la fermeture complète du
# navigateur. Les filtres/tri mémorisés côté serveur (resolve_listing_filters,
# voir plus bas) doivent survivre à une réouverture du projet, pas seulement
# à une navigation dans l'onglet en cours.
app.permanent_session_lifetime = timedelta(days=3650)

# ---------------------------------------------------------------------------
# Protection CSRF (token de session) sur toutes les mutations POST/PUT/PATCH/
# DELETE. Les GET (lecture, stream, vignettes) restent ouverts. Les clients
# JS envoient le token via l'en-tête X-CSRF-Token ou le champ de formulaire
# csrf_token (voir static/js/main.js + meta dans base.html).
# ---------------------------------------------------------------------------
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_CSRF_EXEMPT_ENDPOINTS = frozenset({
    # Heartbeat de lecture : appelé très fréquemment, pas d'effet destructif.
    "playback_heartbeat",
})


def _ensure_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        session["_csrf_token"] = token
    return token


def _csrf_ok():
    expected = session.get("_csrf_token")
    if not expected:
        return False
    got = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
        or ""
    )
    if not got:
        return False
    # Comparaison à temps constant
    if len(got) != len(expected):
        return False
    result = 0
    for a, b in zip(got.encode("utf-8"), expected.encode("utf-8")):
        result |= a ^ b
    return result == 0


@app.context_processor
def _inject_csrf():
    return {"csrf_token": _ensure_csrf_token()}


@app.before_request
def _make_session_permanent():
    session.permanent = True
    _ensure_csrf_token()
    _touch_app_activity()
    if request.method in _CSRF_SAFE_METHODS:
        return None
    endpoint = request.endpoint or ""
    if endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return None
    if not _csrf_ok():
        wants_json = (
            request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in (request.accept_mimetypes.best or "")
            or request.path.startswith("/api/")
            or request.path.startswith("/video/") and request.path.endswith("/delete_file")
            or "/open_external/" in request.path
            or "/open_folder/" in request.path
            or "/transcode/" in request.path
            or "/favorite/" in request.path
        )
        if wants_json:
            return jsonify(ok=False, error="Jeton CSRF invalide ou manquant. Recharge la page."), 403
        flash("Session expirée ou jeton de sécurité invalide. Réessaie.")
        return redirect(request.referrer or url_for("index"))

# ---------------------------------------------------------------------------
# ACTIVITÉ GÉNÉRALE DE L'APPLICATION (navigation, filtres, édition de
# métadonnées, etc.) — pas seulement la lecture vidéo.
# ---------------------------------------------------------------------------
# Le pré-transcodage en arrière-plan des vidéos à codec à risque (voir
# _precode_risky_videos plus bas) ne se mettait en pause QUE pendant une
# lecture vidéo active ou un scan en cours. Résultat : dès qu'on arrêtait de
# regarder une vidéo — même en continuant à naviguer activement dans
# l'appli (filtrer, éditer une fiche, faire défiler la grille...) — ce
# pré-transcodage pouvait démarrer (ou reprendre) et faire remonter le CPU
# quelques minutes plus tard, alors même que l'appli était toujours en
# cours d'utilisation. On mémorise donc l'instant de la dernière requête
# HTTP reçue (hors flux SSE de rafraîchissement en temps réel, qui reste
# ouvert en permanence et ne reflète pas une action réelle) : le
# pré-transcodage n'est autorisé à démarrer que si, EN PLUS de l'absence de
# lecture/scan, l'appli n'a reçu aucune requête depuis un petit moment (voir
# _APP_IDLE_SECONDS et _background_work_allowed ci-dessous, utilisés dans
# _precode_risky_videos et start_precode_risky_videos_async).
_last_activity_lock = threading.Lock()
_last_activity_ts = 0.0
# Chemins exclus du suivi d'activité : connexions techniques qui restent
# ouvertes en permanence ou reviennent toutes les quelques secondes sans
# corréler avec une action réelle de l'utilisateur, et qui rendraient sinon
# l'appli "toujours active" même quand elle n'est plus vraiment utilisée.
_ACTIVITY_EXCLUDED_PATHS = {"/api/live", "/scan/status", "/api/playback/heartbeat"}


def _touch_app_activity():
    if request.path in _ACTIVITY_EXCLUDED_PATHS:
        return
    global _last_activity_ts
    with _last_activity_lock:
        _last_activity_ts = time.time()
    # Signale aussi au scanner (voir scanner.notify_app_activity) qu'une
    # requête "réelle" vient d'arriver, pour qu'un scan en cours suspende
    # brièvement ses nouvelles extractions d'image et laisse cette requête
    # être traitée sans concurrence CPU — l'appli reste réactive même
    # pendant un scan intensif sur une grosse bibliothèque.
    scanner.notify_app_activity()


# Délai d'inactivité générale requis, en plus de l'absence de lecture/scan,
# avant d'autoriser le pré-transcodage en arrière-plan à démarrer ou
# reprendre. Assez court pour reprendre vite une fois l'appli vraiment
# laissée de côté, assez long pour ne pas se déclencher entre deux clics.
_APP_IDLE_SECONDS = 45.0


def _app_idle_enough():
    with _last_activity_lock:
        last = _last_activity_ts
    return (last == 0.0) or (time.time() - last >= _APP_IDLE_SECONDS)


# ---------------------------------------------------------------------------
# Registre des flux vidéo actuellement ouverts côté serveur (par id de
# vidéo). Utilisé exclusivement pour permettre une fermeture immédiate et
# fiable du descripteur de fichier avant une suppression physique (voir
# /video/<id>/delete_file) : sous Windows, un fichier ne peut pas être
# déplacé vers la corbeille tant qu'un processus le garde ouvert (ex. une
# requête de streaming Range mise en pause côté navigateur, dont la
# connexion HTTP reste maintenue). Plutôt que de deviner un délai
# d'attente, on ferme nous-mêmes, explicitement, tout descripteur encore
# actif pour cette vidéo au moment de la suppression.
# ---------------------------------------------------------------------------
_stream_handles_lock = threading.Lock()
_stream_handles = {}  # video_id -> set des objets fichier actuellement ouverts

# Limite le nombre de flux vidéo simultanés pour éviter que le PC ne
# ralentisse pendant la lecture (chaque flux consomme RAM et CPU pour
# la lecture disque + envoi réseau). 4 flux simultanés = 1 lecteur
# principal + marge pour les requêtes Range de prélecture du navigateur.
_MAX_CONCURRENT_STREAMS = 4
_stream_semaphore = threading.Semaphore(_MAX_CONCURRENT_STREAMS)


def _register_stream_handle(video_id, fh):
    with _stream_handles_lock:
        _stream_handles.setdefault(video_id, set()).add(fh)


def _unregister_stream_handle(video_id, fh):
    with _stream_handles_lock:
        handles = _stream_handles.get(video_id)
        if handles is not None:
            handles.discard(fh)
            if not handles:
                _stream_handles.pop(video_id, None)


def force_close_stream_handles(video_id):
    """Ferme immédiatement tout descripteur de fichier encore ouvert côté
    serveur pour cette vidéo. Rend la suppression fiable indépendamment du
    minutage du navigateur (plus besoin d'espérer que la connexion se
    referme d'elle-même)."""
    with _stream_handles_lock:
        handles = list(_stream_handles.pop(video_id, ()))
    for fh in handles:
        try:
            fh.close()
        except Exception:
            pass

with app.app_context():
    db.init_db()
    try:
        db.set_disabled_folders(config.load_settings().get("disabled_folders", []))
    except Exception:
        pass
    try:
        # Calcule en tâche de fond les empreintes de contenu manquantes pour
        # la bibliothèque déjà indexée avant l'ajout de la détection de
        # doublons par contenu (voir scanner.backfill_content_hashes) : ne
        # bloque jamais le démarrage, ne relance aucun traitement ffmpeg.
        scanner.start_backfill_content_hashes_async()
    except Exception:
        pass
    try:
        # Pré-remplit "Date de la vidéo" (si encore vide) pour la
        # bibliothèque déjà indexée avant l'ajout de cette fonctionnalité,
        # à partir du "Modifié le" déjà connu (mtime) : aucun accès disque,
        # aucun rescan, et ne touche jamais une date déjà saisie manuellement.
        scanner.start_backfill_video_dates_async()
    except Exception:
        pass
    try:
        # CORRECTIF "gel de quelques secondes sur codec lourd" : cet appel
        # avait disparu du démarrage (contrairement aux deux backfills
        # ci-dessus, câblés normalement). Sans lui, toute vidéo indexée
        # AVANT l'ajout de la détection "codec à risque" restait pour
        # toujours avec codec_risky=0 (valeur par défaut posée par la
        # migration de colonne, voir db.init_db) : elle ne déclenchait
        # jamais le message d'avertissement "Codec lourd détecté" ni le
        # mode compatible de templates/video.html, et partait donc en
        # lecture intégrée directe — précisément le gel de quelques
        # secondes que ce mécanisme existe pour éviter. Seule une analyse
        # ffprobe légère (pas de vignette, pas de conversion) tourne ici,
        # jamais de transcodage automatique : voir scanner.backfill_risky_codecs.
        scanner.start_backfill_risky_codecs_async()
    except Exception:
        pass


# Taille max d'une image de couverture / tag (anti-DoS disque).
_MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 Mo
_ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# Signatures magiques minimales (les premiers octets).
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),           # JPEG
    (b"\x89PNG\r\n\x1a\n", ".png"),    # PNG
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # WebP : RIFF....WEBP (vérif plus bas)
)


def _save_image_upload(file_storage, dest_dir):
    """Enregistre une image uploadée (si présente et valide) et retourne son
    nom de fichier, ou None si aucun fichier n'a été envoyé / invalide.
    Refuse : extension inconnue, > 8 Mo, contenu non-image (magic bytes)."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        return None
    # Lecture bornée pour ne pas saturer la RAM
    data = file_storage.read(_MAX_IMAGE_UPLOAD_BYTES + 1)
    if not data:
        return None
    if len(data) > _MAX_IMAGE_UPLOAD_BYTES:
        return None
    head = data[:16]
    ok_magic = False
    for magic, _ in _IMAGE_MAGIC:
        if head.startswith(magic):
            if magic == b"RIFF":
                # WebP : octets 8-11 = WEBP
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    ok_magic = True
            else:
                ok_magic = True
            break
    if not ok_magic:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(dest_dir, filename)
    os.makedirs(dest_dir, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return filename


# ---------- Helpers ----------

def format_duration(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_size(num_bytes):
    num_bytes = num_bytes or 0
    for unit in ["o", "Ko", "Mo", "Go", "To"]:
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} Po"


_MOIS_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def format_date_fr(value):
    """Formate une date ISO (YYYY-MM-DD, telle que stockée par le champ
    <input type="date">) en "12 mars 2024". Retourne la valeur telle quelle
    si elle ne correspond pas à ce format (jamais d'erreur)."""
    if not value:
        return ""
    try:
        y, m, d = value.split("-")
        return f"{int(d)} {_MOIS_FR[int(m)]} {y}"
    except (ValueError, IndexError):
        return value


app.jinja_env.filters["duration"] = format_duration
app.jinja_env.filters["filesize"] = format_size
app.jinja_env.filters["frdate"] = format_date_fr


def asset_version(static_relpath):
    """Retourne un identifiant basé sur la date de modification du fichier
    statique, à ajouter en paramètre d'URL (?v=...) pour forcer le
    navigateur à recharger le CSS/JS après une mise à jour, au lieu de
    servir une version en cache."""
    try:
        full_path = os.path.join(app.static_folder, static_relpath)
        return int(os.path.getmtime(full_path))
    except OSError:
        return 0


app.jinja_env.globals["asset_version"] = asset_version


@app.after_request
def _no_cache_for_static_assets(response):
    """Empêche le navigateur de garder en cache une ancienne version du
    CSS/JS entre deux mises à jour : il devra toujours revérifier auprès
    du serveur avant d'utiliser le fichier (le paramètre ?v=... garantit
    lui le rechargement effectif quand le contenu a réellement changé)."""
    if request.path.startswith("/static/css/") or request.path.startswith("/static/js/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif response.mimetype == "text/html":
        # Toute page HTML (navigation classique ou récupérée via fetch()
        # par la navigation douce) doit toujours refléter l'état actuel des
        # données : jamais de version mise en cache par le navigateur.
        response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def _notify_live_changes(response):
    """Toute modification effectuée dans l'application (édition,
    création, suppression, changement de paramètres...) — sans exception —
    est signalée immédiatement à tous les onglets ouverts via /api/live,
    afin qu'ils se mettent à jour d'eux-mêmes (voir static/js/live-sync.js),
    sans jamais nécessiter de recharger manuellement la page."""
    try:
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and response.status_code < 400
            and not request.path.startswith("/api/live")
        ):
            live_updates.bump(request.path)
    except Exception:
        pass  # la diffusion temps réel ne doit jamais faire échouer une requête
    return response


def get_sidebar_data():
    conn = db.get_db()
    categories = db.get_categories()
    playlists = db.get_playlists()
    popular_tags = conn.execute(
        """SELECT t.id, t.name, COUNT(vt.video_id) as n FROM tags t
           JOIN video_tags vt ON vt.tag_id = t.id
           GROUP BY t.id ORDER BY n DESC LIMIT 30"""
    ).fetchall()
    return categories, playlists, popular_tags


# Palette utilisée pour donner à chaque playlist une couleur bien distincte
# dans le panneau latéral (les catégories, elles, gardent la couleur rose
# unique déjà utilisée partout ailleurs dans l'appli).
PLAYLIST_COLORS = [
    "#e07a5f", "#3d5a80", "#8ac926", "#ff006e", "#ffb703",
    "#6a4c93", "#118ab2", "#ef476f", "#06a77d", "#f3722c",
]


def _playlists_with_colors(playlist_tree):
    """Colore l'arborescence des playlists : chaque playlist PRINCIPALE reçoit
    sa propre couleur bien distincte, et ses sous-playlists héritent de la
    même couleur (pour bien les regrouper visuellement dans le panneau).

    La couleur est calculée à partir de l'id de la playlist (immuable), et
    non de sa position dans la liste triée : la liste est réordonnée à
    chaque renommage/création/suppression, alors que l'id d'une playlist ne
    change jamais. Si on se basait sur la position, créer ou supprimer UNE
    SEULE playlist décalerait la couleur de TOUTES celles qui la suivent
    dans l'ordre alphabétique — chaque playlist garde ainsi sa couleur, et
    toute nouvelle playlist reçoit automatiquement la sienne dès sa
    création, sans jamais perturber les autres."""
    colored = []
    for row in playlist_tree:
        item = dict(row)
        item["color"] = PLAYLIST_COLORS[item["id"] % len(PLAYLIST_COLORS)]
        item["children"] = [dict(c, color=item["color"]) for c in row.get("children", [])]
        colored.append(item)
    return colored


# Cache mémoire du panneau latéral (catégories / playlists / tags).
# Sans cache, chaque page HTML relançait 3 requêtes GROUP BY (counts) +
# construction JSON des tags — coût fixe inutile quand rien n'a changé.
# Invalidation : dès que live_updates.bump() incrémente la version (toute
# modification métier, scan, etc.), le cache est reconstruit au prochain
# rendu. TTL de sécurité (60 s) au cas où un bump serait manqué.
_sidebar_nav_cache = {"version": None, "built_at": 0.0, "payload": None}
_sidebar_nav_lock = threading.Lock()
_SIDEBAR_NAV_TTL = 60.0


def _build_sidebar_nav_payload():
    # Tout est converti en dict Python purs : le cache est partagé entre
    # threads/requêtes ; les sqlite3.Row restent liés à la connexion qui les
    # a produits et provoquaient un Internal Server Error dès qu'une autre
    # requête réutilisait le cache.
    categories = [dict(c) for c in db.get_categories()]
    playlists_tree = db.get_playlists_tree()
    conn = db.get_db()
    nav_tags = [
        dict(t)
        for t in conn.execute(
            """SELECT t.id, t.name, t.image1, COUNT(vt.video_id) as n FROM tags t
               LEFT JOIN video_tags vt ON vt.tag_id = t.id
               GROUP BY t.id ORDER BY t.name COLLATE NOCASE"""
        )
    ]
    # Version JSON légère (id/name/image_url) de tous les tags existants,
    # embarquée dans base.html : permet au champ "Tags" du panneau "✎
    # Modifier les métadonnées" (voir _tag_field.html + static/js/main.js,
    # initTagField) de reconnaître instantanément côté client qu'un tag tapé
    # au clavier correspond à un tag déjà existant, et donc d'afficher tout
    # de suite sa photo actuelle sans aller-retour serveur.
    nav_tags_json = [
        {"id": t["id"], "name": t["name"],
         "image_url": url_for("tag_image", filename=t["image1"]) if t["image1"] else None,
         "count": t["n"] or 0}
        for t in nav_tags
    ]
    return dict(
        nav_categories=categories,
        nav_playlists=_playlists_with_colors(playlists_tree),
        nav_tags=nav_tags,
        nav_tags_json=nav_tags_json,
    )


@app.context_processor
def inject_sidebar_nav():
    """Rend disponibles, dans TOUS les templates (via base.html), la liste
    des catégories et l'arborescence des playlists (playlists
    principales + leurs sous-playlists) à afficher dans le panneau vertical,
    chaque playlist principale recevant sa propre couleur, ainsi que la
    liste complète des tags (affichée dans la fenêtre "🏷 Tags", pas dans le
    panneau lui-même — il peut y en avoir beaucoup trop pour y tenir).

    Résultat mis en cache mémoire et invalidé dès qu'une donnée change
    (voir live_updates.current_version / bump)."""
    global _sidebar_nav_cache
    now = time.time()
    try:
        version = live_updates.current_version()
    except Exception:
        version = None
    with _sidebar_nav_lock:
        cached = _sidebar_nav_cache
        if (
            cached["payload"] is not None
            and cached["version"] == version
            and (now - cached["built_at"]) < _SIDEBAR_NAV_TTL
        ):
            return cached["payload"]
    # Construction hors verrou (requêtes SQL) pour ne pas bloquer les autres
    # threads pendant le GROUP BY tags/catégories/playlists.
    payload = _build_sidebar_nav_payload()
    with _sidebar_nav_lock:
        _sidebar_nav_cache = {"version": version, "built_at": time.time(), "payload": payload}
    return payload


def get_flat_categories():
    conn = db.get_db()
    return conn.execute("SELECT id, name FROM categories ORDER BY name COLLATE NOCASE").fetchall()


def _flatten_playlist_tree(tree, depth=0):
    """Aplati un arbre de playlists (issu de db.get_playlists_tree()) en une
    liste, chaque playlist suivie immédiatement de ses sous-playlists, avec
    un champ 'depth' ajouté (0 = playlist principale). Permet d'afficher la
    hiérarchie dans une simple liste (indentation CSS) tout en gardant les
    cartes au même niveau du DOM — nécessaire pour que le filtre/tri de la
    page Organiser continue de fonctionner correctement sur l'ensemble."""
    flat = []
    for node in tree:
        node = dict(node)
        children = node.pop("children", [])
        node["depth"] = depth
        flat.append(node)
        flat.extend(_flatten_playlist_tree(children, depth + 1))
    return flat


def get_flat_playlists(exclude_ids=None):
    """Liste plate des playlists, ordonnée par arborescence (playlist
    principale suivie immédiatement de ses sous-playlists), avec un champ
    'depth' (0 = playlist principale, 1 = sous-playlist, ...) utilisé pour
    l'indentation dans les listes déroulantes et cases à cocher, et un champ
    'root_id' (id de la playlist principale dont elle dépend — sa propre id
    si depth == 0) utilisé pour ne proposer, dans le champ "Sous-playlist",
    que les sous-playlists appartenant à la playlist principale choisie
    dans le champ "Playlist" (filtre dynamique en cascade).
    exclude_ids : ids à exclure du résultat (ex. une playlist et ses propres
    descendantes, pour ne pas pouvoir se choisir elle-même comme parente)."""
    exclude_ids = exclude_ids or set()
    conn = db.get_db()
    rows = conn.execute("SELECT id, name, parent_id FROM playlists ORDER BY name COLLATE NOCASE").fetchall()
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)

    result = []

    def walk(parent_id, depth, parent_path, root_id):
        for r in by_parent.get(parent_id, []):
            if r["id"] in exclude_ids:
                continue
            # 'path' inclut le nom de la/des playlist(s) parente(s)
            # (ex. "Cuisine › Desserts") : utilisé quand la sous-playlist est
            # affichée seule (hors de son groupe indenté), pour qu'on sache
            # toujours de quelle playlist principale elle dépend.
            path = f"{parent_path} › {r['name']}" if parent_path else r["name"]
            this_root_id = r["id"] if depth == 0 else root_id
            result.append({
                "id": r["id"], "name": r["name"], "depth": depth, "path": path,
                "root_id": this_root_id,
            })
            walk(r["id"], depth + 1, path, this_root_id)

    walk(None, 0, "", None)
    return result


def resolve_playlist_root_id(playlist_top_id, playlist_sub_id, playlists_flat):
    """Détermine à quelle playlist principale restreindre le champ
    "Sous-playlist" du formulaire de filtres : celle explicitement choisie
    dans le champ "Playlist", ou à défaut celle dont dépend la sous-playlist
    déjà sélectionnée (pour continuer à proposer ses éventuelles sœurs).
    Retourne None si aucune playlist principale n'encadre le choix (aucun
    des deux champs n'est renseigné) : la liste des sous-playlists n'est
    alors restreinte que par les facettes générales (voir get_filter_facets)."""
    if playlist_top_id:
        return playlist_top_id
    if playlist_sub_id:
        entry = next((p for p in playlists_flat if p["id"] == playlist_sub_id), None)
        if entry:
            return entry["root_id"]
    return None




# ---------- Filtrage des vidéos (utilisé par l'accueil, une catégorie ou une playlist) ----------

def read_common_filters(fallback=None):
    """Lit dans la query string tous les filtres communs aux vues qui listent
    des vidéos (accueil, page catégorie, page playlist).

    Si `fallback` est fourni (dict), un champ absent de la query string
    utilise la valeur mémorisée dans `fallback` au lieu du défaut vide —
    c'est ce qui permet à la page playlist de réappliquer automatiquement
    les derniers filtres/tri utilisés."""
    fallback = fallback or {}

    def get_str(name):
        val = request.args.get(name)
        if val is None:
            val = fallback.get(name, "")
        return (val or "").strip()

    def get_int(name):
        val = request.args.get(name, type=int)
        if val is None and name not in request.args and fallback.get(name):
            try:
                val = int(fallback[name])
            except (TypeError, ValueError):
                val = None
        return val

    def get_float(name):
        val = request.args.get(name, type=float)
        if val is None and name not in request.args and fallback.get(name):
            try:
                val = float(fallback[name])
            except (TypeError, ValueError):
                val = None
        return val

    return {
        "q": get_str("q"),
        "tag_id": get_int("tag"),
        "sort": get_str("sort"),
        "duration_min": get_float("duration_min"),
        "duration_max": get_float("duration_max"),
        # Taille min/max en Mo (Mio, comme l'affichage |filesize) : mêmes
        # helpers get_float/mémorisation en session que duration_min/max
        # ci-dessus, exactement le même principe.
        "size_min": get_float("size_min"),
        "size_max": get_float("size_max"),
        "resolution": get_str("resolution"),
        "rating_min": get_int("rating_min"),
        "date_from": get_str("date_from"),
        "date_to": get_str("date_to"),
        "has_comment": get_str("has_comment"),
        "favorite_only": get_str("favorite_only"),
        # request.args.get("page", type=int) renvoie None (plutôt que de
        # lever une exception) si la valeur n'est pas un entier valide —
        # contrairement à l'ancien int(request.args.get("page", 1)) qui
        # plantait l'application (erreur 500) dès que le paramètre "page"
        # de l'URL n'était pas un nombre valide.
        "page": request.args.get("page", type=int) or 1,
    }


# Champs mémorisés pour la page playlist (le "page" et le "reset" n'en font
# pas partie : la pagination ne doit pas être mémorisée, et "reset" est un
# simple déclencheur).
PLAYLIST_FILTER_FIELDS = [
    "q", "category", "tag", "sort", "duration_min", "duration_max", "size_min", "size_max",
    "resolution", "rating_min", "date_from", "date_to", "has_comment", "favorite_only",
]
PLAYLIST_FILTERS_SESSION_KEY = "playlist_filters_v1"

# Champs mémorisés pour la page catégorie : mêmes filtres communs que pour
# la page playlist, mais avec "playlist"/"subplaylist" à la place de
# "category" (c'est l'inverse : sur la page catégorie, ce sont les champs
# playlist/sous-playlist qui sont proposés en plus des filtres communs -
# cf. show_category/show_playlist dans _filters.html).
CATEGORY_FILTER_FIELDS = [
    "q", "playlist", "subplaylist", "tag", "sort", "duration_min", "duration_max", "size_min", "size_max",
    "resolution", "rating_min", "date_from", "date_to", "has_comment", "favorite_only",
]
CATEGORY_FILTERS_SESSION_KEY = "category_filters_v1"

# Champs mémorisés pour l'accueil ("Toutes les vidéos") : les filtres
# communs, plus À LA FOIS "category" et "playlist"/"subplaylist" puisque
# les deux champs sont proposés sur cette page (show_category ET
# show_playlist valent true dans index.html, contrairement aux pages
# catégorie/playlist dédiées qui n'en proposent qu'un des deux).
INDEX_FILTER_FIELDS = [
    "q", "category", "playlist", "subplaylist", "tag", "sort", "duration_min",
    "duration_max", "size_min", "size_max", "resolution", "rating_min", "date_from", "date_to", "has_comment", "favorite_only",
]
INDEX_FILTERS_SESSION_KEY = "index_filters_v1"

# Champs mémorisés pour la page tag : mêmes filtres communs que pour
# l'accueil (catégorie ET playlist/sous-playlist sont proposées, cf.
# show_category/show_playlist dans tag.html), sans "tag" lui-même
# puisqu'il est fixé par l'URL (/tag/<id>), pas par le formulaire
# (show_tag = false dans tag.html).
TAG_FILTER_FIELDS = [
    "q", "category", "playlist", "subplaylist", "sort", "duration_min",
    "duration_max", "size_min", "size_max", "resolution", "rating_min", "date_from", "date_to", "has_comment", "favorite_only",
]
TAG_FILTERS_SESSION_KEY = "tag_filters_v1"


def resolve_listing_filters(session_key, fields):
    """Mémorise/restaure en session les filtres et le tri d'une vue de
    liste vidéos (playlist ou catégorie) d'une visite à l'autre, pour que
    l'utilisateur n'ait pas à tout refaire à chaque fois qu'il la consulte.

    - Lien "brut" (aucun paramètre de filtre dans l'URL, ex: clic depuis le
      panneau latéral) -> on réapplique les derniers filtres mémorisés.
    - Un filtre est présent dans l'URL -> on le mémorise pour la prochaine
      visite.
    - `?reset=1` -> on oublie les filtres mémorisés et on repart à zéro.
    """
    if request.args.get("reset"):
        session.pop(session_key, None)
        return {}

    has_explicit = any(name in request.args for name in fields)
    if has_explicit:
        current = {
            name: request.args.get(name, "")
            for name in fields
            if request.args.get(name, "")
        }
        session[session_key] = current
        return current

    return session.get(session_key, {})


def resolve_playlist_filters():
    """Filtres mémorisés de la page playlist (une mémoire commune à toutes
    les playlists/sous-playlists, qui partagent la même route)."""
    return resolve_listing_filters(PLAYLIST_FILTERS_SESSION_KEY, PLAYLIST_FILTER_FIELDS)


def resolve_category_filters():
    """Filtres mémorisés de la page catégorie (une mémoire commune à
    toutes les catégories, qui partagent la même route)."""
    return resolve_listing_filters(CATEGORY_FILTERS_SESSION_KEY, CATEGORY_FILTER_FIELDS)


def resolve_index_filters():
    """Filtres mémorisés de l'accueil ("Toutes les vidéos")."""
    return resolve_listing_filters(INDEX_FILTERS_SESSION_KEY, INDEX_FILTER_FIELDS)


def resolve_tag_filters():
    """Filtres mémorisés de la page tag (une mémoire commune à tous les
    tags, qui partagent la même route) - séparée de celle de l'accueil,
    pour que les filtres posés sur une page tag ne se mélangent pas avec
    ceux de l'accueil (et inversement)."""
    return resolve_listing_filters(TAG_FILTERS_SESSION_KEY, TAG_FILTER_FIELDS)


def build_filter_where(filters, exclude=None, join_playlist_for_sort=False):
    """Construit la clause WHERE (+ JOINs + paramètres) correspondant aux
    filtres actifs. `filters` est un dict avec les clés q, category_id,
    tag_id, playlist_id, duration_min, duration_max, resolution,
    rating_min, date_from, date_to, has_comment, favorite_only.

    `exclude` est un ensemble de noms de champs à ignorer lors de la
    construction (le filtre correspondant n'est alors PAS appliqué). C'est
    ce qui permet à get_filter_facets() de calculer, pour un champ donné,
    les valeurs encore disponibles compte tenu de tous les AUTRES filtres
    actifs, sans que le champ lui-même ne se filtre lui-même.

    Tag / playlist : filtre via EXISTS (pas de JOIN) → une ligne par vidéo,
    pas de DISTINCT ni GROUP BY, pagination OFFSET bien plus légère.
    Exception : join_playlist_for_sort=True (tri par position de playlist)
    nécessite le JOIN playlist_items pour ORDER BY pi.position."""
    exclude = exclude or set()
    # disk_off (cache SQL, éventuellement en retard) + exclusion mémoire
    # immédiate des racines désactivées (Disques actifs en temps réel)
    where = ["v.missing = 0", "v.disk_off = 0"]
    params = []
    joins = ""
    roots_sql, roots_params = db.get_disabled_roots_clause("v")
    if roots_sql:
        where.append(roots_sql)
        params.extend(roots_params)

    if filters.get("q") and "q" not in exclude:
        # Recherche plein texte (FTS5, voir videos_fts/build_fts_match_query
        # dans db.py) sur titre/description/commentaire, en remplacement du
        # LIKE '%...%' précédent : plus rapide sur une grosse bibliothèque,
        # et insensible aux accents/à la casse via le tokenizer unicode61.
        q = filters["q"]
        if db.FTS5_AVAILABLE:
            match_query = db.build_fts_match_query(q)
            if match_query:
                where.append("v.id IN (SELECT rowid FROM videos_fts WHERE videos_fts MATCH ?)")
                params.append(match_query)
            else:
                # Recherche sans aucun caractère exploitable (ex:
                # uniquement de la ponctuation) : aucun résultat plutôt
                # que d'ignorer le filtre silencieusement.
                where.append("0")
        else:
            # Repli : ce build de SQLite/Python n'a pas FTS5 (voir
            # db.FTS5_AVAILABLE / init_db). Comportement inchangé par
            # rapport à avant cette migration.
            where.append("(v.title LIKE ? OR v.description LIKE ? OR v.comment LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if filters.get("category_id") and "category_id" not in exclude:
        where.append("v.category_id = ?")
        params.append(filters["category_id"])
    if filters.get("tag_id") and "tag_id" not in exclude:
        # EXISTS : index idx_video_tags_tag, sans multiplier les lignes.
        where.append(
            "EXISTS (SELECT 1 FROM video_tags vt "
            "WHERE vt.video_id = v.id AND vt.tag_id = ?)"
        )
        params.append(filters["tag_id"])
    if filters.get("playlist_id") and "playlist_id" not in exclude:
        if join_playlist_for_sort:
            joins += " JOIN playlist_items pi ON pi.video_id = v.id"
            where.append("pi.playlist_id = ?")
            params.append(filters["playlist_id"])
        else:
            where.append(
                "EXISTS (SELECT 1 FROM playlist_items pi "
                "WHERE pi.video_id = v.id AND pi.playlist_id = ?)"
            )
            params.append(filters["playlist_id"])
    if filters.get("duration_min") and "duration_min" not in exclude:
        where.append("v.duration_seconds >= ?")
        params.append(filters["duration_min"] * 60)
    if filters.get("duration_max") and "duration_max" not in exclude:
        where.append("v.duration_seconds <= ?")
        params.append(filters["duration_max"] * 60)
    if filters.get("size_min") and "size_min" not in exclude:
        # size_min/size_max sont exprimés en Mo (Mio) côté formulaire, comme
        # l'affichage |filesize ; v.size_bytes est en octets.
        where.append("v.size_bytes >= ?")
        params.append(filters["size_min"] * 1024 * 1024)
    if filters.get("size_max") and "size_max" not in exclude:
        where.append("v.size_bytes <= ?")
        params.append(filters["size_max"] * 1024 * 1024)
    if "resolution" not in exclude:
        resolution = filters.get("resolution")
        if resolution == "sd":
            where.append("v.height < 720")
        elif resolution == "hd":
            where.append("v.height >= 720 AND v.height < 1080")
        elif resolution == "fhd":
            where.append("v.height >= 1080")
    if filters.get("rating_min") and "rating_min" not in exclude:
        where.append("v.rating >= ?")
        params.append(filters["rating_min"])
    if filters.get("date_from") and "date_from" not in exclude:
        where.append("v.video_date >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to") and "date_to" not in exclude:
        where.append("v.video_date <= ?")
        params.append(filters["date_to"])
    if filters.get("has_comment") == "1" and "has_comment" not in exclude:
        where.append("v.comment IS NOT NULL AND TRIM(v.comment) != ''")
    if filters.get("favorite_only") == "1" and "favorite_only" not in exclude:
        where.append("v.favorite = 1")

    return where, params, joins


def get_filter_facets(filters):
    """Filtre dynamique inter-champs : calcule pour chaque case à choix
    (catégorie, tag, playlist/sous-playlist, résolution, note minimum)
    l'ensemble des valeurs qui donnent encore au moins un résultat compte
    tenu de TOUS LES AUTRES filtres actuellement actifs (son propre filtre
    est ignoré dans son propre calcul, sinon un champ se filtrerait
    lui-même et on ne pourrait plus revenir en arrière).

    Concrètement : dès qu'un filtre est posé sur une case (ex. une
    catégorie), les autres cases (tags, playlists, résolution, note) ne
    proposent plus ensuite que les choix compatibles avec ce qui a déjà
    été filtré — et ainsi de suite à chaque nouveau filtre ajouté."""
    conn = db.get_db()

    def values_for(exclude_field, select_expr, extra_join=""):
        where, params, joins = build_filter_where(filters, exclude={exclude_field})
        where_sql = " AND ".join(where)
        sql = f"SELECT DISTINCT {select_expr} AS val FROM videos v {joins}{extra_join} WHERE {where_sql}"
        return conn.execute(sql, params).fetchall()

    category_ids = {r["val"] for r in values_for("category_id", "v.category_id") if r["val"] is not None}
    tag_ids = {
        r["val"] for r in values_for(
            "tag_id", "vtf.tag_id", " JOIN video_tags vtf ON vtf.video_id = v.id"
        )
    }
    playlist_ids = {
        r["val"] for r in values_for(
            "playlist_id", "pif.playlist_id", " JOIN playlist_items pif ON pif.video_id = v.id"
        )
    }
    resolutions = {
        r["val"] for r in values_for(
            "resolution",
            "CASE WHEN v.height IS NULL THEN NULL WHEN v.height < 720 THEN 'sd' "
            "WHEN v.height < 1080 THEN 'hd' ELSE 'fhd' END",
        ) if r["val"]
    }
    rating_rows = values_for("rating_min", "v.rating")
    max_rating = max((r["val"] or 0) for r in rating_rows) if rating_rows else 0

    return {
        "category_ids": category_ids,
        "tag_ids": tag_ids,
        "playlist_ids": playlist_ids,
        "resolutions": resolutions,
        "max_rating": max_rating,
    }


# Tri supportant la pagination par curseur (keyset). Les tris "video_date*"
# utilisent une expression CASE trop complexe pour un curseur simple → OFFSET.
_CURSOR_SORTS = {
    # sort: (colonne SQL pour la clé, direction DESC?, expression ORDER BY sans id)
    "recent": ("v.added_at", True, "v.added_at DESC"),
    "title": ("v.title COLLATE NOCASE", False, "v.title COLLATE NOCASE ASC"),
    "duration": ("v.duration_seconds", True, "v.duration_seconds DESC"),
    "views": ("v.views", True, "v.views DESC"),
    "rating": ("v.rating", True, "v.rating DESC"),
    "size": ("v.size_bytes", True, "v.size_bytes DESC"),
    "position": ("pi.position", False, "pi.position ASC"),
}


def _cursor_key_from_row(sort, row):
    """Valeur de clé de tri extraite d'une ligne pour les liens seek next/prev."""
    if sort == "recent":
        return row.get("added_at")
    if sort == "title":
        return row.get("title")
    if sort == "duration":
        return row.get("duration_seconds")
    if sort == "views":
        return row.get("views")
    if sort == "rating":
        return row.get("rating")
    if sort == "size":
        return row.get("size_bytes")
    if sort == "position":
        return row.get("_seek_key")
    return None


def query_videos(q="", category_id=None, tag_id=None, playlist_id=None, sort="recent",
                  duration_min=None, duration_max=None, size_min=None, size_max=None,
                  resolution="", rating_min=None,
                  date_from="", date_to="", has_comment="", favorite_only="", page=1, page_size=40,
                  seek_id=None, seek_key=None, seek_dir=None):
    """Construit et exécute la requête filtrée/triée/paginée des vidéos.

    Pagination :
      - Par **curseur** (seek_id + seek_key + seek_dir=next|prev) quand le tri
        le permet : WHERE sur (clé, id) au lieu de OFFSET → temps stable
        même en page profonde.
      - Par **OFFSET** sinon (clic sur un numéro de page, ou tri video_date).

    Étapes communes :
      1. COUNT(*) sur le filtre
      2. SELECT id (+ clé) … LIMIT page_size
      3. SELECT * WHERE id IN (…)
    """
    conn = db.get_db()
    filters = dict(
        q=q, category_id=category_id, tag_id=tag_id, playlist_id=playlist_id,
        duration_min=duration_min, duration_max=duration_max,
        size_min=size_min, size_max=size_max, resolution=resolution,
        rating_min=rating_min, date_from=date_from, date_to=date_to, has_comment=has_comment,
        favorite_only=favorite_only,
    )
    sort = sort or "recent"
    sort_by_position = bool(playlist_id and sort == "position")
    where, params, joins = build_filter_where(
        filters, join_playlist_for_sort=sort_by_position
    )

    order_map = {
        "recent": "v.added_at DESC",
        "title": "v.title COLLATE NOCASE ASC",
        "duration": "v.duration_seconds DESC",
        "views": "v.views DESC",
        "rating": "v.rating DESC",
        "video_date": "CASE WHEN v.video_date IS NULL OR v.video_date = '' THEN 1 ELSE 0 END, v.video_date DESC",
        "video_date_asc": "CASE WHEN v.video_date IS NULL OR v.video_date = '' THEN 1 ELSE 0 END, v.video_date ASC",
        "size": "v.size_bytes DESC",
    }
    if playlist_id:
        order_map["position"] = "pi.position ASC"
    order_by = order_map.get(sort, order_map["recent"])
    if "v.id" not in order_by:
        order_by = order_by + ", v.id ASC"
    where_sql = " AND ".join(where)
    from_sql = f"videos v {joins}" if joins else "videos v"

    total = conn.execute(
        f"SELECT COUNT(*) as n FROM {from_sql} WHERE {where_sql}", params
    ).fetchone()["n"]

    page_size = max(1, int(page_size or 40))
    total_pages = max(1, math.ceil(total / page_size) if total else 1)
    page = max(1, min(int(page or 1), total_pages))

    use_cursor = (
        sort in _CURSOR_SORTS
        and seek_id is not None
        and seek_dir in ("next", "prev")
        and seek_key is not None
    )

    select_extra = ", pi.position AS _seek_key" if sort_by_position else ""
    id_params = list(params)

    if use_cursor:
        col_sql, is_desc, _ = _CURSOR_SORTS[sort]
        # Comparaison keyset selon le sens du tri et la direction de navigation.
        # ORDER BY col DESC, id ASC  → "next" = plus bas dans ce ordre :
        #   (col < key) OR (col = key AND id > seek_id)
        # ORDER BY col ASC, id ASC   → "next" :
        #   (col > key) OR (col = key AND id > seek_id)
        # "prev" inverse les inégalités, puis on inverse la liste résultat.
        going_next = seek_dir == "next"
        if is_desc:
            if going_next:
                keyset = f"(({col_sql}) < ? OR (({col_sql}) = ? AND v.id > ?))"
            else:
                keyset = f"(({col_sql}) > ? OR (({col_sql}) = ? AND v.id < ?))"
        else:
            if going_next:
                keyset = f"(({col_sql}) > ? OR (({col_sql}) = ? AND v.id > ?))"
            else:
                keyset = f"(({col_sql}) < ? OR (({col_sql}) = ? AND v.id < ?))"
        id_params.extend([seek_key, seek_key, int(seek_id)])
        # Pour "prev", on parcourt dans l'ordre inverse puis on reverse.
        if going_next:
            order_clause = order_by
        else:
            # Inverse simple des ASC/DESC sur la clé principale + id
            if is_desc:
                order_clause = f"{col_sql} ASC, v.id DESC"
            else:
                order_clause = f"{col_sql} DESC, v.id DESC"
        id_sql = (
            f"SELECT v.id{select_extra} FROM {from_sql} "
            f"WHERE {where_sql} AND {keyset} "
            f"ORDER BY {order_clause} LIMIT ?"
        )
        id_params.append(page_size)
    else:
        offset = (page - 1) * page_size
        id_sql = (
            f"SELECT v.id{select_extra} FROM {from_sql} "
            f"WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?"
        )
        id_params.extend([page_size, offset])

    id_rows = conn.execute(id_sql, id_params).fetchall()
    if use_cursor and seek_dir == "prev":
        id_rows = list(reversed(id_rows))

    page_ids = [r["id"] for r in id_rows]
    seek_keys_by_id = {}
    if sort_by_position:
        for r in id_rows:
            seek_keys_by_id[r["id"]] = r["_seek_key"]

    if not page_ids:
        return [], total, total_pages, page, None, None

    placeholders = ",".join("?" for _ in page_ids)
    rows_by_id = {
        r["id"]: dict(r)
        for r in conn.execute(
            f"SELECT * FROM videos WHERE id IN ({placeholders})", page_ids
        )
    }
    videos = []
    for i in page_ids:
        if i not in rows_by_id:
            continue
        row = rows_by_id[i]
        if i in seek_keys_by_id:
            row["_seek_key"] = seek_keys_by_id[i]
        videos.append(row)

    videos = attach_video_collections(videos)

    # Curseurs pour les liens ◀ / ▶ (première et dernière vidéo de la page).
    seek_prev = seek_next = None
    if videos and sort in _CURSOR_SORTS:
        first, last = videos[0], videos[-1]
        k0, k1 = _cursor_key_from_row(sort, first), _cursor_key_from_row(sort, last)
        if k0 is not None:
            seek_prev = {"id": first["id"], "key": k0, "dir": "prev"}
        if k1 is not None:
            seek_next = {"id": last["id"], "key": k1, "dir": "next"}

    return videos, total, total_pages, page, seek_prev, seek_next


def _seek_from_request(sort=None):
    """Lit les paramètres de curseur dans l'URL (?seek_id=&seek_key=&seek_dir=).
    Convertit seek_key en nombre pour les tris numériques (size, views…)."""
    seek_id = request.args.get("seek_id", type=int)
    seek_key = request.args.get("seek_key")
    seek_dir = request.args.get("seek_dir")
    if seek_id is None or seek_key is None or seek_dir not in ("next", "prev"):
        return None, None, None
    # Tris numériques : comparer en int/float, pas en texte ("9" > "10" en string).
    if sort in ("duration", "views", "rating", "size", "position"):
        try:
            if "." in str(seek_key):
                seek_key = float(seek_key)
            else:
                seek_key = int(seek_key)
        except (TypeError, ValueError):
            pass
    return seek_id, seek_key, seek_dir


def attach_video_collections(videos):
    """Ajoute à chaque vidéo (sous forme de dict, en plus de toutes ses
    colonnes d'origine) une clé 'collections' : liste des cases
    catégorie/playlist(s)/sous-playlist(s) auxquelles elle est rattachée,
    chacune avec son nom (chemin complet pour une sous-playlist, ex.
    "Cuisine › Desserts") et l'URL de sa page dédiée. Utilisé par la grille
    de vidéos (_video_grid.html) pour afficher ce rattachement sous chaque
    case, indépendamment des filtres actifs sur la page."""
    video_dicts = [dict(v) for v in videos]
    if not video_dicts:
        return video_dicts

    video_ids = [v["id"] for v in video_dicts]
    conn = db.get_db()

    category_ids = {v["category_id"] for v in video_dicts if v.get("category_id")}
    category_names = {}
    if category_ids:
        placeholders = ",".join("?" for _ in category_ids)
        for r in conn.execute(
            f"SELECT id, name FROM categories WHERE id IN ({placeholders})",
            list(category_ids),
        ):
            category_names[r["id"]] = r["name"]

    playlist_paths = {p["id"]: p["path"] for p in get_flat_playlists()}
    placeholders = ",".join("?" for _ in video_ids)
    playlist_rows = conn.execute(
        f"""SELECT pi.video_id AS video_id, pi.playlist_id AS playlist_id
            FROM playlist_items pi WHERE pi.video_id IN ({placeholders})""",
        video_ids,
    ).fetchall()
    playlists_by_video = {}
    for r in playlist_rows:
        playlists_by_video.setdefault(r["video_id"], []).append(r["playlist_id"])

    for v in video_dicts:
        collections = []
        cat_name = category_names.get(v.get("category_id"))
        if cat_name:
            collections.append({
                "type": "category", "name": cat_name,
                "url": url_for("category_view", category_id=v["category_id"]),
            })
        for pid in playlists_by_video.get(v["id"], []):
            path = playlist_paths.get(pid)
            if path:
                collections.append({
                    "type": "playlist", "name": path,
                    "url": url_for("playlist_view", playlist_id=pid),
                })
        v["collections"] = collections

    return video_dicts


def get_search_group_matches(q, limit=12):
    """Pour une recherche texte libre "q" (utilisée par TOUS les champs de
    recherche du projet : barre du bandeau, panneau "Filtrer par..." sur
    l'accueil, une catégorie ou une playlist), renvoie les catégories,
    playlists et sous-playlists dont le NOM correspond, regroupées par
    type. Les vidéos correspondantes restent affichées séparément par
    query_videos()/build_filter_where() (grille filtrée existante) : cette
    fonction ne s'occupe que des 3 AUTRES types de données, pour que la
    page de résultats puisse les afficher groupés à côté des vidéos, comme
    le fait déjà le menu déroulant de suggestions pendant la frappe.
    Ne renvoie rien si q est vide."""
    if not q:
        return {"categories": [], "playlists": [], "subplaylists": []}

    conn = db.get_db()
    like = f"%{q}%"

    category_rows = conn.execute(
        "SELECT id, name, image1 FROM categories WHERE name LIKE ? ORDER BY name COLLATE NOCASE ASC LIMIT ?",
        (like, limit),
    ).fetchall()
    categories = [
        {
            "id": r["id"], "name": r["name"],
            "url": url_for("category_view", category_id=r["id"]),
            "image_url": url_for("category_image", filename=r["image1"]) if r["image1"] else None,
        }
        for r in category_rows
    ]

    # Playlists ET sous-playlists : même logique de correspondance (nom
    # complet, insensible à la casse) et de distinction par profondeur que
    # /api/search_suggestions, pour un résultat cohérent partout.
    playlist_images = {p["id"]: p["image1"] for p in conn.execute("SELECT id, image1 FROM playlists")}
    q_lower = q.lower()
    matching_playlists = [p for p in get_flat_playlists() if q_lower in p["name"].lower()]
    playlists, subplaylists = [], []
    for p in matching_playlists:
        entry = {
            "id": p["id"], "name": p["path"],
            "url": url_for("playlist_view", playlist_id=p["id"]),
            "image_url": url_for("playlist_image", filename=playlist_images[p["id"]])
                         if playlist_images.get(p["id"]) else None,
        }
        (playlists if p["depth"] == 0 else subplaylists).append(entry)

    return {
        "categories": categories,
        "playlists": playlists[:limit],
        "subplaylists": subplaylists[:limit],
    }


# ---------- Routes principales ----------

@app.route("/")
def index():
    conn = db.get_db()
    fallback = resolve_index_filters()
    f = read_common_filters(fallback=fallback)
    category_id = request.args.get("category", type=int)
    if category_id is None and "category" not in request.args and fallback.get("category"):
        try:
            category_id = int(fallback["category"])
        except (TypeError, ValueError):
            category_id = None
    # Le formulaire de filtres expose 2 champs séparés ("playlist" pour les
    # playlists principales, "subplaylist" pour les sous-playlists) afin de
    # faciliter la recherche, mais un seul filtre playlist_id est appliqué :
    # la sous-playlist est prioritaire si les deux sont renseignés.
    playlist_top_id = request.args.get("playlist", type=int)
    if playlist_top_id is None and "playlist" not in request.args and fallback.get("playlist"):
        try:
            playlist_top_id = int(fallback["playlist"])
        except (TypeError, ValueError):
            playlist_top_id = None
    playlist_sub_id = request.args.get("subplaylist", type=int)
    if playlist_sub_id is None and "subplaylist" not in request.args and fallback.get("subplaylist"):
        try:
            playlist_sub_id = int(fallback["subplaylist"])
        except (TypeError, ValueError):
            playlist_sub_id = None
    playlist_id = playlist_sub_id or playlist_top_id
    sort = f["sort"] or "recent"
    q, tag_id = f["q"], f["tag_id"]
    duration_min, duration_max = f["duration_min"], f["duration_max"]
    size_min, size_max = f["size_min"], f["size_max"]
    resolution, rating_min = f["resolution"], f["rating_min"]
    date_from, date_to, has_comment = f["date_from"], f["date_to"], f["has_comment"]
    favorite_only = f["favorite_only"]

    _sid, _skey, _sdir = _seek_from_request(sort)
    videos, total, total_pages, page, seek_prev, seek_next = query_videos(
        q=q, category_id=category_id, tag_id=tag_id, playlist_id=playlist_id, sort=sort,
        duration_min=duration_min, duration_max=duration_max,
        size_min=size_min, size_max=size_max, resolution=resolution,
        rating_min=rating_min, date_from=date_from, date_to=date_to,
        has_comment=has_comment, favorite_only=favorite_only, page=f["page"],
        page_size=20,
        seek_id=_sid, seek_key=_skey, seek_dir=_sdir,
    )

    categories, playlists, popular_tags = get_sidebar_data()
    categories_flat = get_flat_categories()
    playlists_flat = get_flat_playlists()
    all_tags = conn.execute("SELECT id, name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    facets = get_filter_facets(dict(
        q=q, category_id=category_id, tag_id=tag_id, playlist_id=playlist_id,
        duration_min=duration_min, duration_max=duration_max,
        size_min=size_min, size_max=size_max, resolution=resolution,
        rating_min=rating_min, date_from=date_from, date_to=date_to, has_comment=has_comment,
        favorite_only=favorite_only,
    ))
    playlist_root_id = resolve_playlist_root_id(playlist_top_id, playlist_sub_id, playlists_flat)
    search_matches = get_search_group_matches(q)

    # La vidéo à la une et les recommandations n'ont de sens que sur la
    # page d'accueil « par défaut » (pas de filtre actif, première page).
    any_filter = (q or category_id or tag_id or playlist_id or duration_min or duration_max
                  or size_min or size_max
                  or resolution or rating_min or date_from or date_to or has_comment or favorite_only)
    is_default_view = not any_filter and page == 1
    featured = db.get_featured_video() if is_default_view else None

    return render_template(
        "index.html", videos=videos, categories=categories, playlists=playlists,
        categories_flat=categories_flat, playlists_flat=playlists_flat,
        popular_tags=popular_tags, all_tags=all_tags, page=page, total_pages=total_pages,
        total=total, q=q, category_id=category_id, tag_id=tag_id, playlist_id=playlist_id, sort=sort,
        duration_min=duration_min, duration_max=duration_max,
        size_min=size_min, size_max=size_max, resolution=resolution,
        rating_min=rating_min, date_from=date_from, date_to=date_to, has_comment=has_comment,
        favorite_only=favorite_only,
        featured=featured, facets=facets,
        playlist_root_id=playlist_root_id, search_matches=search_matches,
        seek_prev=seek_prev, seek_next=seek_next,
    )


@app.route("/api/search_suggestions")
def search_suggestions():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify([])
    # Plafond anti-abus (requêtes absurdes / DoS)
    if len(q) > 120:
        q = q[:120]
    conn = db.get_db()
    like = f"%{q}%"

    # Recherche plein texte (FTS5) sur titre/description/commentaire, même
    # principe que build_filter_where() pour rester cohérent avec les
    # résultats affichés dans les grilles de vidéos. Repli LIKE si FTS5
    # est indisponible sur ce build de SQLite (voir db.FTS5_AVAILABLE).
    # Limite stricte : l'autocomplétion n'a besoin que de quelques
    # suggestions. Sans LIMIT, une requête large (ex. 1-2 lettres courantes)
    # sur une bibliothèque de plusieurs milliers de vidéos renvoyait tout
    # le matching en JSON et saturait le navigateur.
    SUGGEST_LIMIT = 12

    if db.FTS5_AVAILABLE:
        match_query = db.build_fts_match_query(q)
        if match_query:
            video_rows = conn.execute(
                """SELECT id, title FROM videos WHERE missing = 0 AND disk_off = 0
                   AND id IN (SELECT rowid FROM videos_fts WHERE videos_fts MATCH ?)
                   ORDER BY views DESC, title ASC LIMIT ?""",
                (match_query, SUGGEST_LIMIT),
            ).fetchall()
        else:
            video_rows = []
    else:
        video_rows = conn.execute(
            """SELECT id, title FROM videos WHERE missing = 0 AND disk_off = 0
               AND (title LIKE ? OR description LIKE ? OR comment LIKE ?)
               ORDER BY views DESC, title ASC LIMIT ?""",
            (like, like, like, SUGGEST_LIMIT),
        ).fetchall()
    results = [
        {"type": "video", "id": r["id"], "title": r["title"],
         "url": url_for("video_detail", video_id=r["id"]), "image_url": None}
        for r in video_rows
    ]

    category_rows = conn.execute(
        "SELECT id, name, image1 FROM categories WHERE name LIKE ? ORDER BY name COLLATE NOCASE ASC LIMIT ?",
        (like, SUGGEST_LIMIT),
    ).fetchall()
    results.extend(
        {
            "type": "category", "id": r["id"], "title": r["name"],
            "url": url_for("category_view", category_id=r["id"]),
            "image_url": url_for("category_image", filename=r["image1"]) if r["image1"] else None,
        }
        for r in category_rows
    )

    # Playlists ET sous-playlists : le "path" (fil d'ariane) sert de titre
    # affiché pour qu'une sous-playlist reste identifiable hors de son
    # groupe (ex. "Cuisine › Desserts"). depth == 0 -> playlist principale,
    # depth > 0 -> sous-playlist : distinguées ici pour être regroupées
    # séparément côté client, chacune avec sa propre photo de couverture.
    playlist_images = {p["id"]: p["image1"] for p in conn.execute("SELECT id, image1 FROM playlists")}
    matching_playlists = [p for p in get_flat_playlists() if q.lower() in p["name"].lower()]
    results.extend(
        {
            "type": "playlist" if p["depth"] == 0 else "subplaylist",
            "id": p["id"], "title": p["path"],
            "url": url_for("playlist_view", playlist_id=p["id"]),
            "image_url": url_for("playlist_image", filename=playlist_images[p["id"]])
                         if playlist_images.get(p["id"]) else None,
        }
        for p in matching_playlists
    )

    return jsonify(results)


@app.route("/api/video/<int:video_id>/meta")
def video_meta_json(video_id):
    """Métadonnées d'une vidéo au format JSON, pour la popup de modification
    rapide depuis la grille (pas besoin d'ouvrir la page de la vidéo)."""
    conn = db.get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    # CORRECTIF "Disques actifs" : par cohérence avec video_detail()/stream()
    # ci-dessous, une vidéo masquée ne doit plus livrer ses métadonnées non
    # plus (titre, description, tags...) — même si cette popup n'expose pas
    # le fichier vidéo lui-même, "Disques actifs" est censé masquer la vidéo
    # PARTOUT (voir le texte d'aide de la section Réglages correspondante).
    if (
        not video
        or video["disk_off"]
        or video["missing"]
        or db.path_is_disabled(video["path"], video["folder_root"])
    ):
        return jsonify(error="Vidéo introuvable"), 404
    tags = db.get_video_tags(video_id)
    category_name = ""
    if video["category_id"]:
        crow = conn.execute("SELECT name FROM categories WHERE id=?", (video["category_id"],)).fetchone()
        category_name = crow["name"] if crow else ""
    playlist_ids = db.get_video_playlist_ids(video_id)
    resp = jsonify({
        "id": video["id"],
        "title": video["title"],
        "description": video["description"] or "",
        "category": category_name,
        # Nécessaire côté client (static/js/main.js) pour que le champ
        # "Catégorie" de la popup rapide sache s'il affiche actuellement le
        # nom d'UNE catégorie précise, et donc se mette à jour tout seul si
        # cette catégorie est renommée pendant que la popup reste ouverte —
        # exactement comme le fait déjà data-initial-category sur la page
        # vidéo dédiée (voir templates/video.html).
        "category_id": video["category_id"],
        "tags": ", ".join(t["name"] for t in tags),
        # Détail complet (id + photo) de chaque tag déjà associé à cette
        # vidéo : permet au champ "Tags" du panneau de modification rapide
        # (voir _tag_field.html) de reconstruire les puces avec leur photo
        # actuelle, exactement comme le fait la page vidéo dédiée au premier
        # chargement (voir templates/video.html).
        "tags_full": [
            {"id": t["id"], "name": t["name"],
             "image_url": url_for("tag_image", filename=t["image1"]) if t["image1"] else None}
            for t in tags
        ],
        "rating": video["rating"] or 0,
        "video_date": video["video_date"] or "",
        "comment": video["comment"] or "",
        "views": video["views"] or 0,
        "playlist_ids": playlist_ids,
    })
    # Ce endpoint est interrogé à chaque ouverture de la popup de modification
    # rapide : on interdit toute mise en cache pour être certain de toujours
    # afficher les dernières métadonnées enregistrées (et non une version
    # obsolète servie depuis le cache du navigateur).
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/playlist/quick_create", methods=["POST"])
def api_playlist_quick_create():
    """Création rapide d'une playlist (ou sous-playlist, si parent_id est
    fourni) sans quitter la fenêtre "✎ Modifier les métadonnées" : renvoie
    la playlist créée en JSON pour l'ajouter immédiatement à la liste de
    cases à cocher, déjà cochée."""
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", type=int)
    manual_creation_date = request.form.get("manual_creation_date", "").strip()
    if not name:
        return jsonify(ok=False, error="Le nom ne peut pas être vide."), 400
    new_id, error = db.create_playlist(name, parent_id=parent_id, manual_creation_date=manual_creation_date)
    if error:
        return jsonify(ok=False, error=error), 400
    depth = 0
    if parent_id:
        parent_entry = next((p for p in get_flat_playlists() if p["id"] == parent_id), None)
        depth = (parent_entry["depth"] + 1) if parent_entry else 1
    return jsonify(ok=True, id=new_id, name=name, depth=depth, parent_id=parent_id)


@app.route("/api/tag/quick_create", methods=["POST"])
def api_tag_quick_create():
    """Création rapide d'un tag "à vide" (sans passer par le champ Tags
    d'une vidéo), depuis la fenêtre "🏷 Tags" : renvoie le tag créé en JSON
    pour l'ajouter immédiatement à la grille, prêt à recevoir une image."""
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify(ok=False, error="Le nom ne peut pas être vide."), 400
    conn = db.get_db()
    existing = conn.execute(
        "SELECT id FROM tags WHERE name = ?", (name.strip().lower(),)
    ).fetchone()
    if existing:
        return jsonify(ok=False, error="Un tag porte déjà ce nom."), 400
    tag_id = db.get_or_create_tag(name)
    return jsonify(ok=True, id=tag_id, name=name.strip().lower())


def compute_membership_cards(video, selected_playlist_ids):
    """Catégorie / playlists / sous-playlists auxquelles la vidéo appartient
    RÉELLEMENT, indépendamment de tout contexte de navigation — pour être
    TOUJOURS affichées EN CASCADE sous le lecteur (voir .video-context-cards
    dans video.html), qu'on ait ouvert cette vidéo depuis cette playlist/
    catégorie précise ou depuis n'importe où ailleurs (recherche, aléatoire,
    tag...).

    Une vidéo peut être rattachée à PLUSIEURS playlists/sous-playlists à la
    fois (cases à cocher multiples dans le panneau d'édition) : contrairement
    à l'ancienne version qui ne retenait que la plus profonde (les autres
    étaient alors invisibles, bien que bel et bien enregistrées), TOUTES sont
    désormais renvoyées, chacune sous sa propre carte. Une sous-playlist
    (playlist ayant un parent) fait apparaître deux cartes : elle-même
    ("Sous-playlist") et sa playlist racine ("Playlist") — dédupliquées si
    plusieurs sous-playlists sélectionnées partagent la même racine, ou si
    cette racine est elle-même directement sélectionnée par ailleurs.

    Factorisé pour être réutilisé à l'identique par video_detail ET par
    l'enregistrement AJAX des métadonnées (video_edit), qui doit recalculer
    ces cartes sans recharger la page (et donc sans interrompre la lecture
    en cours). Renvoie (category, playlists, sub_playlists)."""
    display_category = db.get_category(video["category_id"]) if video["category_id"] else None
    playlists = []
    sub_playlists = []
    seen_playlist_ids = set()
    seen_sub_ids = set()

    for pid in (selected_playlist_ids or []):
        node = db.get_playlist(pid)
        if not node:
            continue
        breadcrumb = db.get_playlist_breadcrumb(pid)
        if breadcrumb:
            if pid not in seen_sub_ids:
                seen_sub_ids.add(pid)
                sub_playlists.append(node)
            root_id = breadcrumb[-1]["id"]
            if root_id not in seen_playlist_ids:
                root_node = db.get_playlist(root_id)
                if root_node:
                    seen_playlist_ids.add(root_id)
                    playlists.append(root_node)
        else:
            if pid not in seen_playlist_ids:
                seen_playlist_ids.add(pid)
                playlists.append(node)

    return display_category, playlists, sub_playlists


def compute_now_playing_context(playlist_ctx, category_ctx):
    """Détermine l'entité "en cours de lecture" pour le bandeau clignotant
    du haut de l'écran (#player-overlay-top, "Lecture maintenant") — CE
    bandeau ne doit s'afficher QUE lorsqu'on a délibérément cliqué "lire" sur
    une playlist / sous-playlist / catégorie précise (paramètre
    "?playlist=<id>" ou "?category=<id>" dans l'URL, posé par le bouton de
    lecture de ces pages), jamais simplement parce que la vidéo appartient à
    l'une d'elles (voir compute_membership_cards ci-dessus pour ça,
    toujours affiché séparément sous le lecteur). playlist_ctx / category_ctx
    doivent déjà avoir été invalidés (remis à None) en amont si la vidéo ne
    fait finalement pas partie de ce contexte. Renvoie (kind, entity), avec
    kind parmi "sub_playlist" / "playlist" / "category", ou (None, None)."""
    if playlist_ctx:
        breadcrumb = db.get_playlist_breadcrumb(playlist_ctx["id"])
        kind = "sub_playlist" if breadcrumb else "playlist"
        return kind, playlist_ctx
    if category_ctx:
        return "category", category_ctx
    return None, None


def _display_entity_json(entity, kind):
    """Sérialise une catégorie/playlist (ou None) pour la réponse JSON de
    video_edit, afin que le JS puisse reconstruire les "cartes contextuelles"
    sous le lecteur sans recharger la page."""
    if not entity:
        return None
    if kind == "category":
        image_url = url_for("category_image", filename=entity["image1"]) if entity["image1"] else None
        view_url = url_for("category_view", category_id=entity["id"])
    else:
        image_url = url_for("playlist_image", filename=entity["image1"]) if entity["image1"] else None
        view_url = url_for("playlist_view", playlist_id=entity["id"])
    return {"id": entity["id"], "name": entity["name"], "image_url": image_url, "url": view_url}


@app.route("/video/<int:video_id>")
def video_detail(video_id):
    conn = db.get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    # "Disques actifs" (voir Réglages) : une vidéo dont le disque est
    # décoché doit rester injoignable PARTOUT, y compris via un accès
    # direct à cette page (lien déjà ouvert, historique, favori du
    # navigateur...) — pas seulement absente des grilles. Sans ce
    # contrôle, la vidéo restait normalement lisible tant que le disque
    # était encore physiquement branché, malgré la case décochée.
    if (
        not video
        or video["disk_off"]
        or db.path_is_disabled(video["path"], video["folder_root"])
    ):
        return "Vidéo introuvable", 404
    # Une seule vue par vidéo et par session navigateur — que la page soit
    # ouverte en navigation classique ou en soft-nav (fetch). Évite
    # l'inflation des compteurs quand on feuillette "À découvrir aussi".
    viewed = session.get("_viewed_videos") or []
    if not isinstance(viewed, list):
        viewed = []
    if video_id not in viewed:
        conn.execute("UPDATE videos SET views = views + 1 WHERE id = ?", (video_id,))
        conn.commit()
        # Plafond 500 ids : évite une session qui grossit sans fin.
        viewed = (list(viewed) + [video_id])[-500:]
        session["_viewed_videos"] = viewed
    tags = db.get_video_tags(video_id)

    # Si la vidéo est ouverte depuis une playlist (lien "?playlist=<id>" posé
    # par la grille vidéos de la page playlist et par cette page elle-même),
    # "À découvrir aussi" affiche toutes les vidéos de cette playlist, dans
    # leur ordre, et les boutons précédent/suivant naviguent dans cet ordre.
    # De même si elle est ouverte depuis une catégorie (lien "?category=<id>"
    # posé par la grille vidéos de la page catégorie), "À découvrir aussi"
    # affiche alors toutes les vidéos de cette catégorie, et précédent/suivant
    # naviguent successivement dans cet ensemble (façon YouTube).
    # Sans contexte playlist ni catégorie, le comportement d'origine est
    # inchangé : suggestions aléatoires parmi toute la bibliothèque, et
    # précédent/suivant naviguent par ordre d'ajout récent (même ordre que le
    # tri par défaut de l'accueil).
    playlist_ctx_id = request.args.get("playlist", type=int)
    playlist_ctx = db.get_playlist(playlist_ctx_id) if playlist_ctx_id else None
    category_ctx_id = request.args.get("category", type=int)
    category_ctx = db.get_category(category_ctx_id) if category_ctx_id else None
    tag_ctx_id = request.args.get("tag", type=int)
    tag_ctx = db.get_tag(tag_ctx_id) if tag_ctx_id else None
    prev_video = next_video = None
    related = None

    # Nombre max de cartes "Vidéo suivante / À découvrir aussi" affichées
    # dans le panneau latéral de la page vidéo. Sans cette borne, ouvrir une
    # vidéo depuis une catégorie/playlist/tag contenant des milliers d'entrées
    # chargeait et rendait TOUTES les vidéos du contexte (SELECT * sans LIMIT
    # + boucle Jinja complète) — latence et mémoire proportionnelles à la
    # taille de la bibliothèque. La navigation précédent/suivant reste exacte
    # sur l'ensemble (via les ids seuls), seule la liste visible est tronquée.
    RELATED_SIDEBAR_LIMIT = 40

    def _related_from_ids(ids, current_id):
        """À partir d'une liste ordonnée d'ids (légère), calcule prev/next
        et charge au plus RELATED_SIDEBAR_LIMIT lignes complètes pour le
        panneau latéral — centré sur la vidéo courante quand c'est possible."""
        if current_id not in ids:
            return None, None, None
        idx = ids.index(current_id)
        prev_id = ids[idx - 1] if idx > 0 else None
        next_id = ids[idx + 1] if idx < len(ids) - 1 else None
        # Fenêtre autour de la position courante (plutôt que "tout après"),
        # pour que les vidéos voisines restent visibles même en milieu de liste.
        half = RELATED_SIDEBAR_LIMIT // 2
        start = max(0, idx - half)
        end = min(len(ids), start + RELATED_SIDEBAR_LIMIT + 1)  # +1 car on exclut current
        if end - start < RELATED_SIDEBAR_LIMIT + 1 and start > 0:
            start = max(0, end - RELATED_SIDEBAR_LIMIT - 1)
        window_ids = [i for i in ids[start:end] if i != current_id][:RELATED_SIDEBAR_LIMIT]
        need_ids = [i for i in (prev_id, next_id) if i] + window_ids
        need_ids = list(dict.fromkeys(need_ids))  # unique, ordre conservé
        by_id = {}
        if need_ids:
            placeholders = ",".join("?" for _ in need_ids)
            for r in conn.execute(
                f"SELECT * FROM videos WHERE id IN ({placeholders})", need_ids
            ):
                by_id[r["id"]] = dict(r)
        prev_v = by_id.get(prev_id) if prev_id else None
        next_v = by_id.get(next_id) if next_id else None
        related_list = [by_id[i] for i in window_ids if i in by_id]
        return prev_v, next_v, related_list

    if playlist_ctx:
        # Ids seuls (pas SELECT *) : O(n) mémoire légère même pour de très
        # grandes playlists ; les lignes complètes ne sont chargées que pour
        # la fenêtre affichée + prev/next.
        ids = [
            r["id"]
            for r in conn.execute(
                """SELECT v.id FROM videos v
                   JOIN playlist_items pi ON pi.video_id = v.id
                   WHERE pi.playlist_id = ? AND v.missing = 0 AND v.disk_off = 0
                   ORDER BY pi.position ASC""",
                (playlist_ctx_id,),
            )
        ]
        prev_video, next_video, related = _related_from_ids(ids, video_id)
        if related is None:
            # La vidéo ne (ou plus) fait partie de cette playlist : le lien
            # de contexte est obsolète, on retombe sur le comportement normal.
            playlist_ctx = None
            playlist_ctx_id = None

    if related is None and category_ctx:
        ids = [
            r["id"]
            for r in conn.execute(
                """SELECT id FROM videos
                   WHERE category_id = ? AND missing = 0 AND disk_off = 0
                   ORDER BY added_at ASC, id ASC""",
                (category_ctx_id,),
            )
        ]
        prev_video, next_video, related = _related_from_ids(ids, video_id)
        if related is None:
            # La vidéo ne (ou plus) fait partie de cette catégorie : le lien
            # de contexte est obsolète, on retombe sur le comportement normal.
            category_ctx = None
            category_ctx_id = None

    if related is None and tag_ctx:
        ids = [
            r["id"]
            for r in conn.execute(
                """SELECT v.id FROM videos v
                   JOIN video_tags vt ON vt.video_id = v.id
                   WHERE vt.tag_id = ? AND v.missing = 0 AND v.disk_off = 0
                   ORDER BY v.added_at ASC, v.id ASC""",
                (tag_ctx_id,),
            )
        ]
        prev_video, next_video, related = _related_from_ids(ids, video_id)
        if related is None:
            # La vidéo ne (ou plus) porte ce tag : le lien de contexte est
            # obsolète, on retombe sur le comportement normal.
            tag_ctx = None
            tag_ctx_id = None

    next_discover_qs = ""
    if related is None:
        # "À découvrir aussi" sans contexte (depuis "toutes les vidéos" ou
        # l'accueil) : la lecture doit s'enchaîner dans l'ORDRE des cases
        # affichées, sans jamais sauter l'une d'elles, jusqu'à épuisement de
        # la file — moment où de nouvelles cases apparaissent. La file
        # "restante" transite d'une vidéo à l'autre via le paramètre d'URL
        # ?discover=<ids restants>, posé sur chaque lien "suivant"/case.
        #
        # L'apparition de nouvelles vidéos dans ces cases est pondérée par
        # leur nombre de lectures : les vidéos rarement (ou jamais) vues ont
        # une bien plus grande chance d'apparaître que les vidéos très vues
        # (tirage aléatoire pondéré, pas un simple tri par vues, pour garder
        # un peu de variété d'une fois à l'autre).
        DISCOVER_QUEUE_SIZE = 50
        discover_param = (request.args.get("discover") or "").strip()
        queue_ids = []
        seen_ids = {video_id}
        if discover_param:
            for part in discover_param.split(","):
                part = part.strip()
                if part.isdigit():
                    qid = int(part)
                    if qid not in seen_ids:
                        queue_ids.append(qid)
                        seen_ids.add(qid)

        related_rows = []
        if queue_ids:
            placeholders = ",".join("?" for _ in queue_ids)
            rows = conn.execute(
                f"""SELECT * FROM videos WHERE id IN ({placeholders})
                    AND missing = 0 AND disk_off = 0""",
                queue_ids,
            ).fetchall()
            by_id = {r["id"]: r for r in rows}
            related_rows = [by_id[i] for i in queue_ids if i in by_id]

        if len(related_rows) < DISCOVER_QUEUE_SIZE:
            exclude_ids = seen_ids | {r["id"] for r in related_rows}
            needed = DISCOVER_QUEUE_SIZE - len(related_rows)
            exclude_placeholders = ",".join("?" for _ in exclude_ids)
            extra = conn.execute(
                f"""SELECT * FROM videos
                    WHERE missing = 0 AND disk_off = 0
                    AND id NOT IN ({exclude_placeholders})
                    ORDER BY (COALESCE(views, 0) + 1)
                             * ((ABS(RANDOM()) % 1000) + 1) ASC
                    LIMIT ?""",
                (*exclude_ids, needed),
            ).fetchall()
            related_rows = related_rows + list(extra)

        related_ids_full = [r["id"] for r in related_rows]
        related = []
        for r in related_rows:
            item = dict(r)
            # Borne la longueur de discover_qs (évite des URL de plusieurs
            # Ko après une longue session "À découvrir" → 414 / truncation).
            rest = [str(i) for i in related_ids_full if i != r["id"]][:40]
            item["discover_qs"] = ",".join(rest)
            related.append(item)

        if related:
            next_video = related_rows[0]
            next_discover_qs = related[0]["discover_qs"]
        else:
            next_video = None

        row = conn.execute(
            """SELECT * FROM videos WHERE missing = 0 AND disk_off = 0
               AND (added_at > ? OR (added_at = ? AND id > ?))
               ORDER BY added_at ASC, id ASC LIMIT 1""",
            (video["added_at"], video["added_at"], video_id),
        ).fetchone()
        prev_video = row

    categories = get_flat_categories()
    playlists = get_flat_playlists()
    selected_playlist_ids = db.get_video_playlist_ids(video_id)
    sidebar_categories, sidebar_playlists, popular_tags = get_sidebar_data()

    # Catégorie / playlists / sous-playlists à présenter EN CASCADE sous le
    # lecteur (façon YouTube : "titre + image de couverture"), pour TOUTES
    # les playlists/sous-playlists/catégorie auxquelles la vidéo est
    # réellement rattachée — indépendamment de tout contexte de navigation
    # (voir compute_membership_cards).
    display_category, display_playlists, display_sub_playlists = compute_membership_cards(
        video, selected_playlist_ids
    )
    # Bandeau "Lecture maintenant" (haut de l'écran) : uniquement si on lit
    # la vidéo depuis un clic "lire" explicite sur une playlist / sous-
    # playlist / catégorie précise — playlist_ctx / category_ctx ont déjà
    # été remis à None ci-dessus si la vidéo n'en fait finalement pas partie.
    now_playing_kind, now_playing_entity = compute_now_playing_context(playlist_ctx, category_ctx)

    ext = os.path.splitext(video["path"])[1].lower()
    likely_playable = ext in {".mp4", ".webm", ".m4v", ".ogv"}

    # Codec lourd (HEVC/AV1/10-bit/4K…) : ne jamais démarrer la lecture ni
    # un transcodage automatique tant qu'aucune version compatible n'est
    # déjà prête. Sinon le décodage logiciel (navigateur) ou ffmpeg peut
    # monter le CPU à 100 % — sauf pendant un scan explicitement lancé.
    codec_risky = bool(video["codec_risky"])
    has_compat_ready = codec_risky and os.path.exists(_transcode_output_path(video_id))
    # Bloquer lecture auto dès qu'un codec lourd n'a pas de version allégée
    # prête (y compris formats "likely_playable" en conteneur mais lourds).
    block_heavy_autoplay = codec_risky and not has_compat_ready

    initial_stream_url = url_for("stream", video_id=video_id)
    if has_compat_ready:
        initial_stream_url = url_for("stream_transcode", video_id=video_id)

    return render_template(
        "video.html", video=video, tags=tags, related=related,
        next_discover_qs=next_discover_qs, initial_stream_url=initial_stream_url,
        categories=categories, playlists=playlists, likely_playable=likely_playable,
        codec_risky=codec_risky, has_compat_ready=has_compat_ready,
        block_heavy_autoplay=block_heavy_autoplay,
        selected_playlist_ids=selected_playlist_ids,
        sidebar_categories=sidebar_categories, sidebar_playlists=sidebar_playlists,
        popular_tags=popular_tags,
        playlist_ctx_id=playlist_ctx_id, category_ctx_id=category_ctx_id, tag_ctx_id=tag_ctx_id,
        prev_video=prev_video, next_video=next_video,
        display_category=display_category,
        display_playlists=display_playlists,
        display_sub_playlists=display_sub_playlists,
        now_playing_kind=now_playing_kind,
        now_playing_entity=_display_entity_json(
            now_playing_entity, "category" if now_playing_kind == "category" else "playlist"
        ) if now_playing_entity else None,
    )


@app.route("/api/shuffle_video/<int:video_id>")
def api_shuffle_video(video_id):
    """Utilisé par le bouton "lecture aléatoire 🔀" de la page vidéo : tire
    au sort une AUTRE vidéo parmi celles du même contexte que la vidéo en
    cours (playlist/sous-playlist si "?playlist=<id>", catégorie si
    "?category=<id>", ou toute la bibliothèque sinon), exactement comme le
    ferait la navigation précédent/suivant normale mais sans respecter
    l'ordre. Renvoie {id: null} si aucune autre vidéo n'est disponible dans
    ce contexte (rien à lire d'autre)."""
    conn = db.get_db()
    playlist_ctx_id = request.args.get("playlist", type=int)
    category_ctx_id = request.args.get("category", type=int)

    if playlist_ctx_id:
        rows = conn.execute(
            """SELECT v.id FROM videos v
               JOIN playlist_items pi ON pi.video_id = v.id
               WHERE pi.playlist_id = ? AND v.missing = 0 AND v.disk_off = 0""",
            (playlist_ctx_id,),
        ).fetchall()
    elif category_ctx_id:
        rows = conn.execute(
            "SELECT id FROM videos WHERE category_id = ? AND missing = 0 AND disk_off = 0",
            (category_ctx_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM videos WHERE missing = 0 AND disk_off = 0").fetchall()

    ids = [r["id"] for r in rows]
    candidates = [i for i in ids if i != video_id]
    if not candidates:
        return jsonify(id=None)
    return jsonify(id=random.choice(candidates))


@app.route("/video/<int:video_id>/feature", methods=["POST"])
def feature_video(video_id):
    db.set_featured(video_id)
    flash("Vidéo mise à la une.")
    return redirect(request.referrer or url_for("video_detail", video_id=video_id))


@app.route("/video/<int:video_id>/unfeature", methods=["POST"])
def unfeature_video(video_id):
    db.unset_featured(video_id)
    flash("Vidéo retirée de la une.")
    return redirect(request.referrer or url_for("index"))


@app.route("/video/<int:video_id>/favorite/toggle", methods=["POST"])
def toggle_favorite_video(video_id):
    """Bascule l'état favori/♥ d'une vidéo — accès rapide indépendant de
    la note par étoiles. Appelé en AJAX depuis le bouton cœur des cartes
    et de la page vidéo (voir js-fav-toggle dans main.js) ; conserve
    aussi un repli non-JS via redirect si le JS est indisponible."""
    new_value = db.toggle_favorite(video_id)
    if new_value is None:
        if request.form.get("ajax"):
            return jsonify(ok=False, error="Vidéo introuvable."), 404
        flash("Vidéo introuvable.")
        return redirect(request.referrer or url_for("index"))
    if request.form.get("ajax"):
        return jsonify(ok=True, favorite=bool(new_value))
    flash("Ajoutée aux favoris." if new_value else "Retirée des favoris.")
    return redirect(request.referrer or url_for("video_detail", video_id=video_id))


@app.route("/bulk_edit", methods=["POST"])
def bulk_edit():
    ids = request.form.getlist("video_id", type=int)
    # Dédoublonnage + plafond (évite payload énorme / timeout SQL)
    ids = list(dict.fromkeys(i for i in ids if i and i > 0))
    if len(ids) > 500:
        ids = ids[:500]
    category_name = request.form.get("category", "").strip()
    add_tags_raw = request.form.get("add_tags", "").strip()
    remove_tags_raw = request.form.get("remove_tags", "").strip()
    add_tags = [t.strip() for t in add_tags_raw.split(",") if t.strip()][:50]
    remove_tags = [t.strip() for t in remove_tags_raw.split(",") if t.strip()][:50]

    if not ids:
        flash("Aucune vidéo sélectionnée.")
        return redirect(request.referrer or url_for("index"))

    db.bulk_apply(ids, category_name or None, add_tags or None, remove_tags or None)
    flash(f"{len(ids)} vidéo(s) mise(s) à jour.")
    return redirect(request.referrer or url_for("index"))


@app.route("/video/<int:video_id>/set_category/<int:category_id>", methods=["POST"])
def video_set_category(video_id, category_id):
    """Affecte rapidement une catégorie à une vidéo (bouton « + Ajouter à une
    catégorie », depuis la page vidéo ou la popup de modification rapide).
    Une vidéo n'ayant qu'une seule catégorie, ceci remplace l'éventuelle
    catégorie précédente."""
    conn = db.get_db()
    video = conn.execute("SELECT id FROM videos WHERE id=?", (video_id,)).fetchone()
    if not video:
        if request.form.get("ajax"):
            return jsonify(ok=False, error="Vidéo introuvable"), 404
        flash("Vidéo introuvable.")
        return redirect(url_for("index"))
    cat = conn.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
    if not cat:
        if request.form.get("ajax"):
            return jsonify(ok=False, error="Catégorie introuvable"), 404
        flash("Catégorie introuvable.")
        return redirect(request.referrer or url_for("video_detail", video_id=video_id))
    conn.execute("UPDATE videos SET category_id=? WHERE id=?", (category_id, video_id))
    conn.commit()
    if request.form.get("ajax"):
        return jsonify(ok=True)
    flash("Catégorie mise à jour.")
    return redirect(request.referrer or url_for("video_detail", video_id=video_id))


@app.route("/video/<int:video_id>/edit", methods=["POST"])
def video_edit(video_id):
    is_ajax = bool(request.form.get("ajax"))
    conn = db.get_db()

    existing = conn.execute("SELECT id, title FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not existing:
        if is_ajax:
            return jsonify(ok=False, error="Vidéo introuvable."), 404
        return "Vidéo introuvable", 404

    try:
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category_name = request.form.get("category", "").strip()
        tags_raw = request.form.get("tags", "")
        tag_names = [t.strip() for t in tags_raw.split(",") if t.strip()]
        rating = request.form.get("rating", type=int) or 0
        rating = max(0, min(5, rating))
        video_date = request.form.get("video_date", "").strip()
        comment = request.form.get("comment", "").strip()
        playlist_ids = request.form.getlist("playlists", type=int)
        views = request.form.get("views", type=int)

        category_id = db.get_or_create_category(category_name) if category_name else None
        final_title = title or "(sans titre)"

        # Si le titre a réellement été changé à la main par rapport à ce qui
        # est en base, on le fige (title_is_custom=1) : un futur renommage
        # physique du fichier détecté par le scanner ne l'écrasera plus
        # jamais automatiquement (voir db.relocate_video).
        if final_title != existing["title"]:
            conn.execute(
                "UPDATE videos SET title=?, description=?, category_id=?, title_is_custom=1 WHERE id=?",
                (final_title, description, category_id, video_id),
            )
        else:
            conn.execute(
                "UPDATE videos SET title=?, description=?, category_id=? WHERE id=?",
                (final_title, description, category_id, video_id),
            )
        conn.commit()
        db.set_video_tags(video_id, tag_names)

        # Images de tags ajoutées/modifiées directement depuis le champ
        # "Tags" du panneau "✎ Modifier les métadonnées" (voir
        # _tag_field.html) : chaque puce de tag portant une photo en attente
        # envoie son nom + son fichier en deux listes appariées (même ordre),
        # plutôt qu'un nom de champ dynamique par tag. On résout chaque nom
        # en identifiant de tag via get_or_create_tag (déjà créé juste au-
        # dessus par set_video_tags, donc idempotent ici), ce qui permet
        # aussi bien de mettre à jour l'image d'un tag existant que celle
        # d'un tag flambant neuf, en un seul enregistrement.
        tag_image_names = request.form.getlist("tag_image_name")
        tag_image_files = request.files.getlist("tag_image_file")
        for t_name, t_file in zip(tag_image_names, tag_image_files):
            t_name = t_name.strip()
            if not t_name:
                continue
            saved = _save_image_upload(t_file, config.TAG_IMAGES_DIR)
            if saved:
                tag_id = db.get_or_create_tag(t_name)
                db.update_tag_images(tag_id, {"image1": saved})

        db.set_video_playlists(video_id, playlist_ids)
        db.update_video_extended(video_id, rating, video_date, comment, views=views)
    except Exception as e:
        if is_ajax:
            return jsonify(ok=False, error=str(e)), 500
        raise

    if is_ajax:
        # La popup de modification rapide met à jour la carte de la vidéo
        # directement dans la grille (sans recharger toute la page), donc on
        # renvoie ici les valeurs enregistrées telles qu'elles ont été
        # traitées côté serveur (titre "(sans titre)" par défaut, vues
        # bornées à 0 minimum, etc.) pour rester bien synchronisé avec la BDD.
        fresh = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        fresh_tags = db.get_video_tags(video_id)
        selected_playlist_ids = db.get_video_playlist_ids(video_id)

        # Contexte de navigation ("?playlist=<id>" / "?category=<id>") tel que
        # posé sur la page vidéo au moment où le formulaire a été soumis :
        # transmis par des champs cachés du formulaire, pour recalculer le
        # bandeau "Lecture maintenant" exactement comme le ferait un
        # rechargement de la page, mais SANS recharger la page (la lecture en
        # cours du lecteur vidéo ne doit jamais être interrompue par un
        # enregistrement).
        playlist_ctx_id = request.form.get("playlist_ctx_id", type=int)
        playlist_ctx = db.get_playlist(playlist_ctx_id) if playlist_ctx_id else None
        # Invalidé si la vidéo ne fait (plus) partie de cette playlist après
        # cet enregistrement (ex. on vient de la décocher dans le panneau) :
        # le bandeau "Lecture maintenant" ne doit pas survivre à ça.
        if playlist_ctx_id is not None and playlist_ctx_id not in selected_playlist_ids:
            playlist_ctx = None

        category_ctx_id = request.form.get("category_ctx_id", type=int)
        category_ctx = db.get_category(category_ctx_id) if category_ctx_id else None
        if category_ctx_id is not None and category_ctx_id != fresh["category_id"]:
            category_ctx = None

        # Catégorie / playlists / sous-playlists d'appartenance : toujours
        # recalculées à partir des données fraîchement enregistrées, pour
        # que les cartes sous le lecteur restent justes quel que soit le
        # contexte de navigation (voir compute_membership_cards).
        display_category, display_playlists, display_sub_playlists = compute_membership_cards(
            fresh, selected_playlist_ids
        )
        now_playing_kind, now_playing_entity = compute_now_playing_context(playlist_ctx, category_ctx)

        return jsonify(
            ok=True,
            title=fresh["title"],
            description=fresh["description"] or "",
            comment=comment,
            video_date=fresh["video_date"] or "",
            video_date_display=format_date_fr(fresh["video_date"]),
            tags=[{"id": t["id"], "name": t["name"],
                   "image_url": url_for("tag_image", filename=t["image1"]) if t["image1"] else None}
                  for t in fresh_tags],
            views=fresh["views"] or 0,
            rating=fresh["rating"] or 0,
            category_id=fresh["category_id"],
            playlist_ids=selected_playlist_ids,
            display_category=_display_entity_json(display_category, "category"),
            display_playlists=[_display_entity_json(p, "playlist") for p in display_playlists],
            display_sub_playlists=[_display_entity_json(p, "playlist") for p in display_sub_playlists],
            now_playing_kind=now_playing_kind,
            now_playing_entity=_display_entity_json(
                now_playing_entity, "category" if now_playing_kind == "category" else "playlist"
            ) if now_playing_entity else None,
        )

    redirect_to = request.form.get("redirect_to")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("video_detail", video_id=video_id))


def _send_to_recycle_bin_once(path):
    """Déplace le fichier vers la corbeille Windows (SHFileOperationW).
    Appli conçue pour Windows uniquement.
    Retourne (True, None) en succès, (False, message) sinon."""
    if platform.system() == "Windows":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40       # place dans la corbeille au lieu d'effacer définitivement
        FOF_NOCONFIRMATION = 0x10  # pas de popup de confirmation Windows
        FOF_SILENT = 0x4           # pas de barre de progression Windows
        FOF_NOERRORUI = 0x400      # pas de popup d'erreur Windows (on gère nous-même)

        # pFrom doit être terminé par un double caractère nul.
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = path + "\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0:
            # Code 32 = ERROR_SHARING_VIOLATION (fichier encore ouvert/en
            # cours de lecture côté navigateur/lecteur) : l'appelant retente.
            return False, f"code d'erreur Windows {result}"
        if op.fAnyOperationsAborted:
            return False, "opération annulée"
        return True, None
    else:
        try:
            import send2trash
            send2trash.send2trash(path)
            return True, None
        except ImportError:
            try:
                os.remove(path)
                return True, None
            except OSError as e:
                return False, str(e)
        except Exception as e:
            return False, str(e)


def send_to_recycle_bin(path, attempts=6, delay_seconds=0.4):
    """Déplace le fichier vers la corbeille, avec plusieurs tentatives en
    cas de verrouillage momentané (ex. ERROR_SHARING_VIOLATION côté Windows
    quand la vidéo vient tout juste d'être fermée côté lecteur/streaming)."""
    last_error = None
    for _ in range(max(1, attempts)):
        ok, error = _send_to_recycle_bin_once(path)
        if ok:
            return True, None
        last_error = error
        time.sleep(delay_seconds)
    return False, last_error


def delete_thumbnail_files(thumbnail, preview_count):
    """Efface du disque la vignette principale et les images d'aperçu au
    survol liées à une vidéo (fichiers dans config.THUMBS_DIR uniquement).
    Ne lève jamais d'erreur : un fichier déjà absent n'est pas un problème."""
    if not thumbnail:
        return
    thumb_path = os.path.join(config.THUMBS_DIR, thumbnail)
    try:
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
    except OSError:
        pass
    thumb_base_noext = os.path.splitext(thumbnail)[0]
    for i in range(1, (preview_count or 0) + 1):
        preview_path = os.path.join(config.THUMBS_DIR, f"{thumb_base_noext}_p{i}.jpg")
        try:
            if os.path.exists(preview_path):
                os.remove(preview_path)
        except OSError:
            pass


@app.route("/video/<int:video_id>/delete_file", methods=["POST"])
def delete_video_file(video_id):
    """Bouton 🗑 de la page lecture vidéo : déplace le fichier physique dans
    la corbeille Windows (jamais de suppression définitive directe) puis
    retire l'entrée de la bibliothèque. Ne touche à aucune autre vidéo, ni
    à ses playlists/catégories/tags (qui disparaissent avec elle, comme
    pour n'importe quelle vidéo supprimée). Sa vignette et ses images
    d'aperçu au survol sont aussi effacées du disque à cette occasion.

    Sécurité : exige confirm=1 (ou confirm=yes) dans le corps pour éviter
    un POST accidentel / rejoué sans intention explicite côté client."""
    confirm = (
        request.form.get("confirm")
        or (request.get_json(silent=True) or {}).get("confirm")
        or ""
    )
    if str(confirm).lower() not in ("1", "yes", "true", "ok"):
        return jsonify(
            ok=False,
            error="Confirmation manquante. La suppression du fichier n'a pas été effectuée.",
        ), 400
    conn = db.get_db()
    video = conn.execute(
        "SELECT path, thumbnail, preview_count FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if not video:
        return jsonify(ok=False, error="Vidéo introuvable."), 404
    path = video["path"]
    # Ferme immédiatement, côté serveur, tout descripteur de fichier encore
    # ouvert pour le streaming de cette vidéo (ex. requête Range restée en
    # suspens pendant que le lecteur était en pause) : c'est la cause
    # concrète d'un fichier "verrouillé" sous Windows au moment de la
    # suppression, quel que soit le nombre de tentatives ensuite. En
    # fermant nous-mêmes le handle qui nous appartient, la suppression
    # devient fiable indépendamment du minutage du navigateur.
    force_close_stream_handles(video_id)
    if os.path.exists(path):
        ok, error = send_to_recycle_bin(path)
        if not ok:
            return jsonify(
                ok=False,
                error=f"Le fichier semble encore verrouillé, réessaie dans un instant ({error}).",
            ), 500
    conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    conn.commit()
    delete_thumbnail_files(video["thumbnail"], video["preview_count"])
    _cleanup_transcode(video_id)
    return jsonify(ok=True)


@app.route("/video/<int:video_id>/remove", methods=["POST"])
def video_remove(video_id):
    """Retire l'entrée de la bibliothèque. Ne supprime jamais le fichier réel."""
    conn = db.get_db()
    conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    conn.commit()
    _cleanup_transcode(video_id)
    flash("Vidéo retirée de la bibliothèque (le fichier original n'a pas été touché).")
    return redirect(url_for("index"))


# Correspondance extension -> type MIME correct pour la vidéo en streaming.
# Sans ça, Python (mimetypes.guess_type, utilisé en interne par send_file)
# ne connaît pas certains formats très courants dans une bibliothèque locale
# (.mkv notamment, mais aussi .ts/.avi/.flv/.wmv/.mpg selon les machines) et
# renvoie un Content-Type générique voire absent. Le navigateur doit alors
# "deviner" le format à la volée : ça fonctionne au début de la lecture
# (assez de données pour sniffer), mais peut se mettre à échouer plus loin
# dans la vidéo, typiquement lors d'un saut dans la timeline ou d'un
# changement de type de trame (plantage/gel du lecteur en cours de lecture).
# Un Content-Type explicite et correct dès la première requête supprime ce
# problème à la source, en particulier pour les vidéos haute résolution où
# chaque requête partielle (Range) doit être interprétée sans ambiguïté.
STREAM_MIMETYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
    ".3gp": "video/3gpp",
    ".vob": "video/dvd",
}


def _serve_video_file(video_id, path, mimetype):
    """Diffuse un fichier vidéo (le fichier original OU sa version convertie
    en cache, voir _transcode_stream_path ci-dessous) au navigateur avec un
    support complet des requêtes partielles (Range — indispensable pour
    glisser dans la timeline sans tout retélécharger). Implémenté "à la
    main" (plutôt que via send_file) pour garder la main sur le cycle de vie
    exact du descripteur de fichier ouvert : chaque flux ouvert est
    enregistré (voir _register_stream_handle) et peut donc être fermé
    explicitement et immédiatement à tout moment — notamment juste avant une
    suppression physique, pour garantir que Windows ne considère jamais le
    fichier comme "verrouillé" par notre propre serveur. Factorisé pour être
    partagé entre /stream/<id> (fichier d'origine) et
    /stream/<id>/transcode (secours converti, mêmes garanties de fiabilité)."""
    if not os.path.exists(path):
        return "Fichier introuvable", 404

    # Limite de flux simultanés : on acquiert le sémaphore avant toute
    # ouverture de fichier. Si 4 flux sont déjà actifs, la requête
    # attend brièvement (0.5s max) puis renvoie 503 pour ne pas
    # bloquer indéfiniment le navigateur ni ralentir le PC.
    acquired = _stream_semaphore.acquire(timeout=0.5)
    if not acquired:
        return "Vidéo temporairement inaccessible", 503

    try:
        stat = os.stat(path)
        file_size = stat.st_size
    except OSError:
        _stream_semaphore.release()
        return "Vidéo temporairement inaccessible", 503

    # --- Cache HTTP conditionnel (ETag / Last-Modified) ---------------------
    # Sans validateur de cache, le navigateur ne peut jamais réutiliser un
    # segment déjà téléchargé : chaque retour en arrière dans la timeline
    # (ou même une simple reprise de lecture) redéclenche une requête réseau
    # complète pour des octets pourtant déjà en mémoire/disque côté client —
    # une cause fréquente des petits "arrêts" pendant le glissement de la
    # barre de progression. En exposant un ETag stable (taille + date de
    # modification du fichier) et un Last-Modified, le navigateur peut
    # servir instantanément depuis son propre cache les zones déjà lues
    # (retour en arrière dans la timeline) sans le moindre aller-retour
    # réseau, tout en revalidant correctement si le fichier a changé
    # entre-temps (ex. reconversion). Purement additif : ne modifie ni la
    # logique Range existante, ni le comportement pour un client qui
    # ignorerait ces en-têtes (il continuera de fonctionner à l'identique).
    etag = f'W/"{video_id}-{int(stat.st_mtime)}-{file_size}"'
    last_modified_dt = time.gmtime(stat.st_mtime)
    last_modified = time.strftime("%a, %d %b %Y %H:%M:%S GMT", last_modified_dt)

    start, end = 0, file_size - 1
    status = 200
    range_header = request.headers.get("Range")
    # If-Range : le navigateur ne demande de reprendre une requête Range
    # partielle QUE si sa version en cache est toujours valide. Si notre
    # validateur ne correspond plus (fichier modifié depuis), on ignore
    # volontairement l'en-tête Range et on repart sur le fichier complet,
    # exactement le comportement HTTP standard attendu ici.
    if_range = request.headers.get("If-Range")
    if range_header and if_range and if_range not in (etag, last_modified):
        range_header = None
    open_ended_range = False
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if m:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                else:
                    open_ended_range = True
            elif m.group(2):
                # Forme "bytes=-N" : les N DERNIERS octets du fichier (norme
                # HTTP). C'était auparavant interprété comme "les N premiers",
                # ce qui empêchait de précharger l'index ("moov") situé en fin
                # de certains MP4 — d'où un démarrage plus lent que prévu.
                suffix = int(m.group(2))
                if suffix <= 0:
                    resp = Response(status=416)
                    resp.headers["Content-Range"] = f"bytes */{file_size}"
                    _stream_semaphore.release()
                    return resp
                start = max(0, file_size - suffix)
                end = file_size - 1
            if start >= file_size or start > end:
                resp = Response(status=416)
                resp.headers["Content-Range"] = f"bytes */{file_size}"
                _stream_semaphore.release()
                return resp
            if end > file_size - 1:
                end = file_size - 1
            status = 206

    # --- Fenêtre de réponse bornée pour les requêtes "jusqu'à la fin" -------
    # Quand le navigateur demande "bytes=X-" (à partir d'ici, jusqu'au bout),
    # le serveur s'engageait à envoyer parfois plusieurs Go dans une seule
    # réponse. À chaque saut dans la timeline, cette réponse géante devait
    # être abandonnée en cours de route : la connexion TCP reste alors
    # encombrée d'octets déjà partis mais devenus inutiles, et la nouvelle
    # position doit attendre derrière eux — un micro-arrêt à chaque
    # navigation. En répondant par fenêtres (norme HTTP : une réponse 206
    # peut être plus courte que demandée), le navigateur enchaîne
    # naturellement la suite sur la même connexion réutilisée, et un saut
    # n'a plus au maximum qu'une petite fenêtre à abandonner.
    RANGE_WINDOW = 12 * 1024 * 1024
    if open_ended_range and (end - start + 1) > RANGE_WINDOW:
        end = start + RANGE_WINDOW - 1



    # Revalidation conditionnelle (If-None-Match / If-Modified-Since) : si le
    # navigateur possède déjà exactement ce même fichier en cache et n'a pas
    # demandé de segment précis (pas de Range), on répond 304 sans rouvrir
    # ni relire le fichier — utile notamment pour les requêtes de
    # (re)vérification que certains lecteurs/extensions effectuent avant de
    # reprendre une lecture déjà commencée.
    if not range_header:
        inm = request.headers.get("If-None-Match")
        if inm and inm == etag:
            resp = Response(status=304)
            resp.headers["ETag"] = etag
            resp.headers["Last-Modified"] = last_modified
            resp.headers["Cache-Control"] = "private, max-age=3600"
            _stream_semaphore.release()
            return resp

    length = end - start + 1
    MAX_CHUNK_SIZE = 4 * 1024 * 1024  # 4 Mo : blocs courts = latence minimale  # 32 Mo : plafond relevé (usage local/LAN, sans intérêt à le limiter davantage)
    # pour un débit maximal en régime établi sur les gros fichiers 4K/haut
    # bitrate, sans surcharger la mémoire (au plus trois blocs de cette
    # taille vivent en mémoire à la fois par flux ouvert, voir pré-lecture
    # ci-dessous : 96 Mo maximum par flux, négligeable pour un usage
    # personnel sur une machine moderne).
    FIRST_CHUNK_SIZE = 256 * 1024      # 256 Ko : premier bloc volontairement petit
    # pour que la lecture reprenne quasi instantanément juste après un
    # "seek" dans la timeline (le tout premier octet part sans attendre
    # d'avoir rempli plusieurs Mo), avant de monter en puissance pour le débit.
    # CORRECTIF MICRO-ARRÊTS : les blocs de 8 à 32 Mo obligeaient le serveur à
    # faire une seule lecture disque énorme et bloquante par bloc. Pendant
    # cette lecture, rien ne partait vers le navigateur, et un saut demandé
    # au même moment devait attendre la fin du bloc en cours : exactement le
    # petit arrêt ressenti en navigant dans la timeline. Des blocs courts
    # (4 Mo max) donnent le même débit en local/LAN tout en gardant le flux
    # régulier et immédiatement interruptible.
    RAMP_STEPS = [FIRST_CHUNK_SIZE, 512 * 1024, 1 * 1024 * 1024,
                  2 * 1024 * 1024, MAX_CHUNK_SIZE]

    try:
        fh = open(path, "rb")
    except (OSError, IOError):
        # Fichier momentanément inaccessible (dossier réseau/NAS qui a un
        # accroc, disque externe débranché un instant...) : un 503 propre
        # permet au lecteur de retenter proprement plutôt que de subir une
        # connexion coupée brutalement sans raison apparente.
        _stream_semaphore.release()
        return "Vidéo temporairement inaccessible", 503
    fh.seek(start)
    # Indice de pré-lecture au niveau du système d'exploitation (Linux) : en
    # plus du double tampon applicatif ci-dessous, on informe directement le
    # noyau que les Mo qui suivent la position demandée vont être lus
    # incessamment, pour qu'il les rapatrie en cache page pendant qu'on
    # traite déjà les tout premiers octets. Purement additif et silencieux :
    # absent sous Windows/macOS (pas de posix_fadvise), il ne fait alors
    # simplement rien, sans aucune conséquence sur le comportement.
    if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_WILLNEED"):
        try:
            os.posix_fadvise(fh.fileno(), start, min(length, 64 * 1024 * 1024), os.POSIX_FADV_WILLNEED)
        except OSError:
            pass
    _register_stream_handle(video_id, fh)
    closed = threading.Event()

    def close_once():
        if not closed.is_set():
            closed.set()
            try:
                fh.close()
            except Exception:
                pass
            _unregister_stream_handle(video_id, fh)
            _stream_semaphore.release()

    # -------------------------------------------------------------------
    # Pré-lecture en double tampon (lecture disque/réseau en avance sur
    # l'envoi réseau) : un thread dédié à ce flux lit le prochain bloc
    # PENDANT que le bloc courant part encore vers le navigateur, au lieu
    # d'alterner strictement "lire puis envoyer, lire puis envoyer". Sur
    # un dossier réseau/NAS ou un disque externe pas toujours instantané,
    # c'est ce qui évite le petit temps mort visible pendant la lecture —
    # la latence de lecture disque est masquée derrière le temps de
    # transmission réseau du bloc précédent. Purement additif : mêmes
    # données, même ordre, mêmes octets, seule la manière de les préparer
    # change. La file (maxsize=3, voir generate() ci-dessous) borne
    # strictement la mémoire supplémentaire utilisée à quelques blocs
    # d'avance par flux (au plus MAX_CHUNK_SIZE chacun).
    # -------------------------------------------------------------------
    _SENTINEL = object()

    def producer(q):
        remaining = length
        step = 0
        try:
            while remaining > 0 and not closed.is_set():
                size = RAMP_STEPS[min(step, len(RAMP_STEPS) - 1)]
                step += 1
                chunk = fh.read(min(size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                # put() bloquant avec réveil périodique : si le consommateur
                # (envoi réseau) prend du retard, ce thread patiente sans
                # tourner en boucle active, tout en restant capable de
                # s'arrêter rapidement si le flux est fermé entre-temps
                # (client qui a sauté ailleurs dans la timeline).
                while not closed.is_set():
                    try:
                        q.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, IOError, ValueError):
            # Fin de flux silencieuse : accroc disque/réseau ou descripteur
            # fermé entre-temps (suppression) — rien d'anormal à signaler.
            pass
        finally:
            try:
                q.put(_SENTINEL, timeout=0.3)
            except queue.Full:
                pass

    def generate():
        # maxsize=3 : marge de pré-lecture pour absorber un accroc
        # disque/réseau un peu plus long sans que l'envoi réseau n'ait à
        # attendre le prochain bloc. Coût mémoire borné et négligeable pour
        # un usage personnel (3 blocs de MAX_CHUNK_SIZE = 96 Mo maximum par
        # flux ouvert sur une machine moderne).
        q = queue.Queue(maxsize=4)  # ~16 Mo d'avance max : lissé, jamais en rafale
        t = threading.Thread(target=producer, args=(q,), daemon=True)
        t.start()
        try:
            while not closed.is_set():
                try:
                    chunk = q.get(timeout=0.25)
                except queue.Empty:
                    if closed.is_set():
                        break
                    continue
                if chunk is _SENTINEL:
                    break
                yield chunk
        finally:
            close_once()
            # Le thread de pré-lecture se termine de lui-même dès qu'il
            # observe "closed" (au plus ~0.5s d'attente) ; pas besoin de le
            # bloquer ici pour ne pas retarder la fermeture de la réponse.

    resp = Response(generate(), status=status, mimetype=mimetype, direct_passthrough=True)
    resp.headers["Content-Length"] = str(length)
    resp.headers["Accept-Ranges"] = "bytes"
    # "private, max-age=3600" (au lieu de "no-cache") : autorise le
    # navigateur à réutiliser directement, depuis son propre cache local et
    # SANS aller-retour réseau, les portions de la vidéo déjà chargées
    # pendant la même session de visionnage (retour en arrière dans la
    # timeline, reprise après une navigation entre vidéos, etc.). Combiné à
    # l'ETag/Last-Modified ci-dessus, le navigateur revalidera correctement
    # si le fichier a changé (conversion de secours terminée, fichier
    # remplacé) plutôt que de servir une version périmée.
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["ETag"] = etag
    resp.headers["Last-Modified"] = last_modified
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    resp.call_on_close(close_once)
    return resp


@app.route("/stream/<int:video_id>")
def stream(video_id):
    scanner.notify_playback_active()
    _kill_background_transcode()
    conn = db.get_db()
    video = conn.execute(
        "SELECT path, folder_root, disk_off FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    # Double garde : colonne disk_off (cache indexé) + path_is_disabled
    # (source de vérité settings en mémoire). Les deux doivent autoriser.
    if (
        not video
        or video["disk_off"]
        or db.path_is_disabled(video["path"], video["folder_root"])
    ):
        return "Fichier introuvable", 404
    path = video["path"]
    ext = os.path.splitext(path)[1].lower()
    mimetype = STREAM_MIMETYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    return _serve_video_file(video_id, path, mimetype)


# ---------------------------------------------------------------------------
# Réchauffement anticipé de la timeline (survol / début de glissement)
# ---------------------------------------------------------------------------
# CORRECTIF CHARGE CPU/DISQUE : un pool borné sans concurrence avec la lecture.
_WARM_WORKERS = 1          # 1 seul worker léger, jamais de pic CPU
_WARM_QUEUE_MAXSIZE = 8    # au-delà, les demandes excédentaires sont ignorées (best-effort)
_warm_queue = queue.Queue(maxsize=_WARM_QUEUE_MAXSIZE)


def _warm_worker_loop():
    while True:
        job = _warm_queue.get()
        try:
            if not scanner.is_playback_active():
                path, offset, size_hint = job
                _warm_stream_region(path, offset, size_hint=size_hint)
        except Exception:
            pass  # best-effort : une erreur ici ne doit jamais interrompre le worker
        finally:
            _warm_queue.task_done()


for _i in range(_WARM_WORKERS):
    threading.Thread(target=_warm_worker_loop, daemon=True, name=f"warm-worker-{_i}").start()


_warm_recent_lock = threading.Lock()
_warm_recent = {}          # (path, bucket) -> timestamp du dernier réchauffement
_WARM_DEDUP_SECONDS = 20   # une même zone n'est pas rechauffée deux fois coup sur coup
_WARM_BUCKET_BYTES = 4 * 1024 * 1024


def _warm_recently_done(path, offset):
    key = (path, offset // _WARM_BUCKET_BYTES)
    now = time.time()
    with _warm_recent_lock:
        last = _warm_recent.get(key)
        if last is not None and (now - last) < _WARM_DEDUP_SECONDS:
            return True
        _warm_recent[key] = now
        if len(_warm_recent) > 512:
            for k, v in list(_warm_recent.items()):
                if (now - v) >= _WARM_DEDUP_SECONDS:
                    _warm_recent.pop(k, None)
    return False


def _submit_warm_job(path, offset, size_hint):
    if scanner.is_playback_active() or _warm_recently_done(path, offset):
        return
    try:
        _warm_queue.put_nowait((path, offset, size_hint))
    except queue.Full:
        pass


def _warm_stream_region(path, offset, size_hint=2 * 1024 * 1024):
    """Réchauffe le cache OS si supporté (posix_fadvise), sans surcharger
    le CPU ni le disque en boucle active de lecture."""
    if scanner.is_playback_active():
        return
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return
    if file_size <= 0:
        return
    offset = max(0, min(offset, file_size - 1))
    length = min(size_hint, file_size - offset)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_WILLNEED"):
            try:
                os.posix_fadvise(fd, offset, length, os.POSIX_FADV_WILLNEED)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


@app.route("/stream/<int:video_id>/warm")
def stream_warm(video_id):
    """Déclenche un réchauffement anticipé (voir _warm_stream_region) de la
    zone du fichier correspondant à l'instant ?t=<secondes>, appelé depuis
    static/js/player-smooth.js dès qu'on survole ou commence à glisser sur
    la timeline — c'est-à-dire AVANT le vrai saut. Le temps que
    l'utilisateur relâche réellement le curseur, la zone visée a de bonnes
    chances d'être déjà chaude en cache, ce qui supprime ou réduit le petit
    arrêt qui suivrait sinon un saut à froid, en particulier perceptible en
    navigation "au hasard" dans la timeline.
    Répond immédiatement (202), le réchauffement se poursuivant en tâche de
    fond : cet endpoint ne doit jamais ralentir l'interface ni retarder le
    vrai flux vidéo. Purement additif : n'interfère avec aucun autre
    mécanisme existant (Range, cache HTTP, conversion de secours...)."""
    conn = db.get_db()
    video = conn.execute(
        "SELECT path, folder_root, duration_seconds, disk_off, missing FROM videos WHERE id = ?",
        (video_id,),
    ).fetchone()
    # CORRECTIF "Disques actifs" : disque décoché → pas de préchauffage non plus
    if (
        not video
        or video["disk_off"]
        or video["missing"]
        or not video["path"]
        or db.path_is_disabled(video["path"], video["folder_root"])
        or not os.path.exists(video["path"])
    ):
        return "", 204
    try:
        t = float(request.args.get("t", "0"))
    except (TypeError, ValueError):
        return "", 204
    duration = video["duration_seconds"] or 0
    if duration <= 0 or t < 0:
        return "", 204
    t = min(t, duration)
    try:
        file_size = os.path.getsize(video["path"])
    except OSError:
        return "", 204
    # Estimation simple de la position en octets à partir du temps ciblé (on
    # suppose un débit globalement constant sur le fichier) : suffisante ici
    # puisqu'il ne s'agit que de réchauffer une zone approximative, à la
    # différence du vrai saut (Range dans /stream/<id>) qui reste exact.
    offset = int((t / duration) * file_size)
    # Fenêtre de réchauffement pilotée par le client (voir warmVideoRegion
    # et initTimelinePrewarm dans player-smooth.js), qui adapte la taille
    # selon le contexte (survol/glissement d'un point précis, ou fragment
    # du préchauffage proactif de tout le fichier). Aucun plafond haut
    # n'est imposé ici : seule une lecture par blocs bornés côté
    # _warm_stream_region garantit que la mémoire reste maîtrisée, quelle
    # que soit la taille demandée. Seul un plancher est conservé, pour
    # éviter une requête dégénérée (span nul ou négatif).
    try:
        span = int(request.args.get("span", 6 * 1024 * 1024))
    except (TypeError, ValueError):
        span = 6 * 1024 * 1024
    # Plafond : au-delà de quelques Mo, le préchauffage n'apporte plus rien
    # de perceptible et ne fait que consommer disque et processeur pendant
    # la lecture (voir le préchauffage côté client, également réduit).
    span = max(256 * 1024, min(span, 8 * 1024 * 1024))
    # Fenêtre plus large que le réchauffement "exact" fait côté client (voir
    # warmVideoRegion dans player-smooth.js) : celle-ci ne coûte rien à
    # transmettre (aucune donnée renvoyée au navigateur), donc on peut se
    # permettre de couvrir une zone plus généreuse pour absorber
    # l'imprécision de l'estimation temps → octet (débit non parfaitement
    # constant sur le fichier réel). Déposée dans le pool borné plutôt que
    # d'ouvrir un thread dédié à chaque appel (voir _submit_warm_job
    # ci-dessus) : best-effort, ignorée silencieusement si la file est déjà
    # pleine, sans jamais ralentir cette réponse ni le vrai flux vidéo.
    # Pendant une lecture active : ne pas réchauffer (contention disque/gels).
    if scanner.is_playback_active():
        return "", 204
    _submit_warm_job(video["path"], offset, span)
    return "", 202


# ---------------------------------------------------------------------------
# Conversion automatique de secours (formats non lus nativement)
# ---------------------------------------------------------------------------
# Certains conteneurs/codecs (vieux .wmv, .avi en DivX, .ts, .mkv en codecs
# rares, etc.) ne se décodent pas nativement dans le navigateur, quelle que
# soit la justesse du Content-Type envoyé. Plutôt que de laisser le lecteur
# planté sur une vidéo illisible, on convertit le fichier une seule fois en
# arrière-plan (via le ffmpeg embarqué, voir scanner.FFMPEG) vers un .mp4
# H.264/AAC universellement compatible, mis en cache dans
# config.TRANSCODE_DIR, puis servi ensuite exactement comme une vidéo
# normale (Range, seek, tout fonctionne à l'identique). Le lecteur
# (static/js/player-smooth.js) bascule automatiquement dessus si la lecture
# native échoue de façon persistante.
_transcode_lock = threading.Lock()
_transcode_jobs = {}  # video_id -> {"status": "running"|"ready"|"error", "error": str}

# ---------------------------------------------------------------------------
# CORRECTIF CPU IMPRÉVISIBLE APRÈS SCAN : ARBITRAGE ENTRE CONVERSIONS
# ---------------------------------------------------------------------------
# Deux mécanismes indépendants pouvaient jusqu'ici lancer une conversion
# ffmpeg (transcode) EN MÊME TEMPS, chacun plafonné séparément à
# NORMAL_USE_CPU_PERCENT (70 % par défaut) SANS AUCUNE COORDINATION entre
# eux :
#   1) Le pré-transcodage en arrière-plan (_precode_risky_videos), qui
#      convertit les vidéos à codec à risque une par une, dès que la
#      machine semble inactive ;
#   2) La conversion de secours À LA DEMANDE (cette route, transcode_start),
#      déclenchée par le lecteur (player-smooth.js) quand la vidéo qu'on
#      est justement en train de regarder ne se lit pas nativement.
#
# Si l'utilisateur se met à regarder une vidéo B pile pendant que le
# pré-transcodage de fond convertit une vidéo A, les DEUX processus ffmpeg
# tournent en parallèle, chacun réclamant jusqu'à ~70 % des cœurs : la
# charge totale grimpe alors bien au-delà de 70 %, de façon imprévisible
# (ça dépend uniquement du hasard du minutage) — exactement le symptôme
# observé. Pire, l'arrière-plan continuait jusqu'à SA propre fin (parfois
# plusieurs minutes sur une vidéo 4K) sans jamais céder la place à la
# vidéo qu'on regarde réellement.
#
# Correctif : toute conversion d'arrière-plan (pré-transcodage) enregistre
# désormais son processus ffmpeg en cours dans _bg_transcode_proc. Dès
# qu'une conversion À LA DEMANDE est réclamée (l'utilisateur attend, là,
# tout de suite, devant son écran — priorité absolue), on tue
# IMMÉDIATEMENT toute conversion d'arrière-plan en cours avant de démarrer
# la sienne : plus jamais deux conversions ffmpeg simultanées, donc plus
# jamais de pic de charge imprévisible. La vidéo d'arrière-plan interrompue
# n'est pas perdue : elle reste éligible et sera retentée automatiquement
# au prochain passage de _precode_risky_videos (aucun fichier partiel ne
# reste, voir _transcode_worker).
_bg_transcode_lock = threading.Lock()
_bg_transcode_proc = {"proc": None, "video_id": None}


def _kill_background_transcode():
    """Termine immédiatement la conversion d'arrière-plan en cours, s'il y
    en a une : appelé systématiquement avant de démarrer une conversion À
    LA DEMANDE, pour que l'utilisateur qui attend devant son écran ne
    partage jamais le CPU avec une tâche de fond opportuniste."""
    with _bg_transcode_lock:
        proc = _bg_transcode_proc["proc"]
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def _transcode_output_path(video_id):
    return os.path.join(config.TRANSCODE_DIR, f"{video_id}.mp4")


def _run_transcode_ffmpeg(src_path, tmp_path, use_hw, background_video_id=None):
    """Lance l'appel ffmpeg de conversion, en matériel (GPU) si demandé et
    disponible, sinon en logiciel (comportement d'origine, inchangé). Ne
    lève jamais d'exception : retourne simplement un objet avec .returncode
    (dont l'appelant vérifie le code retour), exactement comme avant l'ajout
    de l'option matérielle.

    background_video_id : si fourni, cet appel est une conversion
    D'ARRIÈRE-PLAN (pré-transcodage) — son processus est enregistré dans
    _bg_transcode_proc le temps de son exécution, pour pouvoir être
    interrompu instantanément par _kill_background_transcode() si une
    conversion à la demande devient nécessaire entre-temps (voir plus
    haut). Une conversion À LA DEMANDE (background_video_id=None) n'est,
    elle, jamais enregistrée : elle ne doit jamais pouvoir être tuée par
    une autre tâche de fond, elle a toujours la priorité."""
    pre_input_args = list(scanner.NORMAL_FFMPEG_THREADS_ARGS)
    encoder = "libx264"
    encoder_args = ["-preset", "veryfast", "-crf", "23"]
    if use_hw and scanner.HW_ENCODER:
        encoder = scanner.HW_ENCODER
        # -threads (décodage logiciel) n'a plus lieu d'être devant -i
        # quand le décodage lui-même est délégué au GPU (-hwaccel) : on le
        # remplace alors par les arguments -hwaccel correspondants, placés
        # avant -i comme l'exige ffmpeg. Sans accélérateur de décodage
        # connu pour cet encodeur (ex. AMF), le décodage reste logiciel
        # mais l'encodage — la partie la plus coûteuse — bascule quand
        # même sur le GPU.
        if scanner.HW_ENCODER_HWACCEL_ARGS:
            pre_input_args = list(scanner.HW_ENCODER_HWACCEL_ARGS)
        else:
            pre_input_args = []
        # Réglages d'encodeur matériel volontairement proches en qualité
        # perçue du réglage logiciel existant (-crf 23) : chaque fabricant
        # expose ses propres options (-cq pour nvenc/qsv, -qp pour amf),
        # d'où ce petit branchement, mais le résultat (fichier .mp4 lisible
        # par le lecteur) reste strictement équivalent du point de vue de
        # l'application.
        if encoder == "h264_nvenc":
            encoder_args = ["-preset", "p4", "-cq", "23"]
        elif encoder == "h264_qsv":
            encoder_args = ["-preset", "veryfast", "-global_quality", "23"]
        elif encoder == "h264_videotoolbox":
            encoder_args = ["-q:v", "60"]
        elif encoder == "h264_amf":
            encoder_args = ["-quality", "speed", "-qp_i", "23", "-qp_p", "23"]

    cmd = (
        scanner.NORMAL_NICE_PREFIX + [scanner.FFMPEG, "-y"] + pre_input_args + [
            "-i", src_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", encoder,
        ] + encoder_args + [
            "-pix_fmt", "yuv420p",
        ] + scanner.NORMAL_FFMPEG_THREADS_ARGS + [
            # -threads répété ici (option de sortie) : limite aussi les
            # threads internes de l'encodeur logiciel (sans effet, mais
            # inoffensif, pour un encodeur matériel).
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-movflags", "+faststart",
            "-f", "mp4",
            tmp_path,
        ]
    )
    try:
        if background_video_id is not None:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **scanner.NORMAL_SUBPROCESS_KWARGS,
            )
            with _bg_transcode_lock:
                _bg_transcode_proc["proc"] = proc
                _bg_transcode_proc["video_id"] = background_video_id
            try:
                returncode = proc.wait()
            finally:
                with _bg_transcode_lock:
                    if _bg_transcode_proc["proc"] is proc:
                        _bg_transcode_proc["proc"] = None
                        _bg_transcode_proc["video_id"] = None
            return subprocess.CompletedProcess(cmd, returncode)
        return subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **scanner.NORMAL_SUBPROCESS_KWARGS,
        )
    except Exception:
        return None


def _transcode_worker(video_id, src_path, is_background=False):
    out_path = _transcode_output_path(video_id)
    tmp_path = out_path + ".part"
    bg_id = video_id if is_background else None
    try:
        # Politique de priorité/threads HORS-SCAN (voir scanner.py,
        # NORMAL_NICE_PREFIX / NORMAL_SUBPROCESS_KWARGS /
        # NORMAL_FFMPEG_THREADS_ARGS) : contrairement au scan qui tourne
        # à pleine puissance (SCAN_CPU_PERCENT = 100, priorité normale),
        # la conversion de secours est plafonnée à 70 % du CPU et tourne en
        # priorité abaissée (nice -n 10 / BELOW_NORMAL) pour que la lecture
        # vidéo et le système restent fluides. Sans ce bridage, l'encodage
        # ffmpeg pouvait saturer tous les cœurs et ramer le reste du PC
        # (y compris le serveur Flask) pendant toute la conversion.
        # Résultat (fichier .mp4) strictement équivalent, seule la vitesse
        # d'exécution en tâche de fond et l'impact CPU changent.
        #
        # CORRECTIF CPU (voir scanner.HW_ENCODER) : tentative en GPU
        # d'abord si un encodeur matériel a été détecté comme disponible
        # dans ce build de ffmpeg, avec repli AUTOMATIQUE et silencieux sur
        # l'encodage logiciel (comportement d'origine, strictement
        # inchangé) si la tentative matérielle échoue pour quelque raison
        # que ce soit (GPU absent malgré le listage, pilote manquant,
        # fichier .part vide...). Jamais d'échec de la conversion à cause
        # de ce choix d'optimisation : le pire cas possible est de
        # retomber exactement sur le comportement déjà existant.
        proc = None
        if scanner.HW_ENCODER:
            proc = _run_transcode_ffmpeg(src_path, tmp_path, use_hw=True, background_video_id=bg_id)
            hw_ok = (
                proc is not None and proc.returncode == 0
                and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0
            )
            if not hw_ok:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                proc = None
        if proc is None:
            proc = _run_transcode_ffmpeg(src_path, tmp_path, use_hw=False, background_video_id=bg_id)
        if proc is None or proc.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError("échec de la conversion ffmpeg")
        os.replace(tmp_path, out_path)
        with _transcode_lock:
            _transcode_jobs[video_id] = {"status": "ready", "error": None}
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        with _transcode_lock:
            # Une conversion d'arrière-plan interrompue volontairement (voir
            # _kill_background_transcode) ne doit jamais rester marquée
            # "error" : ce statut empêcherait de la ré-afficher comme un
            # vrai échec si l'utilisateur revenait sur cette vidéo entre
            # temps, alors qu'il s'agit d'une simple mise en attente. On
            # retire l'entrée à la place : le prochain passage de
            # _precode_risky_videos (ou un nouveau clic utilisateur) la
            # retentera comme si rien n'avait encore été tenté.
            if is_background:
                _transcode_jobs.pop(video_id, None)
            else:
                _transcode_jobs[video_id] = {"status": "error", "error": str(e)}


def _cleanup_transcode(video_id):
    """Supprime le cache de conversion et oublie l'état du job (utilisé
    quand une vidéo est retirée/supprimée de la bibliothèque)."""
    with _transcode_lock:
        _transcode_jobs.pop(video_id, None)
    try:
        out_path = _transcode_output_path(video_id)
        if os.path.exists(out_path):
            os.remove(out_path)
        tmp_path = out_path + ".part"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass


def _gc_transcode_cache(max_files=40, max_age_days=14):
    """Garde-fou disque : limite le nombre et l'âge des fichiers convertis
    dans data/transcoded/. Supprime d'abord les .part orphelins, puis les
    plus anciens .mp4 au-delà de max_files / max_age_days."""
    try:
        entries = []
        now = time.time()
        max_age = max_age_days * 86400
        for name in os.listdir(config.TRANSCODE_DIR):
            path = os.path.join(config.TRANSCODE_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            # Fichiers .part abandonnés (> 6 h) : conversion interrompue
            if name.endswith(".part"):
                if now - st.st_mtime > 6 * 3600:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                continue
            if not name.endswith(".mp4"):
                continue
            # Trop vieux
            if now - st.st_mtime > max_age:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            entries.append((st.st_mtime, path))
        if len(entries) <= max_files:
            return
        entries.sort()  # plus anciens d'abord
        for _, path in entries[: len(entries) - max_files]:
            try:
                os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


@app.route("/video/<int:video_id>/transcode/start", methods=["POST"])
def transcode_start(video_id):
    # CORRECTIF "Disques actifs" décoché mais vidéo toujours lisible : cette
    # route (déclenchée par le lecteur pour lancer la conversion de secours)
    # ne vérifiait ni "disk_off" ni "missing", contrairement à video_detail()
    # et stream() ci-dessus. Une vidéo dont le disque venait d'être décoché
    # restait donc entièrement accessible tant qu'elle passait par CE chemin
    # (codec à risque / conversion de secours) : le lecteur appelait
    # /transcode/start puis lisait directement /stream/<id>/transcode, qui ne
    # vérifiait rien non plus (voir plus bas). Même garde qu'ailleurs.
    conn = db.get_db()
    video = conn.execute(
        "SELECT path, disk_off, missing FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if not video or video["disk_off"] or video["missing"]:
        return jsonify(status="error", error="Fichier introuvable"), 404

    out_path = _transcode_output_path(video_id)
    if os.path.exists(out_path):
        with _transcode_lock:
            _transcode_jobs[video_id] = {"status": "ready", "error": None}
        return jsonify(status="ready")

    with _transcode_lock:
        job = _transcode_jobs.get(video_id)
        if job and job["status"] == "running":
            return jsonify(status="running")

    if not video["path"] or not os.path.exists(video["path"]):
        return jsonify(status="error", error="Fichier introuvable"), 404

    with _transcode_lock:
        _transcode_jobs[video_id] = {"status": "running", "error": None}
    # PRIORITÉ ABSOLUE : cette conversion est réclamée EN DIRECT par le
    # lecteur pour la vidéo qu'on regarde actuellement — on libère tout de
    # suite le CPU d'une éventuelle conversion d'arrière-plan en cours
    # (voir _kill_background_transcode plus haut), pour ne jamais cumuler
    # deux conversions ffmpeg en même temps.
    _kill_background_transcode()
    threading.Thread(target=_transcode_worker, args=(video_id, video["path"]), daemon=True).start()
    return jsonify(status="running")


@app.route("/video/<int:video_id>/transcode/status")
def transcode_status(video_id):
    # Même garde : si le disque a été décoché pendant que la conversion de
    # secours tournait ou attendait en file, on arrête de la présenter comme
    # "prête" au lecteur — sinon le fichier converti déjà généré restait
    # servable via /stream/<id>/transcode malgré la case décochée.
    conn = db.get_db()
    video = conn.execute(
        "SELECT disk_off, missing FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if not video or video["disk_off"] or video["missing"]:
        return jsonify(status="error", error="Fichier introuvable"), 404
    if os.path.exists(_transcode_output_path(video_id)):
        return jsonify(status="ready")
    with _transcode_lock:
        job = _transcode_jobs.get(video_id)
    if not job:
        return jsonify(status="idle")
    return jsonify(status=job["status"], error=job.get("error"))


# ---------------------------------------------------------------------------
# PRIORITÉ LECTURE : battement envoyé par le lecteur pendant la lecture
# ---------------------------------------------------------------------------
# Tant que ces battements arrivent, le scan suspend ses extractions d'images
# (voir scanner.notify_playback_active) : la lecture vidéo garde le CPU et le
# disque pour elle, l'application et le PC restent parfaitement fluides. Le
# scan reprend automatiquement à pleine vitesse à la pause/fin de lecture, ou
# tout seul quelques secondes après le dernier battement (onglet fermé).
@app.route("/api/playback/heartbeat", methods=["POST"])
def playback_heartbeat():
    payload = request.get_json(silent=True) or {}
    try:
        active = bool(payload.get("active", True))
    except Exception:
        active = True
    if active:
        scanner.notify_playback_active()
    else:
        scanner.notify_playback_stopped()
    # CORRECTIF "Disques actifs" : le lecteur (plein écran OU réduit en
    # mini-lecteur flottant) continuait de lire une vidéo dont le disque
    # venait d'être décoché EN COURS DE LECTURE — les gardes ajoutées sur
    # /video/<id>, /stream/<id> et le chemin de conversion de secours ne
    # protègent qu'un accès qui n'a pas encore commencé ; elles ne peuvent
    # pas couper un flux déjà en train de jouer dans le navigateur. Ce
    # battement, déjà envoyé toutes les 8s pendant toute lecture (voir
    # playback-priority.js) et relancé immédiatement après un changement de
    # "Disques actifs" (voir templates/settings.html), est le seul canal
    # qui touche systématiquement le lecteur actif, y compris quand il est
    # réduit en mini-lecteur sur une tout autre page (ex. Paramètres) : on
    # en profite pour vérifier que la vidéo en cours est toujours autorisée,
    # et prévenir le client sinon.
    video_id = payload.get("video_id")
    try:
        video_id = int(video_id) if video_id is not None else None
    except (TypeError, ValueError):
        video_id = None
    allowed = db.is_video_allowed(video_id) if video_id is not None else True
    return jsonify(ok=True, playback=active, allowed=allowed)


@app.route("/stream/<int:video_id>/transcode")
def stream_transcode(video_id):
    # CORRECTIF "Disques actifs" : dernier verrou avant l'envoi des octets
    # eux-mêmes (comme dans stream() plus haut) — sans lui, un fichier déjà
    # converti restait servable directement par son URL, disque décoché ou
    # non, et un flux de conversion déjà en cours au moment du décochage
    # continuait de fonctionner.
    conn = db.get_db()
    video = conn.execute(
        "SELECT disk_off, missing FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if not video or video["disk_off"] or video["missing"]:
        return "Fichier introuvable", 404
    out_path = _transcode_output_path(video_id)
    if not os.path.exists(out_path):
        return "Conversion pas encore prête", 404
    return _serve_video_file(video_id, out_path, "video/mp4")


# ---------------------------------------------------------------------------
# Pré-transcodage EN AVANCE des vidéos à codec à risque (voir
# scanner.is_risky_codec) — CORRECTIF ARRÊT ~5s / CPU ÉLEVÉ.
# ---------------------------------------------------------------------------
# Avant ce correctif, une vidéo HEVC/10-bit non décodable en matériel était
# servie telle quelle (voir video() ci-dessus, likely_playable basé
# uniquement sur l'extension du fichier) : le lecteur ne basculait vers la
# conversion de secours qu'APRÈS avoir constaté un blocage en lecture, avec
# les délais de prudence de player-smooth.js (jusqu'à 10s) — exactement le
# gel observé. Cette file convertit ces vidéos en tâche de fond, à
# l'avance, pour que la version compatible soit déjà prête au moment où
# l'utilisateur clique lire (voir video(), qui sert alors directement
# /stream/<id>/transcode comme source initiale — plus aucune fenêtre de
# détection à traverser).
_precode_lock = threading.Lock()
_precode_running = False


def _any_stream_active():
    """Vrai si au moins un flux vidéo est en cours de lecture (voir
    _register_stream_handle) : dans ce cas, aucune tâche de fond lourde
    (conversion ffmpeg) ne doit démarrer, elle volerait le CPU au décodage
    de la vidéo qu'on est justement en train de regarder."""
    with _stream_handles_lock:
        return any(_stream_handles.values())


def _background_work_allowed():
    """Vrai seulement si la machine est réellement disponible pour une
    tâche de fond lourde (pré-transcodage) : aucune lecture vidéo en cours,
    aucun scan en cours, ET aucune activité générale récente dans l'appli
    (navigation, filtres, édition de fiche...). Cette dernière condition
    est celle qui manquait : avant, le pré-transcodage ne se mettait en
    pause QUE pendant une lecture vidéo active, pas pendant une utilisation
    normale de l'appli (parcourir la grille, éditer des métadonnées...) —
    il pouvait donc démarrer ou reprendre, et faire remonter le CPU,
    pendant qu'on utilisait encore l'appli sans regarder de vidéo. Voir
    _touch_app_activity / _app_idle_enough dans app.py."""
    # Le pré-transcodage est autorisé uniquement lorsque l'application est
    # réellement laissée de côté. Il reste séquentiel, plafonné et interrompu
    # dès qu'une activité ou une lecture reprend : on anticipe le problème
    # sans créer un nouveau pic CPU pendant l'utilisation normale.
    return (
        not scanner.get_status().get("running")
        and not _any_stream_active()
        and not scanner.is_playback_active()
        and _app_idle_enough()
    )

def _precode_risky_videos():
    """Séquentiel (jamais plusieurs conversions à la fois) et best-effort :
    une erreur sur une vidéo n'interrompt jamais le traitement des
    suivantes. Ne fait jamais planter le scan ni le reste de l'application
    si la bibliothèque est absente/vide."""
    global _precode_running
    with _precode_lock:
        if _precode_running:
            return
        _precode_running = True
    try:
        try:
            scanner.backfill_risky_codecs()
        except Exception:
            pass
        conn = db.get_db()
        rows = conn.execute(
            "SELECT id, path FROM videos WHERE codec_risky = 1 AND missing = 0"
        ).fetchall()
        # CORRECTIF CPU APRÈS SCAN : comme pour les vignettes (voir
        # scanner._build_thumbs_index), on lit UNE SEULE FOIS le contenu du
        # dossier de pré-transcodage (un appel système) plutôt que d'appeler
        # os.path.exists() une fois PAR VIDÉO À CODEC RISQUÉ à chaque
        # déclenchement (après chaque scan, et toutes les 60 s tant qu'il en
        # reste). Sur une bibliothèque comportant beaucoup de HEVC déjà
        # toutes converties, cela évite des centaines/milliers d'appels
        # système répétés pour ne rien trouver de nouveau à faire.
        try:
            _already_transcoded = {
                e.name for e in os.scandir(config.TRANSCODE_DIR) if e.is_file()
            }
        except OSError:
            _already_transcoded = None  # repli : vérification fichier par fichier ci-dessous
        for r in rows:
            # CORRECTIF CPU MAJEUR : ce pré-transcodage encodait, en tâche
            # de fond, TOUTES les vidéos HEVC/10 bits de la bibliothèque —
            # y compris pendant qu'on regardait une vidéo ou pendant un
            # scan. Un encodage ffmpeg sature durablement le processeur :
            # c'était la première cause du PC qui rame. Il ne tourne
            # désormais QUE lorsque la machine est réellement disponible
            # — lecture, scan, ET utilisation générale de l'appli, voir
            # _background_work_allowed — et s'interrompt dès la moindre
            # activité ; il reprendra plus tard tout seul (voir la boucle
            # permanente de start_precode_risky_videos_async ci-dessous).
            if not _background_work_allowed():
                break
            video_id, path = r["id"], r["path"]
            if not path or not os.path.exists(path):
                continue
            out_name = f"{video_id}.mp4"
            already_done = (
                out_name in _already_transcoded if _already_transcoded is not None
                else os.path.exists(_transcode_output_path(video_id))
            )
            if already_done:
                continue
            with _transcode_lock:
                job = _transcode_jobs.get(video_id)
                if job and job["status"] == "running":
                    continue
                _transcode_jobs[video_id] = {"status": "running", "error": None}
            _transcode_worker(video_id, path, is_background=True)  # appel direct, séquentiel : un seul à la fois
    finally:
        with _precode_lock:
            _precode_running = False


def start_precode_risky_videos_async():
    """Point de compatibilité : aucun pré-transcodage automatique.

    La détection des codecs lourds reste active et le fallback compatible est
    généré à la demande. Une limite de threads n'est pas une limite CPU
    fiable pour AV1/HEVC : certains décodeurs créent leurs propres threads et
    peuvent maintenir le processeur à 100 %. Aucun FFmpeg opportuniste ne
    doit donc démarrer après un scan ou quelques minutes d'inactivité.
    """
    return False


def _wait_scan_then_precode():
    """Ancien point d'entrée conservé sans lancer de travail lourd."""
    return False


def _safe_media_filename(filename):
    """Refuse tout path traversal (../, chemins absolus, séparateurs) dans
    les noms de fichiers servis depuis data/ (vignettes, couvertures)."""
    if not filename or not isinstance(filename, str):
        return None
    # Normalise et interdit les composants suspects
    name = filename.replace("\\", "/").lstrip("/")
    if ".." in name.split("/") or name.startswith("/") or ":" in name:
        return None
    base = os.path.basename(name)
    if not base or base in (".", ".."):
        return None
    # Uniquement caractères sûrs (hash uuid + extension typique)
    if not re.match(r"^[\w.\-]+$", base, re.UNICODE):
        return None
    return base


def _cached_media_response(directory, filename, max_age=86400):
    """Sert un fichier image (vignette, couverture) avec cache navigateur.
    max-age long pour ne pas re-télécharger les mêmes vignettes à chaque
    grille ; PAS "immutable" car le nom de fichier (ex. thumb_<hash>.jpg)
    peut rester identique après une régénération — le navigateur doit
    pouvoir revalider via ETag (mtime) et recevoir la nouvelle image."""
    safe = _safe_media_filename(filename)
    if not safe:
        return "Fichier introuvable", 404
    full = os.path.join(directory, safe)
    # Garantit que le chemin résolu reste bien sous directory
    try:
        real_dir = os.path.realpath(directory)
        real_file = os.path.realpath(full)
        if not real_file.startswith(real_dir + os.sep) and real_file != real_dir:
            return "Fichier introuvable", 404
    except OSError:
        return "Fichier introuvable", 404
    if not os.path.isfile(full):
        return "Fichier introuvable", 404
    resp = send_from_directory(directory, safe)
    resp.headers["Cache-Control"] = f"public, max-age={max_age}, must-revalidate"
    try:
        mtime = int(os.path.getmtime(full))
        resp.headers["ETag"] = f'"{mtime}-{safe}"'
        resp.headers["Last-Modified"] = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(mtime)
        )
    except OSError:
        pass
    return resp


@app.route("/thumb/<path:filename>")
def thumb(filename):
    return _cached_media_response(config.THUMBS_DIR, filename)


@app.route("/open_external/<int:video_id>", methods=["POST"])
def open_external(video_id):
    """Ouvre le fichier dans le lecteur par défaut de Windows (VLC, etc.).
    Ne fonctionne que si le serveur tourne sur la même machine que le navigateur.
    N'accepte que des chemins issus de la base (jamais un chemin fourni par le client)."""
    conn = db.get_db()
    video = conn.execute("SELECT path, disk_off, missing FROM videos WHERE id = ?", (video_id,)).fetchone()
    # CORRECTIF "Disques actifs" : ce bouton ouvre le fichier RÉEL avec le
    # lecteur système (VLC/MPV...), en dehors du lecteur intégré — sans
    # cette garde, un disque décoché restait donc accessible par ce biais
    # même quand le lecteur intégré, lui, refusait bien la vidéo.
    if not video or video["disk_off"] or video["missing"] or not video["path"] or not os.path.isfile(video["path"]):
        return jsonify(ok=False, error="Fichier introuvable"), 404
    # Refuse les chemins suspects (pas un fichier régulier)
    try:
        real = os.path.realpath(video["path"])
        if not os.path.isfile(real):
            return jsonify(ok=False, error="Fichier introuvable"), 404
    except OSError:
        return jsonify(ok=False, error="Fichier introuvable"), 404
    try:
        if platform.system() == "Windows":
            os.startfile(video["path"])  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", video["path"]])
        else:
            subprocess.Popen(["xdg-open", video["path"]])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/open_folder/<int:video_id>", methods=["POST"])
def open_folder(video_id):
    """Ouvre l'Explorateur Windows avec le fichier sélectionné.
    Appli Windows uniquement. Chemin uniquement depuis la base."""
    if platform.system() != "Windows":
        return jsonify(ok=False, error="Fonction réservée à Windows."), 400
    conn = db.get_db()
    video = conn.execute("SELECT path, disk_off, missing FROM videos WHERE id = ?", (video_id,)).fetchone()
    # Même garde "Disques actifs" que open_external ci-dessus : décoché, un
    # disque ne doit plus être exposé par aucun bouton, y compris celui qui
    # se contente d'ouvrir l'Explorateur dessus.
    if not video or video["disk_off"] or video["missing"] or not video["path"] or not os.path.isfile(video["path"]):
        return jsonify(ok=False, error="Fichier introuvable"), 404
    try:
        real = os.path.realpath(video["path"])
        if not os.path.isfile(real):
            return jsonify(ok=False, error="Fichier introuvable"), 404
    except OSError:
        return jsonify(ok=False, error="Fichier introuvable"), 404
    try:
        # Une seule ligne de commande, chemin entre guillemets (espaces OK).
        subprocess.Popen(f'explorer /select,"{video["path"]}"')
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# Script PowerShell fixe (aucune interpolation de chaîne dans le script
# lui-même : le dossier de départ passe par une variable d'environnement,
# jamais concaténé dans la commande — élimine tout risque d'injection).
# Écrit dans un fichier .ps1 temporaire puis lancé via "-File" (voir plus
# bas) : plus fiable que le passer par stdin ("-Command -"), qui peut
# échouer silencieusement selon la version de PowerShell.
#
# Fenêtre de sélection : System.Windows.Forms.FolderBrowserDialog, PAS
# le détournement d'OpenFileDialog utilisé dans une version précédente.
# Ce détournement (CheckFileExists=$false + filtre vide) donnait bien la
# fenêtre "Ouvrir" façon Explorateur, mais avec un défaut bloquant :
# double-cliquer un dossier NAVIGUE dedans (comportement normal d'un
# dialogue "Ouvrir un fichier"), il n'y a donc aucun moyen de s'arrêter
# et de choisir un dossier précis — on ne peut que descendre à l'infini
# dans l'arborescence. FolderBrowserDialog est le seul composant conçu
# pour l'usage inverse : un simple clic met un dossier en surbrillance,
# et le bouton "Sélectionner un dossier" le valide immédiatement, sans
# ambiguïté. Avec AutoUpgradeEnabled=$true (véritable ci-dessous, c'est
# de toute façon la valeur par défaut depuis .NET Framework 3.5 SP1),
# Windows Vista et plus affichent la version modernisée de cette fenêtre
# (barre d'adresse en fil d'Ariane, zone de recherche, redimensionnable),
# nettement plus proche de l'Explorateur que l'ancien style "arbre".
#
# Fenêtre "porteuse" invisible mais JAMAIS minimisée (taille ~0, hors
# écran, opacité 0) : sert uniquement à passer le dialogue en premier
# plan (TopMost) au-dessus du navigateur. Une porteuse minimisée peut
# empêcher la fenêtre qu'elle possède de s'afficher du tout — c'était la
# cause du bug "aucune fenêtre ne s'ouvre".
#
# Marqueurs de sortie (MEDIATHEQUE_PATH: / MEDIATHEQUE_CANCELLED /
# MEDIATHEQUE_ERROR:) : permettent à Python de distinguer une vraie
# annulation utilisateur d'un échec PowerShell, pour ne plus jamais
# rester silencieux en cas de problème.
# Implémentation IFileOpenDialog (COM natif de Windows, interface
# Shell "Vista+") plutôt que System.Windows.Forms.FolderBrowserDialog :
# c'est le composant que Windows utilise lui-même pour ses propres
# fenêtres modernes (barre d'adresse en fil d'Ariane, barre de
# recherche, panneau "Accès rapide" / "Ce PC", redimensionnable). Ça
# évite aussi toute dépendance à des propriétés WinForms comme
# UseDescriptionForTitle / AutoUpgradeEnabled, absentes sur certaines
# installations PowerShell/.NET et qui provoquaient les erreurs
# précédentes ("La propriété (...) est introuvable").
# Filet de sécurité : si cette API COM venait à échouer pour une
# raison quelconque, on retombe automatiquement sur l'ancien
# FolderBrowserDialog (identique à la version précédente, propriétés
# optionnelles protégées) plutôt que de laisser la fenêtre ne pas
# s'ouvrir du tout.
_BROWSE_FOLDER_PS1 = r"""
try {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    $start = $env:MEDIATHEQUE_BROWSE_START
    $chosen = $null
    $cancelled = $false
    $usedModern = $false

    try {
        Add-Type -ReferencedAssemblies 'System.Windows.Forms' -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace MediathequeBrowse {

    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IFileDialog {
        [PreserveSig] int Show(IntPtr parent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, uint fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IShellItem {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, out IntPtr ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    public class FileOpenDialogRCW { }

    public static class ModernFolderPicker {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHCreateItemFromParsingName(
            [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
            IntPtr pbc,
            ref Guid riid,
            [MarshalAs(UnmanagedType.Interface)] out IShellItem ppv);

        public static string Pick(string title, string startFolder, IntPtr owner) {
            const uint FOS_PICKFOLDERS = 0x20;
            const uint FOS_FORCEFILESYSTEM = 0x40;
            const uint FOS_PATHMUSTEXIST = 0x800;
            const uint SIGDN_FILESYSPATH = 0x80058000;

            IFileDialog dialog = (IFileDialog)(new FileOpenDialogRCW());
            dialog.SetOptions(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
            dialog.SetTitle(title);

            if (!string.IsNullOrEmpty(startFolder) && System.IO.Directory.Exists(startFolder)) {
                try {
                    IShellItem startItem;
                    Guid shellItemGuid = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
                    SHCreateItemFromParsingName(startFolder, IntPtr.Zero, ref shellItemGuid, out startItem);
                    dialog.SetFolder(startItem);
                } catch { }
            }

            int hr = dialog.Show(owner);
            if (hr != 0) {
                return null; // annulation utilisateur (ou fermeture) : pas une erreur
            }

            IShellItem result;
            dialog.GetResult(out result);
            IntPtr pszPath;
            result.GetDisplayName(SIGDN_FILESYSPATH, out pszPath);
            string path = Marshal.PtrToStringUni(pszPath);
            Marshal.FreeCoTaskMem(pszPath);
            return path;
        }
    }
}
'@ -ErrorAction Stop | Out-Null

        $modernOwner = New-Object System.Windows.Forms.Form
        $modernOwner.FormBorderStyle = 'None'
        $modernOwner.ShowInTaskbar = $false
        $modernOwner.StartPosition = 'Manual'
        $modernOwner.Location = New-Object System.Drawing.Point(-2000, -2000)
        $modernOwner.Size = New-Object System.Drawing.Size(1, 1)
        $modernOwner.Opacity = 0
        $modernOwner.TopMost = $true
        $modernOwner.Show()
        $modernOwner.Activate()

        $picked = [MediathequeBrowse.ModernFolderPicker]::Pick(
            "Choisir un dossier de la bibliotheque",
            $start,
            $modernOwner.Handle
        )
        $modernOwner.Close()

        $usedModern = $true
        if ($picked -and (Test-Path -LiteralPath $picked -PathType Container)) {
            $chosen = $picked
        } else {
            $cancelled = $true
        }
    } catch {
        # La fenêtre moderne (IFileOpenDialog) n'a pas pu s'initialiser sur
        # ce poste : on retombe sur l'ancien sélecteur, plutôt que de ne
        # rien afficher du tout.
        $usedModern = $false
    }

    if (-not $usedModern) {
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = "Choisir un dossier de la bibliotheque"
        try { $dlg.UseDescriptionForTitle = $true } catch { }
        try { $dlg.AutoUpgradeEnabled = $true } catch { }
        $dlg.ShowNewFolderButton = $true

        if ($start -and (Test-Path -LiteralPath $start -PathType Container)) {
            $dlg.SelectedPath = $start
        }

        $owner = New-Object System.Windows.Forms.Form
        $owner.FormBorderStyle = 'None'
        $owner.ShowInTaskbar = $false
        $owner.StartPosition = 'Manual'
        $owner.Location = New-Object System.Drawing.Point(-2000, -2000)
        $owner.Size = New-Object System.Drawing.Size(1, 1)
        $owner.Opacity = 0
        $owner.TopMost = $true
        $owner.Show()
        $owner.Activate()

        $result = $dlg.ShowDialog($owner)
        $owner.Close()

        if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $dlg.SelectedPath -and (Test-Path -LiteralPath $dlg.SelectedPath -PathType Container)) {
            $chosen = $dlg.SelectedPath
        } else {
            $cancelled = $true
        }
    }

    if ($chosen) {
        Write-Output ("MEDIATHEQUE_PATH:" + $chosen)
    } else {
        Write-Output "MEDIATHEQUE_CANCELLED"
    }
} catch {
    Write-Output ("MEDIATHEQUE_ERROR:" + $_.Exception.Message)
    exit 1
}
"""


@app.route("/browse_folder", methods=["POST"])
def browse_folder():
    """Ouvre le sélecteur de dossier natif de Windows (fenêtre Windows
    directement, pas un widget web) pour choisir un dossier de la
    bibliothèque, et renvoie le chemin choisi. Bloque le temps que
    l'utilisateur choisisse (le serveur tourne en mode 'threaded', les
    autres requêtes ne sont donc pas affectées). Windows uniquement :
    sur les autres systèmes, l'utilisateur retombe simplement sur le
    champ texte déjà présent."""
    if platform.system() != "Windows":
        return jsonify(ok=False, error="La fenêtre de sélection Windows n'est disponible que sous Windows. Utilise le champ texte ci-dessus."), 400

    settings = config.load_settings() if hasattr(config, "load_settings") else {}

    # Mémoire du dernier emplacement : priorité au contenu déjà tapé dans le
    # champ texte (l'utilisateur est peut-être en train de corriger un
    # chemin précis) ; sinon, on rouvre là où la dernière sélection s'est
    # terminée (persisté dans data/settings.json, donc ça survit aussi à un
    # redémarrage de l'appli — pas seulement le temps de la session).
    start = (request.form.get("start") or "").strip()
    if not start:
        start = (settings.get("last_browse_folder") or "").strip()
    env = os.environ.copy()
    env["MEDIATHEQUE_BROWSE_START"] = start

    tmp_path = None
    try:
        # Fichier .ps1 réel plutôt qu'un script passé par stdin (voir
        # commentaire ci-dessus). "-ExecutionPolicy Bypass" s'applique
        # uniquement à ce lancement précis (ne change aucun réglage
        # système) et évite un blocage si la stratégie d'exécution du
        # poste est "Restricted" — cas courant hors machine de dev, qui
        # empêcherait la fenêtre de s'ouvrir sans le moindre message.
        fd, tmp_path = tempfile.mkstemp(suffix=".ps1", prefix="mediatheque_browse_")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(_BROWSE_FOLDER_PS1)

        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Sta", "-File", tmp_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="Délai dépassé (5 minutes) sans sélection."), 504
    except FileNotFoundError:
        return jsonify(ok=False, error="PowerShell introuvable sur ce système."), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("MEDIATHEQUE_PATH:"):
            chosen = line[len("MEDIATHEQUE_PATH:"):]
            # Mémorise l'emplacement pour la prochaine ouverture de la
            # fenêtre (voir plus haut). Best-effort : si l'écriture échoue
            # pour une raison quelconque, on renvoie quand même le chemin
            # choisi à l'utilisateur, qui n'a pas à en pâtir.
            #
            # CORRECTIF "Disques actifs se recoche tout seul" : cette
            # fenêtre Windows peut rester ouverte plusieurs minutes (jusqu'à
            # 5, voir timeout ci-dessus) pendant que l'utilisateur choisit
            # un dossier. `settings` a été chargé tout au début de la
            # requête, AVANT cette attente. Si, entre-temps, un autre
            # réglage a changé ailleurs (ex. décochage d'un disque dans
            # "Disques actifs"), réutiliser cette copie périmée pour
            # l'écraser complètement effacerait silencieusement ce
            # changement au moment où la fenêtre se referme enfin. On
            # recharge donc les réglages À CET INSTANT précis (juste avant
            # d'écrire), pour ne modifier que le champ concerné
            # (last_browse_folder) sans jamais écraser un changement fait
            # pendant l'attente.
            try:
                fresh_settings = config.load_settings() if hasattr(config, "load_settings") else dict(settings)
                fresh_settings["last_browse_folder"] = chosen
                config.save_settings(fresh_settings)
            except Exception:
                pass
            return jsonify(ok=True, path=chosen)
        if line == "MEDIATHEQUE_CANCELLED":
            return jsonify(ok=True, cancelled=True)
        if line.startswith("MEDIATHEQUE_ERROR:"):
            return jsonify(ok=False, error=line[len("MEDIATHEQUE_ERROR:"):][:300]), 500

    # Aucun marqueur reconnu : le script n'a probablement jamais tourné
    # jusqu'au bout (échec PowerShell avant même d'atteindre le bloc
    # try). On remonte tout ce qui est disponible pour permettre un vrai
    # diagnostic, au lieu de laisser croire silencieusement à une simple
    # annulation.
    detail = (proc.stderr or proc.stdout or "").strip()
    if not detail:
        detail = f"code retour {proc.returncode}, aucune sortie."
    return jsonify(ok=False, error="La fenêtre Windows ne s'est pas ouverte : " + detail[:300]), 500


# ---------- Catégories / tags / playlists ----------

@app.route("/category/new", methods=["POST"])
def category_new():
    is_ajax = bool(request.form.get("ajax"))
    name = request.form.get("name", "").strip()
    new_id, error = db.create_category(name)
    if is_ajax:
        # Créée depuis le panneau latéral gauche pendant qu'une vidéo est en
        # lecture : on répond en JSON pour insérer la nouvelle catégorie
        # dans la liste sans jamais recharger la page (ce qui couperait la
        # lecture en cours et la ferait reprendre depuis le début).
        if error or not new_id:
            return jsonify(ok=False, error=error or "Erreur inconnue."), 400
        return jsonify(ok=True, id=new_id, name=name)
    flash(error if error else "Catégorie créée.")
    return redirect(request.referrer or url_for("organize_view"))


@app.route("/category/<int:category_id>/full_edit", methods=["POST"])
def category_full_edit(category_id):
    is_ajax = bool(request.form.get("ajax"))
    name = request.form.get("name", "").strip()
    # Upload image seul (sans formulaire complet) : conserver le nom / note /
    # commentaire actuels si non fournis (comme pour les tags).
    current = db.get_category(category_id)
    if not current:
        if is_ajax:
            return jsonify(ok=False, error="Catégorie introuvable."), 404
        flash("Catégorie introuvable.")
        return redirect(url_for("organize_view"))
    if not name:
        name = current["name"] or ""
    if "rating" in request.form:
        rating = request.form.get("rating", type=int) or 0
        rating = max(0, min(5, rating))
    else:
        rating = current["rating"] or 0
    if "comment" in request.form:
        comment = request.form.get("comment", "").strip()
    else:
        comment = current["comment"] or ""
    images = {}
    upload_rejected = False
    for key in ("image1", "image2", "image3"):
        f = request.files.get(key)
        if f and f.filename:
            saved = _save_image_upload(f, config.CATEGORY_IMAGES_DIR)
            if saved:
                images[key] = saved
            else:
                upload_rejected = True
    if upload_rejected and not images:
        msg = (
            "Image refusée : formats acceptés JPG/PNG/WEBP/GIF, max 8 Mo, "
            "fichier réellement image (pas seulement l'extension)."
        )
        if is_ajax:
            return jsonify(ok=False, error=msg), 400
        flash(msg)
        return redirect(request.referrer or url_for("organize_view"))
    ok, error = db.update_category_full(category_id, name, rating, comment, images)
    if is_ajax:
        if not ok:
            return jsonify(ok=False, error=error), 400
        fresh = db.get_category(category_id)
        return jsonify(
            ok=True, name=fresh["name"], rating=fresh["rating"] or 0,
            comment=fresh["comment"] or "",
            image_url=url_for("category_image", filename=fresh["image1"]) if fresh["image1"] else None,
        )
    flash(error if not ok else "Catégorie mise à jour.")
    return redirect(request.referrer or url_for("organize_view"))


@app.route("/category_image/<path:filename>")
def category_image(filename):
    return _cached_media_response(config.CATEGORY_IMAGES_DIR, filename)


@app.route("/category/<int:category_id>")
def category_view(category_id):
    """Page dédiée à une catégorie, façon YouTube : bannière, note, et tous
    les filtres disponibles sur les vidéos qu'elle contient."""
    category = db.get_category(category_id)
    if not category:
        return "Catégorie introuvable", 404

    fallback = resolve_category_filters()
    f = read_common_filters(fallback=fallback)
    sort = f["sort"] or "recent"
    # Cf. index() : "subplaylist" (sous-playlist) est prioritaire sur
    # "playlist" (playlist principale) si les deux sont renseignés.
    # Comme pour "category" sur la page playlist (cf. playlist_view), ces
    # deux champs ne font pas partie de read_common_filters() : on
    # réapplique donc ici le même repli manuel sur les derniers filtres
    # mémorisés lorsqu'aucun des deux n'est présent dans l'URL.
    playlist_top_id = request.args.get("playlist", type=int)
    if playlist_top_id is None and "playlist" not in request.args and fallback.get("playlist"):
        try:
            playlist_top_id = int(fallback["playlist"])
        except (TypeError, ValueError):
            playlist_top_id = None
    playlist_sub_id = request.args.get("subplaylist", type=int)
    if playlist_sub_id is None and "subplaylist" not in request.args and fallback.get("subplaylist"):
        try:
            playlist_sub_id = int(fallback["subplaylist"])
        except (TypeError, ValueError):
            playlist_sub_id = None
    playlist_id = playlist_sub_id or playlist_top_id

    _sid, _skey, _sdir = _seek_from_request(sort)
    videos, total, total_pages, page, seek_prev, seek_next = query_videos(
        q=f["q"], category_id=category_id, tag_id=f["tag_id"], playlist_id=playlist_id, sort=sort,
        duration_min=f["duration_min"], duration_max=f["duration_max"],
        size_min=f["size_min"], size_max=f["size_max"],
        resolution=f["resolution"], rating_min=f["rating_min"],
        date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"], page=f["page"],
        page_size=18,
        seek_id=_sid, seek_key=_skey, seek_dir=_sdir,
    )
    categories, playlists, popular_tags = get_sidebar_data()
    categories_flat = get_flat_categories()
    playlists_flat = get_flat_playlists()
    all_tags = db.get_db().execute("SELECT id, name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    facets = get_filter_facets(dict(
        q=f["q"], category_id=category_id, tag_id=f["tag_id"], playlist_id=playlist_id,
        duration_min=f["duration_min"], duration_max=f["duration_max"], size_min=f["size_min"], size_max=f["size_max"], resolution=f["resolution"],
        rating_min=f["rating_min"], date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"],
    ))
    playlist_root_id = resolve_playlist_root_id(playlist_top_id, playlist_sub_id, playlists_flat)
    search_matches = get_search_group_matches(f["q"])

    return render_template(
        "category.html", category=category, videos=videos, total=total,
        total_pages=total_pages, page=page, categories=categories, playlists=playlists,
        categories_flat=categories_flat, playlists_flat=playlists_flat,
        popular_tags=popular_tags, all_tags=all_tags,
        q=f["q"], tag_id=f["tag_id"], playlist_id=playlist_id, sort=sort,
        duration_min=f["duration_min"], duration_max=f["duration_max"], size_min=f["size_min"], size_max=f["size_max"], resolution=f["resolution"],
        rating_min=f["rating_min"], date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"], facets=facets, playlist_root_id=playlist_root_id,
        is_category_page=True, category_ctx_id=category_id, search_matches=search_matches,
        seek_prev=seek_prev, seek_next=seek_next,
    )


@app.route("/category/<int:category_id>/play")
def category_play(category_id):
    """Lecture directe depuis la case catégorie (cercle "play") : ouvre tout
    de suite la première vidéo de la catégorie, dans le même ordre que celui
    utilisé pour le contexte catégorie de video_detail (added_at ASC)."""
    category = db.get_category(category_id)
    if not category:
        return "Catégorie introuvable", 404
    conn = db.get_db()
    first = conn.execute(
        """SELECT id FROM videos WHERE category_id = ? AND missing = 0 AND disk_off = 0
           ORDER BY added_at ASC, id ASC LIMIT 1""",
        (category_id,),
    ).fetchone()
    if not first:
        return redirect(url_for("category_view", category_id=category_id))
    return redirect(url_for("video_detail", video_id=first["id"], category=category_id))


@app.route("/category/<int:category_id>/rename", methods=["POST"])
def category_rename(category_id):
    is_ajax = bool(request.form.get("ajax"))
    new_name = request.form.get("name", "")
    ok, error = db.rename_category(category_id, new_name)
    if is_ajax:
        # Renommée depuis le panneau latéral gauche (potentiellement pendant
        # la lecture d'une vidéo) : JSON, jamais de redirection/rechargement.
        if not ok:
            return jsonify(ok=False, error=error or "Erreur inconnue."), 400
        return jsonify(ok=True, id=category_id, name=new_name.strip())
    flash(error if not ok else "Catégorie renommée.")
    return redirect(request.referrer or url_for("index"))


@app.route("/category/<int:category_id>/delete", methods=["POST"])
def category_delete(category_id):
    is_ajax = bool(request.form.get("ajax"))
    db.delete_category(category_id)
    own_page = url_for("category_view", category_id=category_id)
    if is_ajax:
        # Supprimée depuis le panneau latéral gauche : JSON, jamais de
        # rechargement. On indique seulement si la page actuelle était la
        # page dédiée de cette catégorie (auquel cas le client doit
        # naviguer, la page affichée n'existant plus).
        referrer = request.referrer or ""
        return jsonify(ok=True, own_page=own_page in referrer)
    flash("Catégorie supprimée. Les vidéos concernées n'ont pas été touchées.")
    referrer = request.referrer or ""
    if own_page in referrer:
        return redirect(url_for("index"))
    return redirect(referrer or url_for("index"))


@app.route("/tag/<int:tag_id>")
def tag_view(tag_id):
    """Page dédiée à un tag, façon "vue des tags" : grande image de
    couverture (méthode letterbox, jamais rognée) + toutes les vidéos
    portant ce tag, avec les mêmes filtres que les pages catégorie/playlist."""
    tag = db.get_tag(tag_id)
    if not tag:
        return "Tag introuvable", 404

    fallback = resolve_tag_filters()
    f = read_common_filters(fallback=fallback)
    sort = f["sort"] or "recent"
    category_id = request.args.get("category", type=int)
    playlist_top_id = request.args.get("playlist", type=int)
    playlist_sub_id = request.args.get("subplaylist", type=int)
    playlist_id = playlist_sub_id or playlist_top_id

    _sid, _skey, _sdir = _seek_from_request(sort)
    videos, total, total_pages, page, seek_prev, seek_next = query_videos(
        q=f["q"], category_id=category_id, tag_id=tag_id, playlist_id=playlist_id, sort=sort,
        duration_min=f["duration_min"], duration_max=f["duration_max"],
        size_min=f["size_min"], size_max=f["size_max"],
        resolution=f["resolution"], rating_min=f["rating_min"],
        date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"], page=f["page"],
        page_size=18,
        seek_id=_sid, seek_key=_skey, seek_dir=_sdir,
    )

    categories, playlists, popular_tags = get_sidebar_data()
    categories_flat = get_flat_categories()
    playlists_flat = get_flat_playlists()
    all_tags = db.get_db().execute("SELECT id, name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    facets = get_filter_facets(dict(
        q=f["q"], category_id=category_id, tag_id=tag_id, playlist_id=playlist_id,
        duration_min=f["duration_min"], duration_max=f["duration_max"], size_min=f["size_min"], size_max=f["size_max"], resolution=f["resolution"],
        rating_min=f["rating_min"], date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"],
    ))
    playlist_root_id = resolve_playlist_root_id(playlist_top_id, playlist_sub_id, playlists_flat)
    search_matches = get_search_group_matches(f["q"])

    return render_template(
        "tag.html", tag=tag, videos=videos, total=total,
        total_pages=total_pages, page=page, categories=categories, playlists=playlists,
        categories_flat=categories_flat, playlists_flat=playlists_flat,
        popular_tags=popular_tags, all_tags=all_tags,
        q=f["q"], category_id=category_id, playlist_id=playlist_id, sort=sort,
        duration_min=f["duration_min"], duration_max=f["duration_max"], size_min=f["size_min"], size_max=f["size_max"], resolution=f["resolution"],
        rating_min=f["rating_min"], date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"], facets=facets, playlist_root_id=playlist_root_id,
        search_matches=search_matches,
        seek_prev=seek_prev, seek_next=seek_next,
        # Contexte de navigation pour la lecture d'une vidéo depuis cette
        # vue (voir is_playlist_page/is_category_page dans _video_grid.html
        # et le bloc tag_ctx dans video_detail) : "Vidéo suivante" au lieu
        # de "À découvrir aussi", et suggestions limitées aux vidéos de ce
        # tag.
        is_tag_page=True, tag_ctx_id=tag_id,
    )


@app.route("/tag/<int:tag_id>/full_edit", methods=["POST"])
def tag_full_edit(tag_id):
    """Met à jour l'image (tag "image") d'un tag, et éventuellement son nom
    en même temps, depuis l'interface de gestion des tags ou la page dédiée."""
    is_ajax = bool(request.form.get("ajax"))
    new_name = request.form.get("name", "").strip()
    ok, error = True, None
    if new_name:
        ok, error = db.rename_tag(tag_id, new_name)
    images = {}
    upload_rejected = False
    for key in ("image1", "image2", "image3"):
        f = request.files.get(key)
        if f and f.filename:
            saved = _save_image_upload(f, config.TAG_IMAGES_DIR)
            if saved:
                images[key] = saved
            else:
                upload_rejected = True
    if upload_rejected and not images:
        msg = (
            "Image refusée : formats acceptés JPG/PNG/WEBP/GIF, max 8 Mo, "
            "fichier réellement image."
        )
        if is_ajax:
            return jsonify(ok=False, error=msg), 400
        flash(msg)
        return redirect(request.referrer or url_for("index"))
    if ok and images:
        db.update_tag_images(tag_id, images)
    if is_ajax:
        if not ok:
            return jsonify(ok=False, error=error), 400
        fresh = db.get_tag(tag_id)
        return jsonify(
            ok=True, id=tag_id, name=fresh["name"],
            image_url=url_for("tag_image", filename=fresh["image1"]) if fresh["image1"] else None,
        )
    flash(error if not ok else "Tag mis à jour.")
    return redirect(request.referrer or url_for("index"))


@app.route("/tag_image/<path:filename>")
def tag_image(filename):
    return _cached_media_response(config.TAG_IMAGES_DIR, filename)


@app.route("/tag/<int:tag_id>/rename", methods=["POST"])
def tag_rename(tag_id):
    is_ajax = bool(request.form.get("ajax"))
    new_name = request.form.get("name", "")
    ok, error = db.rename_tag(tag_id, new_name)
    if is_ajax:
        if not ok:
            return jsonify(ok=False, error=error or "Erreur inconnue."), 400
        return jsonify(ok=True, id=tag_id, name=new_name.strip().lower())
    flash(error if not ok else "Tag renommé.")
    return redirect(request.referrer or url_for("index"))


@app.route("/tag/<int:tag_id>/delete", methods=["POST"])
def tag_delete(tag_id):
    is_ajax = bool(request.form.get("ajax"))
    db.delete_tag(tag_id)
    if is_ajax:
        return jsonify(ok=True, own_page=False)
    flash("Tag supprimé.")
    return redirect(request.referrer or url_for("index"))


@app.route("/playlist/<int:playlist_id>")
def playlist_view(playlist_id):
    """Page dédiée à une playlist, façon YouTube : bannière, note, et tous
    les filtres disponibles sur les vidéos qu'elle contient."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return "Playlist introuvable", 404

    fallback = resolve_playlist_filters()
    f = read_common_filters(fallback=fallback)
    sort = f["sort"] or "position"
    category_id = request.args.get("category", type=int)
    if category_id is None and "category" not in request.args and fallback.get("category"):
        try:
            category_id = int(fallback["category"])
        except (TypeError, ValueError):
            category_id = None

    # Sous-playlist ou playlist principale : les deux utilisent désormais
    # la même grille compacte à 18 vidéos par page (voir index() /
    # category_view() / tag_view() : même pagination partout).
    breadcrumb = db.get_playlist_breadcrumb(playlist_id)

    _sid, _skey, _sdir = _seek_from_request(sort)
    videos, total, total_pages, page, seek_prev, seek_next = query_videos(
        q=f["q"], category_id=category_id, tag_id=f["tag_id"], playlist_id=playlist_id, sort=sort,
        duration_min=f["duration_min"], duration_max=f["duration_max"],
        size_min=f["size_min"], size_max=f["size_max"],
        resolution=f["resolution"], rating_min=f["rating_min"],
        date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"], page=f["page"],
        page_size=18,
        seek_id=_sid, seek_key=_skey, seek_dir=_sdir,
    )

    categories, playlists, popular_tags = get_sidebar_data()
    categories_flat = get_flat_categories()
    playlists_flat = get_flat_playlists()
    all_tags = db.get_db().execute("SELECT id, name FROM tags ORDER BY name COLLATE NOCASE").fetchall()

    sub_playlists = db.get_playlist_children(playlist_id)
    # Playlists proposables comme "playlist parente" dans le formulaire
    # d'édition : on exclut la playlist elle-même et ses propres
    # sous-playlists, pour ne pas pouvoir créer de boucle.
    descendant_ids = {c["id"] for c in sub_playlists}
    descendant_ids.add(playlist_id)
    parent_options = get_flat_playlists(exclude_ids=descendant_ids)
    # Options pour le champ "Playlist parente" des cartes de SOUS-playlists
    # affichées sur cette page : contrairement à parent_options ci-dessus
    # (pour la playlist courante elle-même), on n'exclut PAS playlist_id,
    # car il s'agit justement de leur parente actuelle légitime — l'exclure
    # ferait retomber leur select sur "— Aucune —" par défaut et basculerait
    # la sous-playlist en playlist principale au premier enregistrement.
    sub_parent_options = get_flat_playlists(exclude_ids=descendant_ids - {playlist_id})
    facets = get_filter_facets(dict(
        q=f["q"], category_id=category_id, tag_id=f["tag_id"], playlist_id=playlist_id,
        duration_min=f["duration_min"], duration_max=f["duration_max"], size_min=f["size_min"], size_max=f["size_max"], resolution=f["resolution"],
        rating_min=f["rating_min"], date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"],
    ))
    search_matches = get_search_group_matches(f["q"])

    return render_template(
        "playlist.html", playlist=playlist, videos=videos, total=total,
        total_pages=total_pages, page=page, categories=categories, playlists=playlists,
        categories_flat=categories_flat, playlists_flat=playlists_flat,
        popular_tags=popular_tags, all_tags=all_tags,
        sub_playlists=sub_playlists, breadcrumb=breadcrumb, parent_options=parent_options,
        sub_parent_options=sub_parent_options,
        q=f["q"], tag_id=f["tag_id"], category_id=category_id, playlist_id=playlist_id,
        is_playlist_page=True, sort=sort,
        duration_min=f["duration_min"], duration_max=f["duration_max"], size_min=f["size_min"], size_max=f["size_max"], resolution=f["resolution"],
        rating_min=f["rating_min"], date_from=f["date_from"], date_to=f["date_to"],
        has_comment=f["has_comment"], favorite_only=f["favorite_only"], facets=facets, search_matches=search_matches,
        seek_prev=seek_prev, seek_next=seek_next,
    )


@app.route("/playlist/<int:playlist_id>/play")
def playlist_play(playlist_id):
    """Lecture directe depuis la case playlist/sous-playlist (cercle "play") :
    ouvre tout de suite la première vidéo de la playlist, dans le même ordre
    que celui utilisé pour le contexte playlist de video_detail (pi.position)."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return "Playlist introuvable", 404
    conn = db.get_db()
    first = conn.execute(
        """SELECT v.id FROM videos v
           JOIN playlist_items pi ON pi.video_id = v.id
           WHERE pi.playlist_id = ? AND v.missing = 0 AND v.disk_off = 0
           ORDER BY pi.position ASC LIMIT 1""",
        (playlist_id,),
    ).fetchone()
    if not first:
        return redirect(url_for("playlist_view", playlist_id=playlist_id))
    return redirect(url_for("video_detail", video_id=first["id"], playlist=playlist_id))


@app.route("/playlist/new", methods=["POST"])
def playlist_new():
    is_ajax = bool(request.form.get("ajax"))
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", type=int)
    manual_creation_date = request.form.get("manual_creation_date", "").strip()
    new_id, error = (None, None)
    if name:
        new_id, error = db.create_playlist(name, parent_id=parent_id, manual_creation_date=manual_creation_date)
        if error:
            flash(error)
    elif is_ajax:
        error = "Le nom ne peut pas être vide."
    if is_ajax:
        # Créée depuis le panneau latéral gauche pendant qu'une vidéo est en
        # lecture : JSON, jamais de rechargement de la page.
        if error or not new_id:
            return jsonify(ok=False, error=error or "Erreur inconnue."), 400
        return jsonify(ok=True, id=new_id, name=name)
    return redirect(request.referrer or url_for("index"))


@app.route("/playlist/<int:playlist_id>/new_sub", methods=["POST"])
def playlist_new_sub(playlist_id):
    """Crée une sous-playlist directement rattachée à cette playlist, depuis
    le formulaire dédié affiché sur sa page."""
    name = request.form.get("name", "").strip()
    manual_creation_date = request.form.get("manual_creation_date", "").strip()
    if name:
        new_id, error = db.create_playlist(name, parent_id=playlist_id, manual_creation_date=manual_creation_date)
        if error:
            flash(error)
    else:
        flash("Le nom ne peut pas être vide.")
    return redirect(request.referrer or url_for("playlist_view", playlist_id=playlist_id))


@app.route("/playlist/<int:playlist_id>/full_edit", methods=["POST"])
def playlist_full_edit(playlist_id):
    is_ajax = bool(request.form.get("ajax"))
    before = db.get_playlist(playlist_id)
    if not before:
        if is_ajax:
            return jsonify(ok=False, error="Playlist introuvable."), 404
        flash("Playlist introuvable.")
        return redirect(url_for("organize_view"))
    name = request.form.get("name", "").strip()
    if not name:
        name = before["name"] or ""
    if "rating" in request.form:
        rating = request.form.get("rating", type=int) or 0
        rating = max(0, min(5, rating))
    else:
        rating = before["rating"] or 0
    if "comment" in request.form:
        comment = request.form.get("comment", "").strip()
    else:
        comment = before["comment"] or ""
    images = {}
    upload_rejected = False
    for key in ("image1", "image2", "image3"):
        f = request.files.get(key)
        if f and f.filename:
            saved = _save_image_upload(f, config.PLAYLIST_IMAGES_DIR)
            if saved:
                images[key] = saved
            else:
                upload_rejected = True
    if upload_rejected and not images:
        msg = (
            "Image refusée : formats acceptés JPG/PNG/WEBP/GIF, max 8 Mo, "
            "fichier réellement image (pas seulement l'extension)."
        )
        if is_ajax:
            return jsonify(ok=False, error=msg), 400
        flash(msg)
        return redirect(request.referrer or url_for("organize_view"))
    # Le champ "parent_id" n'est présent que sur les formulaires qui exposent
    # le choix de la playlist parente (page playlist dédiée) ; sur la page
    # Organiser, la parenté n'est pas modifiée via ce formulaire.
    change_parent = "parent_id" in request.form
    parent_id = request.form.get("parent_id", type=int) if change_parent else None
    # Le champ "manual_creation_date" n'est présent que sur les formulaires
    # d'édition d'une sous-playlist (il n'existe pas pour une playlist
    # principale) : s'il est absent, on ne touche pas à la valeur en base.
    manual_creation_date = request.form.get("manual_creation_date")
    if manual_creation_date is not None:
        manual_creation_date = manual_creation_date.strip()
    ok, error = db.update_playlist_full(
        playlist_id, name, rating, comment, images,
        parent_id=parent_id, change_parent=change_parent,
        manual_creation_date=manual_creation_date,
    )
    if is_ajax:
        if not ok:
            return jsonify(ok=False, error=error), 400
        fresh = db.get_playlist(playlist_id)
        parent_moved = change_parent and before and fresh and before["parent_id"] != fresh["parent_id"]
        return jsonify(
            ok=True, name=fresh["name"], rating=fresh["rating"] or 0,
            comment=fresh["comment"] or "",
            image_url=url_for("playlist_image", filename=fresh["image1"]) if fresh["image1"] else None,
            parent_changed=parent_moved,
            manual_creation_date=fresh["manual_creation_date"] or "",
            manual_creation_date_display=format_date_fr(fresh["manual_creation_date"]) if fresh["manual_creation_date"] else "",
        )
    flash(error if not ok else "Playlist mise à jour.")
    return redirect(request.referrer or url_for("organize_view"))


@app.route("/playlist_image/<path:filename>")
def playlist_image(filename):
    return _cached_media_response(config.PLAYLIST_IMAGES_DIR, filename)


@app.route("/playlist/<int:playlist_id>/add/<int:video_id>", methods=["POST"])
def playlist_add(playlist_id, video_id):
    conn = db.get_db()
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 as n FROM playlist_items WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO playlist_items(playlist_id, video_id, position) VALUES (?, ?, ?)",
        (playlist_id, video_id, row["n"]),
    )
    conn.commit()
    return redirect(request.referrer or url_for("index"))


@app.route("/playlist/<int:playlist_id>/remove/<int:video_id>", methods=["POST"])
def playlist_remove(playlist_id, video_id):
    conn = db.get_db()
    conn.execute(
        "DELETE FROM playlist_items WHERE playlist_id = ? AND video_id = ?",
        (playlist_id, video_id),
    )
    conn.commit()
    return redirect(request.referrer or url_for("playlist_view", playlist_id=playlist_id))


@app.route("/playlist/<int:playlist_id>/rename", methods=["POST"])
def playlist_rename(playlist_id):
    is_ajax = bool(request.form.get("ajax"))
    new_name = request.form.get("name", "")
    ok, error = db.rename_playlist(playlist_id, new_name)
    if is_ajax:
        # Renommée depuis le panneau latéral gauche (potentiellement pendant
        # la lecture d'une vidéo) : JSON, jamais de rechargement.
        if not ok:
            return jsonify(ok=False, error=error or "Erreur inconnue."), 400
        return jsonify(ok=True, id=playlist_id, name=new_name.strip())
    flash(error if not ok else "Playlist renommée.")
    return redirect(request.referrer or url_for("index"))


@app.route("/playlist/<int:playlist_id>/delete", methods=["POST"])
def playlist_delete(playlist_id):
    is_ajax = bool(request.form.get("ajax"))
    db.delete_playlist(playlist_id)
    own_page = url_for("playlist_view", playlist_id=playlist_id)
    if is_ajax:
        referrer = request.referrer or ""
        return jsonify(ok=True, own_page=own_page in referrer)
    flash("Playlist supprimée.")
    referrer = request.referrer or ""
    if own_page in referrer:
        return redirect(url_for("index"))
    return redirect(referrer or url_for("index"))


@app.route("/organize")
def organize_view():
    section = request.args.get("section")  # 'category', 'playlist', ou absent (les deux)
    show_categories = section != "playlist"
    show_playlists = section != "category"
    if section == "category":
        page_heading = "Catégories"
    elif section == "playlist":
        page_heading = "Playlists"
    else:
        page_heading = "Organiser"

    categories, playlists, popular_tags = get_sidebar_data()
    playlist_list = _flatten_playlist_tree(db.get_playlists_tree())
    flat_categories = get_flat_categories()
    flat_playlists = get_flat_playlists()
    top_level_count = sum(1 for p in playlist_list if not p["depth"])
    sub_count = sum(1 for p in playlist_list if p["depth"])
    stats = {
        "categories": len(categories),
        "playlists_top": top_level_count,
        "playlists_sub": sub_count,
        "playlist_videos": sum(p["n"] for p in playlist_list),
    }
    return render_template(
        "organize.html", category_list=categories, playlist_list=playlist_list,
        categories=categories, playlists=playlists, popular_tags=popular_tags,
        flat_categories=flat_categories, flat_playlists=flat_playlists,
        show_categories=show_categories, show_playlists=show_playlists,
        page_heading=page_heading, stats=stats,
    )


# ---------- Paramètres / scan ----------

def _detect_browsers():
    """Détecte les navigateurs installés sur la machine et retourne une liste
    de dictionnaires {id, label, path}. 'system' (path=None) correspond au
    navigateur par défaut du système et est toujours proposé en premier."""
    browsers = [{"id": "system", "label": "Navigateur par défaut du système", "path": None}]
    definitions = [
        ("chrome", "Google Chrome"),
        ("firefox", "Mozilla Firefox"),
        ("edge", "Microsoft Edge"),
        ("brave", "Brave"),
        ("opera", "Opera"),
    ]

    found_paths = {}
    if platform.system() == "Windows":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidate_paths = {
            "chrome": [
                os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            ],
            "firefox": [
                os.path.join(pf, "Mozilla Firefox", "firefox.exe"),
                os.path.join(pf86, "Mozilla Firefox", "firefox.exe"),
            ],
            "edge": [
                os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
            "brave": [
                os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(pf86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            ],
            "opera": [
                os.path.join(local, "Programs", "Opera", "opera.exe"),
                os.path.join(pf, "Opera", "opera.exe"),
            ],
        }
        for bid, paths in candidate_paths.items():
            for p in paths:
                if p and os.path.isfile(p):
                    found_paths[bid] = p
                    break
    else:
        bin_names = {
            "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
            "firefox": ["firefox"],
            "edge": ["microsoft-edge", "microsoft-edge-stable"],
            "brave": ["brave-browser", "brave"],
            "opera": ["opera"],
        }
        for bid, names in bin_names.items():
            for name in names:
                found = shutil.which(name)
                if found:
                    found_paths[bid] = found
                    break

    for bid, label in definitions:
        if bid in found_paths:
            browsers.append({"id": bid, "label": label, "path": found_paths[bid]})
    return browsers


@app.route("/api/disk-filter", methods=["POST"])
def api_disk_filter():
    """API JSON dédiée « Disques actifs ».

    Corps : { "disabled_folders": [...], "video_id": optional }
    Cache mémoire mis à jour tout de suite (stream/heartbeat/mini-lecteur).
    Colonne SQL disk_off mise à jour en arrière-plan (pas de gel UI).
    """
    payload = request.get_json(silent=True) or {}
    raw = payload.get("disabled_folders")
    if not isinstance(raw, list):
        return jsonify(ok=False, error="disabled_folders doit être une liste"), 400

    settings = config.load_settings()
    media = list(settings.get("media_folders") or [])
    media_norm = {db._normalize_folder_path(m): m for m in media}
    disabled = []
    for f in raw:
        if not f or not isinstance(f, str):
            continue
        n = db._normalize_folder_path(f)
        if n in media_norm:
            disabled.append(media_norm[n])
        else:
            for m in media:
                if m == f:
                    disabled.append(m)
                    break

    seen = set()
    disabled_unique = []
    for d in disabled:
        if d not in seen:
            seen.add(d)
            disabled_unique.append(d)

    settings["disabled_folders"] = disabled_unique
    config.save_settings(settings)

    # Cache mémoire immédiat. disk_off en fond ; bump live différé
    # (évite soft-nav concurrent pendant le geste cocher/décocher).
    def _after_bg(_n):
        def _bump():
            try:
                live_updates.bump("disk_filter")
            except Exception:
                pass
        try:
            import threading
            threading.Timer(1.2, _bump).start()
        except Exception:
            _bump()

    db.set_disabled_folders(disabled_unique, on_done=_after_bg)

    video_id = payload.get("video_id")
    try:
        video_id = int(video_id) if video_id is not None else None
    except (TypeError, ValueError):
        video_id = None
    allowed = db.is_video_allowed(video_id) if video_id is not None else True

    return jsonify(
        ok=True,
        disabled_folders=disabled_unique,
        allowed=allowed,
        media_folders=media,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    settings = config.load_settings() if hasattr(config, "load_settings") else None
    if settings is None:
        settings = {"media_folders": [], "items_per_page": 40, "thumbnail_time_seconds": 10, "browser": "system", "disabled_folders": []}

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_folder":
            folder = request.form.get("folder", "").strip()
            if folder and folder not in settings["media_folders"]:
                settings["media_folders"].append(folder)
                config.save_settings(settings)
        elif action == "remove_folder":
            folder = request.form.get("folder", "").strip()
            settings["media_folders"] = [f for f in settings["media_folders"] if f != folder]
            settings["disabled_folders"] = [f for f in settings.get("disabled_folders", []) if f != folder]
            config.save_settings(settings)
        elif action == "set_browser":
            settings["browser"] = request.form.get("browser", "system").strip() or "system"
            config.save_settings(settings)
            flash("Navigateur de lancement enregistré.")
        elif action == "set_disk_filter":
            # Formulaire classique (onchange → requestSubmit) ou noscript.
            is_ajax = bool(request.form.get("ajax"))
            enabled = set(request.form.getlist("enabled_folder"))
            settings["disabled_folders"] = [f for f in settings["media_folders"] if f not in enabled]
            config.save_settings(settings)

            def _after_bg(_n):
                try:
                    live_updates.bump("disk_filter")
                except Exception:
                    pass

            db.set_disabled_folders(settings["disabled_folders"], on_done=_after_bg)
            if is_ajax:
                return jsonify(ok=True, disabled_folders=settings["disabled_folders"])
            flash("Filtre de disques mis à jour.")
            return redirect(url_for("settings_view") + "#section-disques")
        elif action == "start_scan":
            started = scanner.start_scan_async(
                settings["media_folders"],
                settings.get("thumbnail_time_seconds", 10),
            )
            if not started:
                flash("Un scan est déjà en cours.")
            else:
                # Les codecs à risque sont préparés pour le fallback à la
                # demande. Aucun FFmpeg de pré-transcodage ne démarre après
                # le scan : cela garantit une charge CPU nulle en fin de scan.
                pass
        return redirect(url_for("settings_view"))

    categories, playlists, popular_tags = get_sidebar_data()
    return render_template(
        "settings.html", settings=settings, ffmpeg=scanner.FFMPEG, ffprobe=scanner.FFPROBE,
        categories=categories, playlists=playlists, popular_tags=popular_tags,
        browsers=_detect_browsers(),
    )


@app.route("/settings/missing/delete", methods=["POST"])
def delete_missing():
    ids = request.form.getlist("video_id", type=int)
    if not ids:
        # Aucune case cochée : on supprime toutes les vidéos manquantes
        ids = [v["id"] for v in db.get_missing_videos()]
    db.delete_videos(ids)
    flash(f"{len(ids)} entrée(s) supprimée(s) de la bibliothèque (les fichiers, s'ils existent encore, n'ont pas été touchés).")
    return redirect(url_for("settings_view"))



# Dossiers d'images de couverture, par type d'entité — utilisés à la fois
# par l'export (lire les fichiers référencés dans data[kind]["images"])
# et par l'import (réécrire ces mêmes fichiers au bon endroit). Reflète
# exactement config.CATEGORY_IMAGES_DIR / TAG_IMAGES_DIR / PLAYLIST_IMAGES_DIR.
_COVER_IMAGE_DIRS = {
    "categories": config.CATEGORY_IMAGES_DIR,
    "tags": config.TAG_IMAGES_DIR,
    "playlists": config.PLAYLIST_IMAGES_DIR,
}
# Mêmes extensions que _save_image_upload : on n'accepte à l'import que des
# images, jamais un autre type de fichier, même si le zip a été trafiqué.
_COVER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@app.route("/library/export")
def library_export():
    """Exporte l'intégralité des métadonnées de la bibliothèque (catégories,
    tags, playlists + leur contenu, métadonnées par vidéo) ET leurs images
    de couverture, en une seule archive .zip téléchargeable — indépendante
    du fichier SQLite brut, pour sauvegarder ou migrer la bibliothèque sans
    le reste des caches/vignettes locaux. Les vidéos elles-mêmes ne sont ni
    lues ni copiées : seuls du texte (chemins, titres, notes, tags...) et
    les quelques images de couverture (pas les vignettes générées par le
    scan) sont exportés.

    Structure de l'archive :
      export.json            -> les métadonnées (comme avant), avec en plus
                                 pour chaque catégorie/tag/playlist la liste
                                 des noms de fichiers de ses images ("images")
      images/categories/...  -> fichiers images des catégories
      images/tags/...        -> fichiers images des tags
      images/playlists/...   -> fichiers images des playlists
    """
    data = db.export_library()
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("export.json", payload)
        for kind, src_dir in _COVER_IMAGE_DIRS.items():
            for entry in data.get(kind, []):
                for fname in entry.get("images") or []:
                    src_path = os.path.join(src_dir, fname)
                    if os.path.exists(src_path):
                        zf.write(src_path, f"images/{kind}/{fname}")
    buf.seek(0)

    filename = f"mediatheque_export_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=filename,
    )


@app.route("/library/import", methods=["POST"])
def library_import():
    """Réimporte un fichier produit par /library/export : fusionne
    catégories/tags/playlists/métadonnées vidéo (+ leurs images de
    couverture) dans la bibliothèque actuelle, par correspondance de
    chemin de fichier. Ne supprime jamais rien.

    Accepte le nouveau format .zip (export.json + images/) ; accepte
    aussi, par compatibilité, un ancien fichier .json nu (export
    d'avant l'ajout des images) — dans ce cas les métadonnées reviennent
    normalement, simplement sans couvertures (comme avant)."""
    file = request.files.get("library_file")
    if not file or not file.filename:
        flash("Aucun fichier sélectionné.")
        return redirect(url_for("settings_view"))

    raw = file.read()

    if zipfile.is_zipfile(io.BytesIO(raw)):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                with zf.open("export.json") as jf:
                    data = json.loads(jf.read().decode("utf-8"))
                if not isinstance(data, dict):
                    flash("Fichier zip invalide : export.json a une structure inattendue.")
                    return redirect(url_for("settings_view"))

                # Pour chaque image référencée dans le JSON, on l'extrait du
                # zip vers le bon dossier data/..._images/ sous un nouveau
                # nom de fichier unique (évite tout risque d'écraser une
                # image existante par coïncidence de nom), puis on met à
                # jour la liste "images" de l'entrée avec ce nouveau nom
                # AVANT de la transmettre à db.import_library().
                names_in_zip = set(zf.namelist())
                for kind, dest_dir in _COVER_IMAGE_DIRS.items():
                    for entry in data.get(kind, []):
                        new_names = []
                        for fname in entry.get("images") or []:
                            ext = os.path.splitext(fname or "")[1].lower()
                            zpath = f"images/{kind}/{fname}"
                            if ext not in _COVER_IMAGE_EXTENSIONS or zpath not in names_in_zip:
                                continue
                            new_name = f"{uuid.uuid4().hex}{ext}"
                            with zf.open(zpath) as src:
                                with open(os.path.join(dest_dir, new_name), "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                            new_names.append(new_name)
                        entry["images"] = new_names
        except KeyError:
            flash("Fichier zip invalide : export.json introuvable à l'intérieur de l'archive.")
            return redirect(url_for("settings_view"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            flash(f"export.json invalide dans l'archive : {exc}")
            return redirect(url_for("settings_view"))
    else:
        # Compatibilité : ancien format .json nu, sans images.
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            flash(f"Fichier JSON invalide : {exc}")
            return redirect(url_for("settings_view"))
        if not isinstance(data, dict):
            flash("Fichier JSON invalide : structure inattendue.")
            return redirect(url_for("settings_view"))

    try:
        stats = db.import_library(data)
    except Exception as exc:
        # Un JSON valide peut tout de même contenir une entrée mal formée
        # (rating non numérique, liste inattendue, export ancien, etc.).
        # Ne jamais laisser cette exception remonter jusqu'au serveur Flask :
        # l'utilisateur doit retrouver une application utilisable après un
        # import raté, avec un message explicite et sans faux « import
        # terminé ».
        flash(f"Import annulé : aucune donnée supplémentaire n'a pu être rattachée ({exc}).")
        return redirect(url_for("settings_view"))

    # Rafraîchissement temps réel de tous les onglets ouverts (catégories,
    # tags, grilles, métadonnées) sans rechargement manuel.
    try:
        live_updates.bump("library_import")
    except Exception:
        pass

    # Note : les vidéos absentes de la bibliothèque actuelle
    # (stats['videos_not_found']) sont mises en file d'attente et
    # réattribuées automatiquement dès qu'un scan les indexe — le chemin
    # exact n'a pas d'importance (content_hash / nom de fichier / taille).
    pending = stats.get("pending_remaining", 0)
    not_found = stats.get("videos_not_found", 0)
    extra = ""
    if pending or not_found:
        extra = (
            f" {not_found} vidéo(s) pas encore indexée(s) : leurs infos seront "
            f"réattribuées automatiquement dès le prochain scan "
            f"({pending} encore en attente)."
        )
    flash(
        "Import terminé — "
        f"{stats['categories']} catégorie(s), {stats['tags']} tag(s), "
        f"{stats['playlists']} playlist(s), {stats['videos_updated']} vidéo(s) mise(s) à jour "
        f"({stats['playlist_items_linked']} rattachement(s) de playlist)."
        + extra
    )
    return redirect(url_for("settings_view"))


# Dossiers/fichiers systématiquement exclus de la sauvegarde complète du
# projet (voir /project/backup ci-dessous) : caches Python et fichiers
# annexes de la base SQLite en mode WAL (remplacés par une copie cohérente
# unique, voir db.backup_to), inutiles voire trompeurs une fois recopiés
# tels quels sur une autre machine.
_PROJECT_BACKUP_EXCLUDED_DIRNAMES = {"__pycache__"}
_PROJECT_BACKUP_EXCLUDED_FILENAMES = {
    os.path.basename(config.DB_PATH) + "-wal",
    os.path.basename(config.DB_PATH) + "-shm",
}
_PROJECT_BACKUP_EXCLUDED_EXTENSIONS = {".pyc", ".pyo"}


@app.route("/project/backup")
def project_backup():
    """Sauvegarde de l'application entière (code + données) dans un unique
    fichier .zip téléchargeable : dézippé sur n'importe quel autre PC (avec
    Python et ffmpeg installés, voir README.md), il redonne l'application
    complète avec sa bibliothèque (catégories, tags, playlists, notes,
    favoris, vignettes, réglages...) telle qu'elle était au moment de la
    sauvegarde. Les vidéos elles-mêmes, situées dans les dossiers configurés
    en dehors de ce projet, ne sont jamais incluses (elles restent sur leurs
    disques et se re-rattachent d'elles-mêmes à un nouveau scan si les
    chemins n'ont pas changé)."""
    base_dir = config.BASE_DIR
    project_name = os.path.basename(base_dir.rstrip(os.sep)) or "mediatheque"

    tmp = tempfile.NamedTemporaryFile(prefix="mediatheque_backup_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Copie cohérente de la base à part (voir db.backup_to) : le fichier
    # -wal courant, lui, est explicitement exclu ci-dessous (il n'aurait
    # aucun sens sans le processus SQLite qui l'accompagne).
    db_backup_fd, db_backup_path = tempfile.mkstemp(suffix=".db")
    os.close(db_backup_fd)
    os.remove(db_backup_path)  # sqlite3 crée lui-même le fichier au backup
    try:
        db.backup_to(db_backup_path)

        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirnames, filenames in os.walk(base_dir):
                dirnames[:] = [d for d in dirnames if d not in _PROJECT_BACKUP_EXCLUDED_DIRNAMES]
                for filename in filenames:
                    if filename in _PROJECT_BACKUP_EXCLUDED_FILENAMES:
                        continue
                    if os.path.splitext(filename)[1] in _PROJECT_BACKUP_EXCLUDED_EXTENSIONS:
                        continue
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.join(project_name, os.path.relpath(full_path, base_dir))
                    if os.path.abspath(full_path) == os.path.abspath(config.DB_PATH):
                        zf.write(db_backup_path, rel_path)  # copie cohérente, pas le fichier live
                    else:
                        zf.write(full_path, rel_path)
    finally:
        if os.path.exists(db_backup_path):
            os.remove(db_backup_path)

    filename = f"mediatheque_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, mimetype="application/zip", as_attachment=True, download_name=filename)


@app.route("/scan/status")
def scan_status():
    return jsonify(scanner.get_status())


@app.route("/api/live")
def api_live():
    """Flux SSE (Server-Sent Events) : le navigateur reste connecté et
    reçoit un signal dès qu'une donnée change n'importe où dans
    l'application (voir live_updates.py et static/js/live-sync.js)."""
    resp = Response(stream_with_context(live_updates.stream()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ---------- Lancement ----------

def _open_browser():
    import time
    time.sleep(1.2)
    url = f"http://127.0.0.1:{config.PORT}"

    browser_id = "system"
    try:
        browser_id = config.load_settings().get("browser", "system")
    except Exception:
        pass

    if browser_id and browser_id != "system":
        try:
            match = next(
                (b for b in _detect_browsers() if b["id"] == browser_id and b.get("path")),
                None,
            )
            if match:
                subprocess.Popen([match["path"], url])
                return
        except Exception:
            pass  # en cas de souci, on retombe sur le navigateur par défaut ci-dessous

    webbrowser.open(url)


if __name__ == "__main__":
    # -------------------------------------------------------------------
    # Réglages réseau bas niveau pour un streaming vidéo ultra-fluide
    # -------------------------------------------------------------------
    # Purement additif : n'affecte aucune route, aucune page, aucun
    # comportement fonctionnel — uniquement la façon dont les octets
    # transitent sur le socket TCP local. Chaque réglage est protégé
    # individuellement par un try/except : en cas d'environnement où un
    # réglage ne serait pas disponible (OS, permissions...), le serveur
    # démarre quand même normalement avec le comportement par défaut.
    try:
        import socketserver
        # Backlog de connexions en attente (5 par défaut en Python) : trop
        # bas pour un lecteur vidéo qui peut ouvrir plusieurs requêtes
        # quasi simultanées lors d'un glissement rapide dans la timeline
        # (ancienne requête abandonnée pas encore totalement fermée +
        # nouvelle requête de seek qui arrive déjà). Une valeur plus haute
        # évite qu'une connexion soit refusée/retardée dans ce cas précis.
        socketserver.TCPServer.request_queue_size = 128
    except Exception:
        pass

    try:
        import socket as _socket
        from werkzeug.serving import WSGIRequestHandler as _WSGIRequestHandler

        class _FastStreamingRequestHandler(_WSGIRequestHandler):
            """Identique en tout point au gestionnaire de requêtes standard
            de Werkzeug — seule différence : désactive l'algorithme de
            Nagle (TCP_NODELAY) et agrandit le tampon d'envoi du socket
            juste après l'établissement de la connexion. Nagle retarde de
            quelques dizaines de ms l'envoi de petits paquets pour les
            regrouper, ce qui est invisible pour une page web classique
            mais peut se traduire par un micro-retard perceptible pour le
            tout premier bloc envoyé juste après un seek dans la vidéo
            (voir la montée en charge progressive des blocs dans
            _serve_video_file). Aucune route ni aucun comportement HTTP
            n'est modifié : uniquement le comportement bas niveau du
            socket."""

            # HTTP/1.1 explicite : les connexions TCP sont réutilisées d'une
            # requête Range à l'autre. En HTTP/1.0, chaque saut dans la
            # timeline rouvrait une connexion (poignée de main TCP + montée
            # en débit à zéro) — une cause directe de micro-arrêts.
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                try:
                    self.connection.setsockopt(
                        _socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1
                    )
                except Exception:
                    pass
                try:
                    self.connection.setsockopt(
                        _socket.SOL_SOCKET, _socket.SO_SNDBUF, 1024 * 1024
                    )
                except Exception:
                    pass

        _request_handler = _FastStreamingRequestHandler
    except Exception:
        _request_handler = None

    # Nettoyage cache transcode au démarrage (fichiers .part / trop vieux)
    try:
        _gc_transcode_cache()
    except Exception:
        pass

    # Startup GC note
    if config.HOST in ("0.0.0.0", "::"):
        print(
            "⚠  Médiathèque écoute sur toutes les interfaces (HOST=%s). "
            "Toute machine du réseau local peut accéder à ta bibliothèque "
            "et aux actions (suppression, paramètres). "
            "Pour restreindre à cet ordinateur : HOST = \"127.0.0.1\" dans config.py."
            % (config.HOST,)
        )
    threading.Thread(target=_open_browser, daemon=True).start()
    threading.Thread(target=live_updates.watch_scanner, args=(scanner.get_status,), daemon=True).start()
    # Nettoyage opportuniste du cache de conversion (fichiers orphelins /
    # trop vieux / trop nombreux) — purement défensif, n'affecte jamais
    # une conversion en cours (les .part récents sont conservés).
    try:
        _gc_transcode_cache()
    except Exception:
        pass
    # Pré-transcode en arrière-plan les vidéos à codec à risque déjà
    # indexées (voir _precode_risky_videos) : ne bloque jamais le
    # démarrage, une seule conversion à la fois.
    start_precode_risky_videos_async()
    if _request_handler is not None:
        app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True,
                request_handler=_request_handler)
    else:
        app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
