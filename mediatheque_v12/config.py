# -*- coding: utf-8 -*-
"""
Configuration de la Médiathèque locale.
Tout est stocké dans le dossier data/ à côté de l'application :
- data/mediatheque.db   -> base SQLite (métadonnées, tags, catégories)
- data/thumbnails/      -> vignettes générées (jpg)
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "mediatheque.db")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbnails")
CATEGORY_IMAGES_DIR = os.path.join(DATA_DIR, "category_images")
PLAYLIST_IMAGES_DIR = os.path.join(DATA_DIR, "playlist_images")
TAG_IMAGES_DIR = os.path.join(DATA_DIR, "tag_images")
# Cache des vidéos converties à la volée pour le lecteur (voir "conversion
# automatique de secours" dans app.py) — un fichier .mp4 compatible par
# vidéo, généré une seule fois puis réutilisé.
TRANSCODE_DIR = os.path.join(DATA_DIR, "transcoded")

# Extensions vidéo reconnues lors du scan
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".m2ts", ".mts", ".ogv", ".vob"
}

# Chemins vers ffmpeg / ffprobe. Si ces binaires sont dans le PATH Windows,
# laisser tel quel. Sinon, mettre le chemin complet, ex:
# r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_BIN = os.environ.get("MEDIATHEQUE_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("MEDIATHEQUE_FFPROBE", "ffprobe")

# Port et hôte du serveur local.
# host="0.0.0.0" -> accessible aussi depuis d'autres appareils du même réseau local (WiFi maison/bureau)
# host="127.0.0.1" -> accessible uniquement depuis cet ordinateur
HOST = "0.0.0.0"
PORT = 5000

# Nombre de vidéos affichées par page dans la grille
PAGE_SIZE = 60

# Aperçu au survol (défilement d'images dans la grille)
PREVIEW_FRAME_COUNT = 10           # nombre d'images pour l'aperçu au survol
PREVIEW_MIN_DURATION = 20          # durée minimale (secondes) pour générer un aperçu multi-images
PREVIEW_FRACTIONS = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]  # positions dans la vidéo

# ---------------------------------------------------------------------------
# PUISSANCE UTILISÉE PAR LE SCAN
# ---------------------------------------------------------------------------
# Pourcentage de la puissance CPU de la machine que le scan a le droit
# d'utiliser (1 à 100). Réservé UNIQUEMENT au scan lancé depuis Paramètres :
# 100 = vitesse maximale sur tous les cœurs, priorité normale. Dès qu'une
# vidéo est lue (ou toute autre tâche hors-scan), le scan se met en pause
# (voir SCAN_PAUSE_DURING_PLAYBACK) et le CPU redevient disponible pour le
# lecteur / le système à une fréquence idéale sans ramer.
# Mettre 50 pour un scan discret en arrière-plan, 25 pour très discret.
SCAN_CPU_PERCENT = 100

# Pendant le scan (Paramètres uniquement), l'application utilise TOUTE la
# puissance du processeur : ffmpeg/ffprobe en priorité NORMALE, tous les
# cœurs. Hors scan (lecture, navigation, conversion de secours…), le CPU
# est plafonné par NORMAL_USE_CPU_PERCENT ci-dessous pour rester fluide.
# Mettre True pour forcer une priorité réduite même pendant le scan.
SCAN_YIELD_TO_OTHER_APPS = False

# (ancien réglage, conservé pour compatibilité : priorité "inactif" extrême)
SCAN_LOW_PRIORITY = False

# Nombre de cœurs laissés libres en permanence pendant le scan.
# 0 = le scan exploite tous les cœurs (puissance maximale). La lecture vidéo
# reste fluide car le scan se met en pause pendant la lecture.
SCAN_RESERVED_CORES = 0

# ---------------------------------------------------------------------------
# PUISSANCE UTILISÉE EN DEHORS DU SCAN
# ---------------------------------------------------------------------------
# Pourcentage maximum de CPU que les tâches hors-scan (conversion de
# secours, warm disque, etc.) ont le droit d'utiliser.
# 30 = plafond bas (~30 % des cœurs) pour garantir fluidité totale pendant
# la lecture (timeline sans à-coups) et le reste de l'appli, sans ramer le
# PC. Le scan (SCAN_CPU_PERCENT = 100) n'est PAS concerné : il conserve
# sa plage maximale et s'exempte de ce plafond.
# Priorité OS réduite (BELOW_NORMAL / nice) en complément de ce budget.
NORMAL_USE_CPU_PERCENT = 30


# Indexation parallèle : nombre de vidéos traitées en même temps par le
# scanner (ffprobe + vignettes). 0 = automatique, calculé à partir de
# SCAN_CPU_PERCENT ci-dessus (recommandé).
SCAN_WORKERS = 0

# Durée maximale d'une extraction ffmpeg pendant le scan. Certains fichiers
# incomplets/corrompus ou hébergés sur un lecteur réseau peuvent laisser
# ffmpeg attendre plusieurs minutes sur les toutes dernières vidéos, donnant
# l'impression que le scan est bloqué vers 97 %. Passé ce délai, le fichier
# est tout de même indexé et le scan continue immédiatement.
SCAN_FFMPEG_TIMEOUT = 60

# Priorité absolue à la lecture vidéo : quand une vidéo est en cours de
# lecture dans l'application, le scan suspend ses extractions d'images pour
# que la lecture reste parfaitement fluide (et que le PC ne rame pas). Le
# scan reprend tout seul dès la pause/fin de la lecture. Mettre False pour
# laisser le scan tourner même pendant la lecture.
SCAN_PAUSE_DURING_PLAYBACK = True



os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)
os.makedirs(CATEGORY_IMAGES_DIR, exist_ok=True)
os.makedirs(PLAYLIST_IMAGES_DIR, exist_ok=True)
os.makedirs(TAG_IMAGES_DIR, exist_ok=True)
os.makedirs(TRANSCODE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Paramètres modifiables depuis la page /settings (dossiers à scanner, etc.)
# Stockés dans data/settings.json pour persister entre les lancements.
# ---------------------------------------------------------------------------
import json  # noqa: E402

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "media_folders": [],
    "items_per_page": 40,
    "thumbnail_time_seconds": 10,
    "browser": "system",  # navigateur utilisé pour ouvrir l'appli au démarrage
    "disabled_folders": [],  # dossiers (disques) décochés : leurs vidéos sont masquées partout
    "last_browse_folder": "",  # dernier dossier choisi via "Parcourir..." (mémoire entre sessions)
}


def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


def save_settings(settings):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
