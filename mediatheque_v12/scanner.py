# -*- coding: utf-8 -*-
"""
Scanner de bibliothèque : parcourt les dossiers configurés, détecte les
nouvelles vidéos (ou celles modifiées), extrait durée/résolution via ffprobe
et génère une vignette via ffmpeg.

Ne touche JAMAIS aux fichiers vidéo eux-mêmes : lecture seule.
"""
import os
import sys
import json
import subprocess
import threading
import time
import shutil
import hashlib
import datetime
import queue
import concurrent.futures

import config
import db


def mtime_to_date_str(mtime):
    """Convertit un timestamp mtime (date de modification du fichier,
    identique à la colonne "Modifié le" de l'explorateur Windows) en chaîne
    YYYY-MM-DD, au format attendu par le champ <input type="date">. Utilise
    l'heure locale, comme l'explorateur Windows. Ne lève jamais d'erreur."""
    try:
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""

_status_lock = threading.Lock()
_status = {
    "running": False,
    "phase": "idle",       # idle | listing | processing | done | error
    "total_found": 0,
    "processed": 0,
    "new": 0,
    "updated": 0,
    "current_file": "",
    "error": "",
    "started_at": 0,
    "finished_at": 0,
}


def get_status():
    with _status_lock:
        return dict(_status)


def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def resolve_ffmpeg():
    """Utilise le binaire configuré s'il existe dans le PATH, sinon se
    replie sur le binaire ffmpeg embarqué fourni par imageio-ffmpeg."""
    ffmpeg_bin = config.FFMPEG_BIN
    if shutil.which(ffmpeg_bin):
        return ffmpeg_bin
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ffmpeg_bin  # tant pis, on tentera quand même


def resolve_ffprobe(ffmpeg_path):
    ffprobe_bin = config.FFPROBE_BIN
    if shutil.which(ffprobe_bin):
        return ffprobe_bin
    # ffprobe n'est pas fourni par imageio-ffmpeg ; on essaie à côté de ffmpeg
    candidate = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe")
    if os.path.exists(candidate) or os.path.exists(candidate + ".exe"):
        return candidate
    return ffprobe_bin


FFMPEG = resolve_ffmpeg()
FFPROBE = resolve_ffprobe(FFMPEG)

# Priorité système réduite au minimum pour les processus ffmpeg/ffprobe
# lancés par le scanner (probe_video, make_thumbnail, make_preview_frames,
# make_thumbnail_and_previews) : le décodage vidéo (en particulier
# HEVC/H.265, nettement plus coûteux en CPU que le H.264 en décodage
# logiciel) peut saturer tous les cœurs disponibles pendant un scan, en
# particulier avec plusieurs vidéos traitées en parallèle (SCAN_WORKERS).
# Sans cela, non seulement le serveur Flask (pages, aperçus, navigation)
# mais tout le PC (autres applications) peut devenir lent/saccadé pendant
# toute la durée du scan.
#
# Deux causes cumulées étaient en jeu, corrigées ensemble ici :
#
# 1) Priorité de planification trop haute : BELOW_NORMAL reste une
#    priorité "normale" pour le système, qui continue à donner de larges
#    tranches de temps CPU à ffmpeg dès que tous les cœurs sont occupés.
#    IDLE_PRIORITY_CLASS (Windows) / nice au maximum (Linux/Mac) indique
#    explicitement au planificateur de ne consommer le CPU que lorsque
#    aucune autre application n'en a besoin à cet instant : le scan reste
#    aussi rapide quand le PC est inactif, mais cède immédiatement la main
#    dès qu'on touche à autre chose.
# 2) Sursouscription des threads : chaque processus ffmpeg utilise, par
#    défaut, un thread interne de décodage par cœur logique détecté. Avec
#    SCAN_WORKERS vidéos traitées en parallèle, cela peut lancer bien plus
#    de threads de décodage que de cœurs réels (ex. 4 vidéos x 8 threads
#    sur un CPU 8 cœurs = 32 threads en compétition), ce qui sature et fait
#    "ramer" la machine entière même avec une priorité basse. Voir
#    FFMPEG_THREADS_PER_JOB, appliqué via "-threads" sur chaque appel
#    ffmpeg, pour répartir les cœurs disponibles entre les jobs parallèles
#    au lieu de laisser chacun tenter de tous les utiliser.
#
# Résultat identique dans les deux cas (mêmes vignettes, mêmes
# durées/résolutions), seule la vitesse d'exécution en tâche de fond et
# l'impact sur le reste du PC changent.
try:
    # os.process_cpu_count() (Python 3.13+) respecte l'affinité réelle du
    # processus (cœurs autorisés), plus juste que os.cpu_count().
    _CPU_COUNT = os.process_cpu_count() or os.cpu_count() or 4
except AttributeError:
    _CPU_COUNT = os.cpu_count() or 4

# Part de la puissance machine allouée au scan (voir SCAN_CPU_PERCENT dans
# config.py). 95 par défaut = vitesse maximale.
try:
    _CPU_PERCENT = float(getattr(config, "SCAN_CPU_PERCENT", 95) or 95)
except (TypeError, ValueError):
    _CPU_PERCENT = 95.0
_CPU_PERCENT = max(5.0, min(100.0, _CPU_PERCENT))
# Budget de threads de décodage simultanés (au moins 1 cœur).
_CPU_BUDGET = max(1, int(round(_CPU_COUNT * _CPU_PERCENT / 100.0)))

# Priorité de planification du scan : le scan garde son budget CPU maximal,
# mais ses processus ffmpeg/ffprobe tournent en priorité RÉDUITE. Quand rien
# d'autre ne tourne, le scan prend toute la machine (vitesse maximale
# inchangée) ; dès qu'une vidéo est lue dans l'application ou qu'un autre
# logiciel demande du CPU, le système leur donne la main instantanément.
_LOW_PRIORITY = bool(getattr(config, "SCAN_LOW_PRIORITY", False))
_YIELD = bool(getattr(config, "SCAN_YIELD_TO_OTHER_APPS", False))

_NICE_PREFIX = []
_SUBPROCESS_KWARGS = {}
if _LOW_PRIORITY:
    if os.name == "nt":
        _SUBPROCESS_KWARGS = {"creationflags": subprocess.IDLE_PRIORITY_CLASS}
    elif shutil.which("nice"):
        # Préfixe externe plutôt qu'un preexec_fn : le scanner lance ces
        # commandes depuis plusieurs threads (ThreadPoolExecutor), et
        # preexec_fn n'y est pas sûr (risque de blocage au fork).
        _NICE_PREFIX = ["nice", "-n", "19"]
elif _YIELD:
    if os.name == "nt":
        _SUBPROCESS_KWARGS = {
            "creationflags": getattr(
                subprocess,
                "BELOW_NORMAL_PRIORITY_CLASS",
                subprocess.NORMAL_PRIORITY_CLASS,
            )
        }
    elif shutil.which("nice"):
        # nice 10 : plein pot sur machine libre, mais cède le processeur à la
        # lecture vidéo et aux autres applications dès qu'elles en ont besoin.
        _NICE_PREFIX = ["nice", "-n", "10"]
elif os.name == "nt":
    # Puissance maximale : priorité normale (aucun bridage) pour ffmpeg/ffprobe.
    _SUBPROCESS_KWARGS = {"creationflags": subprocess.NORMAL_PRIORITY_CLASS}

# Nombre de vidéos traitées en parallèle. On sur-souscrit volontairement
# (2 jobs par cœur du budget, plafonné à 32) : chaque job passe une grande
# partie de son temps à ATTENDRE (lecture disque, démarrage du processus
# ffmpeg, ffprobe). Ces attentes se recouvrent, si bien que le CPU reste
# réellement occupé à ~SCAN_CPU_PERCENT au lieu de tourner à vide entre
# deux fichiers.
SCAN_WORKERS = config.SCAN_WORKERS or min(32, max(4, _CPU_BUDGET * 4))
SCAN_WORKERS = max(1, min(SCAN_WORKERS, 8 * _CPU_COUNT))
# Threads internes par appel ffmpeg : le produit workers x threads reste
# aligné sur le budget CPU choisi.
FFMPEG_THREADS_PER_JOB = max(1, _CPU_BUDGET // max(1, SCAN_WORKERS))
_THREADS_ARGS = ["-threads", str(FFMPEG_THREADS_PER_JOB)]

# Un fichier lent, incomplet ou corrompu ne doit jamais retenir les derniers
# pourcents du scan pendant plusieurs minutes. L'ancienne formule autorisait
# jusqu'à 330 s par tentative, puis autant en repli. Cette limite vaut pour
# la commande groupée entière ; en fonctionnement normal elle termine en
# quelques secondes. Les métadonnées restent enregistrées même si aucune
# image exploitable ne peut être extraite dans ce délai.
try:
    _SCAN_FFMPEG_TIMEOUT = int(getattr(config, "SCAN_FFMPEG_TIMEOUT", 60) or 60)
except (TypeError, ValueError):
    _SCAN_FFMPEG_TIMEOUT = 60
_SCAN_FFMPEG_TIMEOUT = max(15, min(180, _SCAN_FFMPEG_TIMEOUT))

# ---------------------------------------------------------------------------
# SÉPARATION ATTENTE DISQUE / TRAVAIL CPU (scan plus rapide, PC réactif)
# ---------------------------------------------------------------------------
# Un fichier passe par deux phases très différentes : d'abord de l'ATTENTE
# (ouverture du fichier, lecture des en-têtes, empreinte de contenu) où le
# processeur ne fait presque rien, puis du DÉCODAGE d'images, où il travaille
# à fond. On lance donc beaucoup de vidéos en parallèle (SCAN_WORKERS, pour
# que les attentes disque se recouvrent au maximum et que le processeur ne
# tourne jamais à vide), mais on autorise seulement _DECODE_BUDGET extractions
# d'images EN MÊME TEMPS. Le disque est ainsi toujours sollicité au maximum
# pendant que la charge processeur reste plafonnée à la part choisie
# (SCAN_CPU_PERCENT) : le scan va au plus vite sans jamais saturer la machine
# ni faire ramer les autres applications.
_RESERVED = max(0, int(getattr(config, "SCAN_RESERVED_CORES", 1) or 0))
# On laisse toujours au moins un cœur libre pour la lecture vidéo, la
# navigation et les autres applications pendant le scan.
_DECODE_BUDGET = max(1, min(_CPU_BUDGET, _CPU_COUNT - _RESERVED if _CPU_COUNT > _RESERVED + 1 else _CPU_COUNT))
_DECODE_SLOTS = threading.Semaphore(_DECODE_BUDGET)

# ---------------------------------------------------------------------------
# PRIORITÉ ABSOLUE À LA LECTURE VIDÉO (application fluide, PC qui ne rame pas)
# ---------------------------------------------------------------------------
# Le scan tourne désormais à pleine vitesse (SCAN_CPU_PERCENT = 100). Pour que
# cela ne se paie JAMAIS pendant qu'on regarde une vidéo, le lecteur envoie un
# signal de vie ("battement") au serveur pendant la lecture : tant qu'une
# vidéo est en cours de lecture, les extractions d'images du scan sont mises
# en attente (aucun nouveau ffmpeg lancé), donc plus aucune concurrence CPU/
# disque avec le décodage vidéo. Dès la pause/fin de la vidéo — ou
# automatiquement au bout de _PLAYBACK_TTL secondes sans battement (onglet
# fermé, navigateur tué...) — le scan repart instantanément à pleine vitesse.
# Aucun travail n'est perdu : le scan est seulement suspendu, jamais annulé.
_PLAYBACK_TTL = 12.0
_PLAYBACK_LOCK = threading.Lock()
_PLAYBACK_UNTIL = 0.0
_PLAYBACK_GATE = threading.Event()
_PLAYBACK_GATE.set()  # ouvert = scan autorisé


def notify_playback_active(ttl=_PLAYBACK_TTL):
    """Appelé par le serveur quand une vidéo est en cours de lecture."""
    global _PLAYBACK_UNTIL
    with _PLAYBACK_LOCK:
        _PLAYBACK_UNTIL = time.time() + max(1.0, float(ttl or _PLAYBACK_TTL))
        _PLAYBACK_GATE.clear()


def notify_playback_stopped():
    """Appelé quand la lecture est mise en pause / terminée / quittée."""
    global _PLAYBACK_UNTIL
    with _PLAYBACK_LOCK:
        _PLAYBACK_UNTIL = 0.0
        _PLAYBACK_GATE.set()


def is_playback_active():
    with _PLAYBACK_LOCK:
        if _PLAYBACK_UNTIL and time.time() < _PLAYBACK_UNTIL:
            return True
        if not _PLAYBACK_GATE.is_set():
            _PLAYBACK_GATE.set()
        return False


# ---------------------------------------------------------------------------
# RÉACTIVITÉ PENDANT UN SCAN EN ARRIÈRE-PLAN (CLICS, NAVIGATION)
# ---------------------------------------------------------------------------
# La pause ci-dessus ne couvre que la LECTURE d'une vidéo (battements
# envoyés par le lecteur). Mais un scan à pleine puissance (SCAN_CPU_PERCENT
# = 100, priorité normale par défaut) peut, sur une grosse bibliothèque,
# occuper tous les cœurs assez longtemps pour que le simple fait de cliquer
# sur un lien (page vidéo, changement de page, filtre...) doive attendre son
# tour face aux processus ffmpeg/ffprobe déjà lancés — l'appli "tourne"
# quelques instants avant de répondre, alors même qu'aucune vidéo n'est en
# cours de lecture. On applique donc la même idée, en plus léger : chaque
# requête HTTP "réelle" reçue par le serveur (voir _touch_app_activity dans
# app.py, qui appelle notify_app_activity() ci-dessous) marque l'appli comme
# activement utilisée pendant quelques secondes ; le scan suspend alors ses
# NOUVELLES extractions d'image le temps que la requête en cours ait une
# vraie chance d'être traitée rapidement, puis reprend automatiquement.
# Sur une bibliothèque de plusieurs dizaines de milliers de vidéos, cette
# pause est négligeable sur la durée totale du scan (quelques secondes à
# chaque clic, pas en continu) ; en contrepartie l'appli reste réactive même
# pendant un scan intensif, sans avoir à ralentir sa vitesse de croisière.
_APP_ACTIVITY_LOCK = threading.Lock()
_APP_ACTIVITY_UNTIL = 0.0
_APP_ACTIVITY_PAUSE = 2.5  # secondes de répit accordées après chaque requête


def notify_app_activity(pause_seconds=_APP_ACTIVITY_PAUSE):
    """Appelé (voir app.py, _touch_app_activity) à chaque requête HTTP
    "réelle" reçue par le serveur, hors connexions techniques qui restent
    ouvertes en permanence (streaming SSE, battement de lecture...)."""
    global _APP_ACTIVITY_UNTIL
    with _APP_ACTIVITY_LOCK:
        _APP_ACTIVITY_UNTIL = time.time() + max(0.5, float(pause_seconds or _APP_ACTIVITY_PAUSE))


def _app_activity_recent():
    with _APP_ACTIVITY_LOCK:
        return bool(_APP_ACTIVITY_UNTIL and time.time() < _APP_ACTIVITY_UNTIL)


def _wait_if_playback():
    """Bloque tant qu'une lecture vidéo est en cours (expiration incluse),
    OU qu'une requête HTTP vient d'arriver dans l'application (voir
    notify_app_activity ci-dessus) : dans les deux cas, le scan reste en
    pause le temps nécessaire pour que l'appli/le lecteur restent
    parfaitement réactifs, sans aucun ralentissement perceptible côté
    utilisateur."""
    pause_for_playback = getattr(config, "SCAN_PAUSE_DURING_PLAYBACK", True)
    # Garde-fou : un utilisateur qui enchaîne les clics en continu (chaque
    # requête repoussant _APP_ACTIVITY_UNTIL de quelques secondes de plus)
    # pourrait sinon garder CETTE fonction bloquée indéfiniment. Or le
    # chien de garde "aucune progression" de scan_all (voir
    # _SCAN_FFMPEG_TIMEOUT * 3, au moins 45 s) annule alors les tâches
    # restantes en croyant le scan bloqué, alors qu'il est seulement mis en
    # attente au profit de l'utilisateur. On plafonne donc le temps
    # d'attente pour activité générale (jamais pour une lecture vidéo, qui
    # reste prioritaire sans limite) largement en dessous de ce délai : la
    # navigation reste fluide pendant les quelques secondes qui suivent
    # chaque clic, sans jamais pouvoir, même en cas d'usage très intensif,
    # déclencher à tort l'annulation du reste du scan.
    activity_wait_started = None
    ACTIVITY_WAIT_CEILING = 20.0
    while True:
        if pause_for_playback and is_playback_active():
            activity_wait_started = None
            # _PLAYBACK_GATE n'est "clear" (bloquant) QUE pendant une
            # lecture active : .wait() dort réellement jusqu'à la fin de
            # la lecture (ou l'expiration du TTL), sans réveil inutile.
            _PLAYBACK_GATE.wait(0.5)
            continue
        if _app_activity_recent():
            if activity_wait_started is None:
                activity_wait_started = time.time()
            elif time.time() - activity_wait_started >= ACTIVITY_WAIT_CEILING:
                break
            # Ici _PLAYBACK_GATE est "set" (aucune lecture en cours) :
            # .wait() y retournerait immédiatement et transformerait cette
            # boucle en boucle active consommant tout un cœur pour rien.
            # Un vrai sommeil (time.sleep) est nécessaire dans ce cas.
            time.sleep(0.5)
            continue
        break


class _DecodeSlot:
    """Jeton de décodage qui respecte d'abord la priorité lecture vidéo."""

    def __enter__(self):
        _wait_if_playback()
        _DECODE_SLOTS.acquire()
        return self

    def __exit__(self, *exc):
        _DECODE_SLOTS.release()
        return False


_DECODE_GATE = _DecodeSlot()
# Les threads de décodage internes de ffmpeg sont désormais calculés par
# rapport au nombre d'extractions réellement simultanées (et non par rapport
# au nombre total de vidéos en cours, dont la plupart attendent le disque) :
# chaque extraction reçoit donc davantage de cœurs, sans sursouscription.
FFMPEG_THREADS_PER_JOB = max(1, _CPU_BUDGET // _DECODE_BUDGET)
_THREADS_ARGS = ["-threads", str(FFMPEG_THREADS_PER_JOB)]



# ---------------------------------------------------------------------------
# DÉCODAGE ACCÉLÉRÉ PAR LE MATÉRIEL POUR LA GÉNÉRATION DES VIGNETTES
# ---------------------------------------------------------------------------
# Le scan passe l'essentiel de son temps à DÉCODER des images (11 par vidéo :
# la vignette + les aperçus au survol), en logiciel, sur le processeur — d'où
# les cœurs à fond et le PC qui rame pendant un scan, en particulier sur les
# vidéos HEVC/4K, les plus coûteuses à décoder.
#
# "-hwaccel auto" demande à ffmpeg d'utiliser le décodeur matériel de la
# machine quand il en existe un (carte graphique NVIDIA/AMD/Intel, VideoToolbox
# sur Mac, DXVA2/D3D11VA sur Windows) : le décodage bascule alors sur le GPU,
# quasi gratuit pour le processeur. C'est une option de repli automatique et
# silencieuse : si aucun décodeur matériel n'est utilisable pour ce fichier,
# ffmpeg décode en logiciel exactement comme avant, sans erreur ni différence
# sur les images produites.
_HWACCEL_ARGS = ["-hwaccel", "auto"]
HWACCEL_ARGS = _HWACCEL_ARGS

# Choix intelligent de l'accélération matérielle : initialiser le décodeur du
# GPU coûte quelques dizaines de millisecondes PAR IMAGE demandée (11 par
# vidéo). Pour une vidéo légère (H.264 en 1080p ou moins), ce coût de
# démarrage dépasse le gain : le processeur décode l'image plus vite tout
# seul. Pour les formats réellement lourds (HEVC/H.265, AV1, VP9, 4K...) —
# ceux qui font monter les ventilateurs pendant un scan — le GPU est très
# nettement gagnant et libère complètement le processeur. On applique donc
# l'accélération matérielle uniquement là où elle rapporte. En cas
# d'indisponibilité, ffmpeg retombe silencieusement sur le décodage logiciel,
# exactement comme avant : aucun risque d'échec ajouté.
_HEAVY_CODECS = {"hevc", "h265", "av1", "vp9", "vp8", "mpeg2video", "prores", "dnxhd"}


def _hwaccel_args_for(codec_name="", width=0, height=0):
    codec_name = (codec_name or "").lower()
    if codec_name in _HEAVY_CODECS:
        return _HWACCEL_ARGS
    try:
        if int(width or 0) >= 1920 or int(height or 0) >= 1080:
            return _HWACCEL_ARGS
    except (TypeError, ValueError):
        pass
    if not codec_name:
        # Codec inconnu (ffprobe en échec) : on garde le comportement
        # historique, accélération matérielle automatique.
        return _HWACCEL_ARGS
    return []

# ---------------------------------------------------------------------------
# SCAN PLUS RAPIDE À CHARGE CPU ÉGALE (OU MOINDRE)
# ---------------------------------------------------------------------------
# Le coût CPU d'une vignette ne vient pas de l'écriture du JPEG mais du
# DÉCODAGE des images vidéo qui précède. Trois réglages, purement techniques,
# suppriment du décodage inutile sans rien changer au résultat visible :
#
# 1) "-noaccurate_seek" : par défaut, après un saut à l'instant T, ffmpeg
#    décode en silence TOUTES les images depuis l'image-clé précédente
#    jusqu'à T (parfois plusieurs secondes de vidéo décodées pour une seule
#    image conservée). Avec cette option, l'image est prise directement à
#    l'image-clé : quelques dixièmes de seconde d'écart au plus sur la
#    position de la vignette, pour un décodage souvent 5 à 10 fois plus
#    court et donc autant de CPU en moins.
# 2) "-fflags +fastseek" : saut direct dans le fichier, sans relecture
#    d'index inutile.
# 3) "-probesize/-analyzeduration" réduits : ffmpeg n'analyse plus des
#    dizaines de Mo d'en-tête avant de commencer, ce qui est inutile ici
#    (on ne veut qu'une image du flux vidéo principal).
# 4) "-an -sn -dn" : les pistes audio, sous-titres et données ne sont ni
#    décodées ni transportées — elles ne servent à rien pour une vignette.
# Réglages "sûrs" (comportement historique) : conservés comme repli si la
# variante ultra-rapide ci-dessous ne produit pas toutes les images.
_SAFE_INPUT_ARGS = [
    "-noaccurate_seek",
    "-flags2", "+fast",
    "-skip_loop_filter", "all",
    "-thread_type", "frame+slice",
    "-fflags", "+fastseek",
    "-probesize", "1M",
    "-analyzeduration", "1M",
]

# Variante ultra-rapide, mesurée ~3,5x plus rapide (≈70 % de temps en moins)
# sur l'extraction des 11 images d'une vidéo 1080p :
# 5) "-skip_frame nokey" : le décodeur ignore purement et simplement toutes
#    les images non-clés. Comme "-noaccurate_seek" fait déjà atterrir la
#    lecture sur une image-clé, l'image conservée est la même qu'avant, mais
#    ffmpeg ne décode plus aucune image intermédiaire : c'est le gain
#    principal.
# 6) "-analyzeduration 0" + "-probesize 500k" : ffmpeg démarre immédiatement
#    au lieu d'analyser l'en-tête (11 ouvertures de fichier par vidéo, donc
#    11 analyses économisées).
# 7) "-thread_type slice" : pour UNE image, le parallélisme par images
#    n'apporte rien et ajoute de la latence de démarrage à chaque entrée ;
#    le mode par tranches est plus rapide ici.
_FAST_INPUT_ARGS = [
    "-noaccurate_seek",
    "-flags2", "+fast",
    "-skip_loop_filter", "all",
    "-skip_frame", "nokey",
    "-thread_type", "slice",
    "-fflags", "+fastseek",
    "-probesize", "500k",
    "-analyzeduration", "0",
]
_NO_EXTRA_STREAMS = ["-an", "-sn", "-dn"]


# Alias publics (sans le "_" initial) des réglages de priorité/threads
# ci-dessus, pour que d'autres modules (voir _transcode_worker dans app.py,
# conversion de secours à la volée pour le lecteur) puissent lancer leurs
# propres appels ffmpeg avec exactement la même politique — celle qui évite
# de faire ramer le reste du PC — sans dupliquer cette logique. Valeurs
# identiques, seul le nom change ; rien n'est recalculé.
NICE_PREFIX = _NICE_PREFIX
SUBPROCESS_KWARGS = _SUBPROCESS_KWARGS
THREADS_ARGS = _THREADS_ARGS
# Réglage séparé pour la conversion de secours à la volée (app.py) : à la
# différence du scan, où plusieurs vidéos sont traitées en parallèle
# (SCAN_WORKERS) et où les threads ffmpeg sont donc divisés en
# conséquence, une conversion à la volée tourne le plus souvent seule (une
# vidéo en cours de lecture). Limiter quand même à la moitié des cœurs
# logiques (au lieu de les utiliser tous) laisse de la marge au système,
# au navigateur et au serveur Flask lui-même pendant la conversion, tout en
# restant nettement plus rapide qu'une seule vidéo traitée en parallèle
# pendant un scan.
TRANSCODE_THREADS = max(1, _CPU_COUNT // 2)
TRANSCODE_THREADS_ARGS = ["-threads", str(TRANSCODE_THREADS)]

# ---------------------------------------------------------------------------
# PLAFOND CPU HORS-SCAN (NORMAL_USE_CPU_PERCENT dans config.py)
# ---------------------------------------------------------------------------
# Les tâches hors-scan (conversion de secours, pré-transcodage, navigation…)
# ne doivent JAMAIS saturer la machine : la lecture vidéo et le système
# doivent rester fluides. On limite donc ffmpeg à un plafond de cœurs
# calculé à partir de NORMAL_USE_CPU_PERCENT (30 % par défaut) et on
# abaisse la priorité du processus (nice -n 10 sur Linux, BELOW_NORMAL sur
# Windows) pour que le transcode cède immédiatement le CPU à tout ce qui
# est plus urgent (lecteur, navigateur, serveur Flask). Le scan, lui,
# conserve SA propre politique (SCAN_CPU_PERCENT = 100, priorité normale)
# et n'est JAMAIS concerné par ces constantes.
_NORMAL_USE_CPU_PERCENT = float(
    getattr(config, "NORMAL_USE_CPU_PERCENT", 30) or 30
)
_NORMAL_USE_CPU_PERCENT = max(5.0, min(100.0, _NORMAL_USE_CPU_PERCENT))
NORMAL_CPU_BUDGET = max(1, int(round(_CPU_COUNT * _NORMAL_USE_CPU_PERCENT / 100.0)))

# Priorité abaissée pour le hors-scan : nice -n 10 sur Linux,
# BELOW_NORMAL_PRIORITY_CLASS sur Windows.
_NORMAL_NICE_PREFIX = []
_NORMAL_SUBPROCESS_KWARGS = {}
if sys.platform == "win32":
    _NORMAL_SUBPROCESS_KWARGS = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}
else:
    _NORMAL_NICE_PREFIX = ["nice", "-n", "10"]

NORMAL_NICE_PREFIX = _NORMAL_NICE_PREFIX
NORMAL_SUBPROCESS_KWARGS = _NORMAL_SUBPROCESS_KWARGS

# Nombre de threads ffmpeg pour le hors-scan : plafond à NORMAL_CPU_BUDGET
# (30 % des cœurs par défaut). Une seule conversion à la fois en général,
# donc on lui donne tout le budget normal (contrairement au scan où le
# budget est réparti entre SCAN_WORKERS tâches parallèles).
NORMAL_FFMPEG_THREADS = max(1, NORMAL_CPU_BUDGET)
NORMAL_FFMPEG_THREADS_ARGS = ["-threads", str(NORMAL_FFMPEG_THREADS)]

# ---------------------------------------------------------------------------
# CORRECTIF CPU ÉLEVÉ / ARRÊTS PENDANT LA CONVERSION DE SECOURS
# ---------------------------------------------------------------------------
# La conversion de secours (voir _transcode_worker dans app.py, déclenchée
# automatiquement quand une vidéo haute résolution ne se décode plus du tout
# nativement dans le navigateur, ou manuellement via le bouton "Mode
# compatible") encodait jusqu'ici toujours en logiciel (libx264) : sur une
# machine équipée d'une carte graphique récente, cet encodage logiciel
# n'utilise JAMAIS le GPU et peut à lui seul saturer plusieurs cœurs CPU
# pendant toute la conversion — exactement le genre de charge qui, cumulée
# au décodage (lui aussi logiciel) de la vidéo source déjà lourde à lire,
# fait ramer toute la machine.
#
# On détecte ici, une seule fois au démarrage, si un encodeur matériel
# (GPU) est réellement disponible dans ce build de ffmpeg — NVIDIA (nvenc),
# Intel Quick Sync (qsv), AMD (amf) ou Apple (videotoolbox) — sans jamais
# rien changer si aucun n'est présent ou fonctionnel : la conversion se
# comporte alors exactement comme avant (repli automatique sur libx264,
# voir _transcode_worker). Le simple LISTAGE d'un encodeur par ffmpeg ne
# garantit pas qu'il fonctionne réellement sur cette machine précise (GPU
# absent, pilote manquant...) : _transcode_worker doit donc, de son côté,
# retenter automatiquement en logiciel si la tentative matérielle échoue —
# jamais d'échec définitif à cause de ce choix d'optimisation.
# ---------------------------------------------------------------------------
HW_ENCODERS = [
    # (nom de l'encodeur ffmpeg, arguments -hwaccel pour le décodage
    # correspondant — None si aucun accélérateur de décodage fiable et
    # multiplateforme n'est associé à cet encodeur)
    ("h264_nvenc", ["-hwaccel", "cuda"]),
    ("h264_qsv", ["-hwaccel", "qsv"]),
    ("h264_videotoolbox", ["-hwaccel", "videotoolbox"]),
    ("h264_amf", None),
]


def _detect_hw_encoder():
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=8, text=True,
        )
        listed = proc.stdout or ""
    except Exception:
        return None, None
    for name, hwaccel_args in HW_ENCODERS:
        if name in listed:
            return name, hwaccel_args
    return None, None


HW_ENCODER, HW_ENCODER_HWACCEL_ARGS = _detect_hw_encoder()


def probe_video(path):
    """Retourne (duration_seconds, width, height, codec_name, pix_fmt), ou
    des valeurs vides/nulles si échec. codec_name/pix_fmt (ex. "hevc",
    "yuv420p10le") servent uniquement à is_risky_codec() ci-dessous — ffprobe
    est déjà appelé ici pour la durée/résolution, ces deux champs
    supplémentaires ne coûtent donc pas d'appel ffprobe de plus."""
    def _run(probesize, analyzeduration):
        cmd = [
            FFPROBE, "-v", "error",
            "-probesize", probesize, "-analyzeduration", analyzeduration,
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,pix_fmt",
            "-show_entries", "format=duration",
            "-of", "json",
            path,
        ]
        out = subprocess.run(_NICE_PREFIX + cmd, capture_output=True, text=True,
                             timeout=30, **_SUBPROCESS_KWARGS)
        data = json.loads(out.stdout or "{}")
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        streams = data.get("streams", [])
        width = int(streams[0].get("width", 0)) if streams else 0
        height = int(streams[0].get("height", 0)) if streams else 0
        codec_name = (streams[0].get("codec_name") or "") if streams else ""
        pix_fmt = (streams[0].get("pix_fmt") or "") if streams else ""
        return duration, width, height, codec_name, pix_fmt

    try:
        # Analyse minimale d'abord : la durée et la résolution sont écrites
        # dans l'en-tête du conteneur, inutile d'analyser 1 Mo de flux.
        result = _run("500k", "0")
        if not result[0] or not result[1]:
            # En-tête inhabituel (flux sans durée annoncée) : on refait
            # l'analyse complète, exactement comme avant.
            result = _run("1M", "1M")
        return result
    except Exception:
        return 0.0, 0, 0, "", ""



# Codecs/profils connus pour ne pas être décodés en matériel par le
# navigateur sur une bonne partie des machines (GPU trop ancien, pilote
# incomplet...), ce qui force un décodage logiciel lourd en CPU. La liste
# couvre HEVC/H.265 et AV1 quel que soit leur profil, ainsi que tout flux
# 10 bits. Le coût réel dépend du navigateur et de la carte graphique :
# dans le doute, on prépare donc le mode compatible sans l'imposer si une
# conversion n'est pas encore disponible.
# Codecs qui peuvent faire basculer le navigateur en décodage logiciel
# permanent, surtout en 4K/10 bits. AV1 était auparavant absent de cette
# liste : une vidéo AV1 pourtant servie directement pouvait donc consommer
# tous les cœurs avant que l'utilisateur ait la possibilité de demander le
# mode compatible.
_RISKY_CODEC_NAMES = {
    "hevc", "h265", "av1", "vp9", "vp8",
    "mpeg2video", "vc1", "vc1image",
    "prores", "prores_aw", "prores_ks",
    "dnxhd", "dnxhr", "wmv3", "vc-1",
}

# Codecs généralement peu coûteux et correctement décodés par les
# navigateurs modernes. Tout codec inconnu reste volontairement considéré
# comme à risque : mieux vaut préparer un secours compatible que laisser le
# navigateur monopoliser le CPU sans avertissement.
_LIGHT_CODEC_NAMES = {"h264", "avc1", "mpeg4"}


def is_risky_codec(codec_name, pix_fmt, width=0, height=0):
    codec_name = (codec_name or "").lower()
    pix_fmt = (pix_fmt or "").lower()
    if codec_name in _RISKY_CODEC_NAMES:
        return True
    if "10le" in pix_fmt or "10be" in pix_fmt or pix_fmt.endswith("p10"):
        return True
    try:
        # Même un codec normalement léger peut devenir coûteux en 1080p/4K,
        # surtout en 60 fps. On prépare donc un mode compatible pour les
        # grosses sources, sans modifier le fichier original.
        max_dimension = max(int(width or 0), int(height or 0))
        if max_dimension >= 3840 or (
            codec_name not in _LIGHT_CODEC_NAMES and max_dimension >= 1920
        ):
            return True
    except (TypeError, ValueError):
        pass
    if codec_name and codec_name not in _LIGHT_CODEC_NAMES:
        return True
    if not codec_name:
        # Codec non identifié : la compatibilité est inconnue, donc on
        # prépare le fallback plutôt que de risquer un décodage logiciel.
        return True
    return False


def _thumb_file_ok(out_path):
    """Une vignette n'est valable que si le fichier existe ET n'est pas
    vide : ffmpeg crée parfois un JPEG de 0 octet quand le décodage échoue
    en cours de route (cas typique du HEVC/10 bits décodé en matériel sur
    un pilote incomplet)."""
    try:
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
    except OSError:
        return False


# ---------------------------------------------------------------------------
# CORRECTIF CPU APRÈS SCAN D'UNE BIBLIOTHÈQUE DÉJÀ EN PLACE (rescan "à vide")
# ---------------------------------------------------------------------------
# Le cas le plus fréquent d'un scan n'est pas l'ajout de nouvelles vidéos :
# c'est de relancer un scan sur une bibliothèque qui n'a pas changé (juste
# pour vérifier). Pour chaque vidéo DÉJÀ connue et inchangée, la passe
# rapide ci-dessous vérifiait jusqu'ici sa vignette avec deux appels
# système PAR VIDÉO (os.path.exists + os.path.getsize) — donc, par exemple,
# environ 20 000 appels système rien que pour ça sur une bibliothèque de
# 10 000 vidéos, à chaque scan, même quand rien n'a bougé. Ce coût est du
# CPU pur côté système d'exploitation (résolution de chemin, changement de
# contexte noyau) et s'ajoute, non pas au décodage ffmpeg (qui, lui, ne
# tourne toujours QUE pour les vidéos réellement nouvelles/modifiées), mais
# à TOUT scan, y compris — surtout — un scan qui ne trouve au final aucune
# nouveauté : c'est exactement ce qui donnait l'impression que le PC
# continuait à travailler "après" un scan de vidéos déjà en place.
#
# _build_thumbs_index() lit une seule fois, par UN SEUL appel système
# (os.scandir) sur le dossier LOCAL des vignettes (jamais sur les dossiers
# vidéo, potentiellement sur disque externe/NAS), la liste de tous les
# fichiers de vignettes déjà présents avec leur taille. La vérification
# "cette vignette existe-t-elle et n'est-elle pas vide ?" devient ensuite
# une simple consultation en mémoire (dictionnaire Python), pour chacune
# des dizaines de milliers de vidéos déjà indexées : aucun appel système
# supplémentaire, quelle que soit la taille de la bibliothèque. Résultat
# strictement identique à l'ancienne vérification fichier par fichier (même
# critère : présence + taille > 1024 octets) ; seule la façon de
# l'obtenir change.
def _build_thumbs_index():
    """Retourne {nom_de_fichier: taille_en_octets} pour tout le contenu du
    dossier des vignettes, ou None si le dossier est illisible (repli
    automatique sur la vérification fichier par fichier, voir son usage
    dans scan_all)."""
    index = {}
    try:
        with os.scandir(config.THUMBS_DIR) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        index[entry.name] = entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return index


def _thumb_ok_in_index(name, index):
    """Équivalent de _thumb_file_ok(...) mais servi depuis l'index en
    mémoire (voir _build_thumbs_index) plutôt que par un appel système. Se
    replie sur la vérification disque classique si l'index n'a pas pu être
    construit (index is None) ou si le nom en est absent par prudence
    (ex. vignette créée par un autre processus entre-temps) — jamais moins
    fiable que l'ancien comportement, seulement plus rapide dans le cas
    courant."""
    if not name:
        return False
    if index is not None:
        size = index.get(name)
        if size is not None:
            return size > 1024
        return False
    return _thumb_file_ok(os.path.join(config.THUMBS_DIR, name))


def _run_thumb_attempt(path, out_path, ts, hwaccel_args, input_args,
                       accurate=False, scale=640, quality="2", timeout=45):
    """Une tentative d'extraction d'image. `accurate=True` demande un saut
    EXACT (saut approximatif juste avant la position, puis avance précise
    après ouverture) : plus lent, mais c'est le seul mode qui produit une
    image sur les fichiers dont l'index d'images-clés est absent, tronqué
    ou mal écrit — très fréquent sur les .mkv/.mp4 HEVC."""
    try:
        if _thumb_file_ok(out_path):
            return True
        cmd = _NICE_PREFIX + [FFMPEG, "-y"] + list(hwaccel_args) + list(input_args)
        if accurate:
            pre = max(0.0, ts - 3.0)
            cmd += ["-ss", str(pre), "-i", path]
            post = ts - pre
            if post > 0.05:
                cmd += ["-ss", str(post)]
        else:
            cmd += ["-ss", str(ts), "-i", path]
        cmd += _THREADS_ARGS + _NO_EXTRA_STREAMS + [
            "-frames:v", "1", "-q:v", quality,
            # "format=yuvj420p" : conversion explicite vers un format que
            # l'encodeur JPEG accepte toujours. Sans cela, un flux 10 bits
            # (yuv420p10le, très courant en HEVC) fait échouer l'encodage
            # sur certains builds de ffmpeg — d'où des vignettes absentes
            # uniquement sur ces vidéos.
            "-vf", "scale=%d:-2,format=yuvj420p" % scale,
            "-f", "image2", out_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=timeout, **_SUBPROCESS_KWARGS)
        return _thumb_file_ok(out_path)
    except Exception:
        return False


def make_thumbnail(path, out_path, duration, timestamp_setting,
                   scale=640, quality="2"):
    """Génère une vignette JPEG à un instant donné de la vidéo, en essayant
    successivement plusieurs stratégies jusqu'à obtenir une image. Chaque
    repli est plus lent mais plus tolérant que le précédent, et n'est tenté
    QUE si le précédent n'a rien produit : sur l'immense majorité des
    vidéos, seul le premier essai (le plus rapide, identique à avant) est
    exécuté — aucun coût ajouté. Sur les vidéos qui restaient sans vignette
    (typiquement HEVC/H.265, 10 bits, index d'images-clés incomplet), l'un
    des replis finit par en produire une."""
    ts = min(timestamp_setting, max(duration - 1, 0)) if duration else 1
    ts = max(ts, 0)
    mid = (duration / 2.0) if duration else 0

    attempts = [
        # 1) rapide, avec accélération matérielle (comportement historique)
        (ts, _HWACCEL_ARGS, _SAFE_INPUT_ARGS, False),
        # 2) décodage 100 % logiciel : contourne un décodeur GPU défaillant
        (ts, [], _SAFE_INPUT_ARGS, False),
        # 3) saut EXACT en logiciel : contourne un index d'images-clés absent
        (ts, [], _SAFE_INPUT_ARGS, True),
        # 4) au milieu de la vidéo, saut exact (position toujours valide)
        (mid, [], _SAFE_INPUT_ARGS, True),
        # 5) tout premier instant décodable : dernier recours
        (0, [], _SAFE_INPUT_ARGS, False),
    ]
    for att_ts, hw, input_args, accurate in attempts:
        if att_ts is None or att_ts < 0:
            continue
        if _run_thumb_attempt(path, out_path, att_ts, hw, input_args,
                              accurate=accurate, scale=scale, quality=quality):
            return True
    return False


def make_preview_frames(path, thumb_base_noext, duration):
    """Génère jusqu'à PREVIEW_FRAME_COUNT images à intervalles réguliers
    dans la vidéo, pour l'aperçu au survol dans la grille. Retourne le
    nombre d'images réellement générées (0 si la vidéo est trop courte).
    Conservée telle quelle comme repli, image par image (voir
    make_thumbnail_and_previews)."""
    if not duration or duration < config.PREVIEW_MIN_DURATION:
        return 0
    count = 0
    for i, frac in enumerate(config.PREVIEW_FRACTIONS[: config.PREVIEW_FRAME_COUNT], start=1):
        ts = max(0, min(duration * frac, duration - 0.5))
        out_path = os.path.join(config.THUMBS_DIR, f"{thumb_base_noext}_p{i}.jpg")
        try:
            cmd = _NICE_PREFIX + [
                FFMPEG, "-y",
            ] + _HWACCEL_ARGS + _SAFE_INPUT_ARGS + ["-ss", str(ts), "-i", path] + _THREADS_ARGS + _NO_EXTRA_STREAMS + [
                "-frames:v", "1", "-q:v", "3",
                "-vf", "scale=480:-2",
                out_path,
            ]
            subprocess.run(cmd, capture_output=True, timeout=30, **_SUBPROCESS_KWARGS)
            if os.path.exists(out_path):
                count += 1
        except Exception:
            break
    return count


def make_thumbnail_and_previews(path, thumb_out_path, thumb_base_noext, duration, timestamp_setting,
                                codec_name="", width=0, height=0):
    """Génère la vignette principale ET toutes les images d'aperçu au survol
    en UN SEUL processus ffmpeg (au lieu d'un processus séparé par image,
    soit jusqu'à 11 auparavant pour une seule vidéo).

    Chaque image garde exactement sa propre entrée "-ss <t> -i <path>"
    dédiée, donc le même seek rapide (par mots-clés, avant -i) qu'avant,
    image par image : ni la précision, ni la qualité, ni la charge de
    décodage par image ne changent. Seul change le nombre de DÉMARRAGES de
    processus ffmpeg par vidéo (1 au lieu de plusieurs), ce qui accélère
    nettement le scan (surtout sur de grosses bibliothèques et sous
    Windows, où lancer un processus a un coût non négligeable) sans
    consommer plus de CPU : c'est le même travail de décodage, avec
    beaucoup moins d'overhead de lancement/fermeture autour.

    Robustesse : si la commande combinée échoue entièrement (timeout,
    ffmpeg introuvable, exception quelconque), on se replie automatiquement
    sur l'ancienne méthode image par image (make_thumbnail +
    make_preview_frames), strictement identique à avant. Le résultat final
    (quelles images existent réellement sur le disque) est donc au moins
    aussi bon que l'ancien comportement, jamais moins bon.

    Retourne (thumb_ok: bool, preview_count: int).
    """
    ts_thumb = min(timestamp_setting, max(duration - 1, 0)) if duration else 1
    ts_thumb = max(ts_thumb, 0)

    preview_targets = []  # liste de (timestamp, chemin_sortie)
    if duration and duration >= config.PREVIEW_MIN_DURATION:
        for i, frac in enumerate(config.PREVIEW_FRACTIONS[: config.PREVIEW_FRAME_COUNT], start=1):
            ts = max(0, min(duration * frac, duration - 0.5))
            out_path = os.path.join(config.THUMBS_DIR, f"{thumb_base_noext}_p{i}.jpg")
            preview_targets.append((ts, out_path))

    all_targets = [(ts_thumb, thumb_out_path)] + preview_targets

    hwaccel_args = _hwaccel_args_for(codec_name, width, height)

    # Le mode ultra-rapide ("-skip_frame nokey") ne peut produire une image
    # que s'il existe une image-clé après la position demandée. Dans le
    # tout dernier groupe d'images d'un fichier, ce n'est pas garanti : on
    # y utilise donc d'emblée les réglages historiques (une poignée
    # d'images seulement, coût négligeable) plutôt que de payer une
    # deuxième passe ffmpeg complète.
    _late = (duration * 0.8) if duration else 0

    def _args_for(ts):
        return _SAFE_INPUT_ARGS if (_late and ts >= _late) else _FAST_INPUT_ARGS

    def _build_cmd(targets, force_args=None):
        """Commande combinée pour la liste (timestamp, sortie) donnée.
        La vignette principale sort en 640 px (q=2), les aperçus en
        480 px (q=3)."""
        cmd = _NICE_PREFIX + [FFMPEG, "-y"]
        for ts, _out in targets:
            # -threads placé avant chaque -i : limite les threads de
            # DÉCODAGE de cette entrée (voir FFMPEG_THREADS_PER_JOB
            # ci-dessus), pas seulement l'encodage de la vignette JPEG qui
            # suit (négligeable en CPU par rapport au décodage vidéo).
            cmd += _THREADS_ARGS + hwaccel_args + (force_args or _args_for(ts)) + ["-ss", str(ts), "-i", path]
        for idx, (_ts, out_path) in enumerate(targets):
            if out_path == thumb_out_path:
                cmd += _NO_EXTRA_STREAMS + ["-map", f"{idx}:v:0", "-frames:v", "1",
                                            "-q:v", "2", "-vf", "scale=640:-2", out_path]
            else:
                cmd += _NO_EXTRA_STREAMS + ["-map", f"{idx}:v:0", "-frames:v", "1",
                                            "-q:v", "3", "-vf", "scale=480:-2", out_path]
        return cmd


    def _count_results():
        # _thumb_file_ok (et non un simple "le fichier existe") : un JPEG de
        # 0 octet laissé par un décodage matériel qui a échoué en cours de
        # route ne doit surtout pas être considéré comme une vignette
        # valable, sinon la case de la vidéo affiche une image cassée — le
        # symptôme observé sur les vidéos HEVC.
        return (_thumb_file_ok(thumb_out_path),
                sum(1 for _ts, out_path in preview_targets if _thumb_file_ok(out_path)))

    try:
        # Une limite globale par commande évite qu'un seul fichier défectueux
        # retienne les derniers workers et fige la barre vers 97 %.
        timeout_s = _SCAN_FFMPEG_TIMEOUT
        # Un jeton de décodage : au plus _DECODE_BUDGET extractions d'images
        # en même temps sur la machine (voir _DECODE_SLOTS).
        with _DECODE_GATE:
            subprocess.run(_build_cmd(all_targets), capture_output=True,
                           timeout=timeout_s, **_SUBPROCESS_KWARGS)

        thumb_ok, preview_count = _count_results()

        # Filet de sécurité CIBLÉ : le mode ultra-rapide peut, sur certains
        # fichiers, ne pas trouver d'image-clé exploitable tout près de la
        # fin de la vidéo. On refait alors, avec les réglages historiques,
        # UNIQUEMENT les images manquantes (et non les 11) : le gain de
        # vitesse est conservé et le résultat final reste identique à
        # l'ancienne version.
        missing = [t for t in all_targets if not _thumb_file_ok(t[1])]
        if missing:
            try:
                with _DECODE_GATE:
                    subprocess.run(
                        _build_cmd(missing, force_args=_SAFE_INPUT_ARGS),
                        capture_output=True,
                        timeout=max(15, _SCAN_FFMPEG_TIMEOUT // 2),
                        **_SUBPROCESS_KWARGS,
                    )
            except subprocess.TimeoutExpired:
                # Les éventuelles images déjà écrites sont conservées ; on
                # n'enchaîne surtout pas dix nouveaux délais de 30 secondes.
                pass
            thumb_ok, preview_count = _count_results()

        # Repli partiel : si la vignette (la plus importante) n'a pas été
        # produite par la commande combinée, on la régénère seule avec la
        # chaîne de replis de make_thumbnail (décodage logiciel, saut exact,
        # conversion de format explicite) : c'est elle qui récupère les
        # vidéos HEVC/10 bits qui restaient auparavant sans vignette.
        if not thumb_ok:
            with _DECODE_GATE:
                thumb_ok = make_thumbnail(path, thumb_out_path, duration, timestamp_setting)

        # Même logique pour les images d'aperçu au survol encore manquantes,
        # mais UNIQUEMENT si la vidéo est de celles qui ont eu besoin d'un
        # repli (sinon on ne fait rien de plus qu'avant) : une vidéo sans
        # aucun aperçu perdait tout son défilement au survol.
        still_missing = [t for t in preview_targets if not _thumb_file_ok(t[1])]
        if thumb_ok and still_missing and preview_count == 0:
            for ts_prev, out_prev in still_missing:
                with _DECODE_GATE:
                    if make_thumbnail(path, out_prev, duration, ts_prev,
                                      scale=480, quality="3"):
                        preview_count += 1

        return thumb_ok, preview_count
    except subprocess.TimeoutExpired:
        # Un timeout signifie généralement un média incomplet, un disque
        # réseau momentanément lent ou un décodeur bloqué. Le fichier sera
        # indexé sans vignette manquante et pourra être retraité au prochain
        # scan, sans empêcher tous les autres d'atteindre 100 %.
        return _count_results()
    except Exception:
        # Repli total : comportement 100% identique à l'ancienne version.
        thumb_ok = make_thumbnail(path, thumb_out_path, duration, timestamp_setting)
        preview_count = make_preview_frames(path, thumb_base_noext, duration)
        return thumb_ok, preview_count


def compute_content_hash(path, size_bytes):
    """Empreinte rapide du contenu réel du fichier, SANS le lire en entier
    (beaucoup trop lent pour de gros fichiers vidéo) : combine la taille
    exacte avec un hash des tout premiers et tout derniers octets. Deux
    vidéos qui partagent cette empreinte sont, en pratique, un seul et même
    fichier physique dupliqué (copie bit à bit) — même si leurs noms de
    fichiers sont totalement différents. C'est cette empreinte, et non le
    nom, qui sert à repérer et numéroter les doublons dans l'appli."""
    # 1 Mo à chaque extrémité (au lieu de 4) : combiné à la taille exacte du
    # fichier, c'est tout aussi fiable en pratique pour reconnaître une copie
    # bit à bit, mais 4 fois moins d'octets lus par vidéo — la lecture disque
    # étant l'un des postes les plus lents du scan sur disque dur ou dossier
    # réseau.
    CHUNK = 1024 * 1024
    try:
        h = hashlib.sha1()
        h.update(str(size_bytes).encode("utf-8"))
        with open(path, "rb") as f:
            h.update(f.read(CHUNK))
            if size_bytes and size_bytes > CHUNK:
                f.seek(max(0, size_bytes - CHUNK))
                h.update(f.read(CHUNK))
        return h.hexdigest()
    except Exception:
        return None


def _process_file(task):
    """Travail lourd (ffprobe + vignette + aperçus) exécuté en parallèle,
    sans toucher à la base de données (chaque thread ne fait que des appels
    ffmpeg/ffprobe, qui libèrent le GIL Python pendant l'exécution — donc
    plusieurs vidéos sont traitées en même temps, ce qui accélère beaucoup
    l'indexation de grosses bibliothèques)."""
    _wait_if_playback()
    folder_root, path, thumbnail_time_seconds, existing_thumb = task[:4]
    if len(task) >= 6:
        # Taille/date déjà obtenues pendant le listage (voir
        # _iter_video_files) : inutile de réinterroger le disque.
        size_bytes, mtime = task[4], task[5]
    else:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        size_bytes = stat.st_size
        mtime = stat.st_mtime
    duration, width, height, codec_name, pix_fmt = probe_video(path)
    codec_risky = 1 if is_risky_codec(codec_name, pix_fmt, width, height) else 0
    filename = os.path.basename(path)
    content_hash = compute_content_hash(path, size_bytes)

    thumb_name = existing_thumb or f"thumb_{abs(hash(path))}.jpg"
    thumb_path = os.path.join(config.THUMBS_DIR, thumb_name)
    thumb_base_noext = os.path.splitext(thumb_name)[0]
    # Vignette + aperçus en un seul appel ffmpeg (voir
    # make_thumbnail_and_previews) : nettement plus rapide qu'un appel par
    # image, à résultat et charge CPU par image identiques.
    _thumb_ok, preview_count = make_thumbnail_and_previews(
        path, thumb_path, thumb_base_noext, duration, thumbnail_time_seconds,
        codec_name=codec_name, width=width, height=height,
    )

    return {
        "folder_root": folder_root, "path": path, "filename": filename,
        "size_bytes": size_bytes, "mtime": mtime, "duration": duration,
        "width": width, "height": height,
        # Aucune image produite malgré tous les replis : on n'enregistre PAS
        # un nom de vignette qui ne correspond à aucun fichier, sinon la
        # grille affiche une image cassée au lieu du visuel de repli. Le
        # prochain scan retentera automatiquement (voir scan_all, qui
        # reprend toute vidéo dont le fichier de vignette est absent).
        "thumb_name": (thumb_name if (_thumb_ok or _thumb_file_ok(thumb_path)) else None),
        "preview_count": preview_count, "content_hash": content_hash,
        "video_codec": codec_name, "codec_risky": codec_risky,
    }


def _iter_video_files(root):
    """Parcourt récursivement un dossier et retourne (chemin, stat) pour
    chaque vidéo trouvée. Utilise os.scandir : la taille et la date de
    modification sont fournies DIRECTEMENT par le parcours du dossier
    (elles sont déjà lues par le système à ce moment-là), ce qui évite un
    accès disque supplémentaire par fichier ensuite. Sur une bibliothèque
    de plusieurs milliers de vidéos, en particulier sur disque dur ou
    dossier réseau, c'est la phase de listage qui devient plusieurs fois
    plus rapide — sans le moindre CPU en plus."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if os.path.splitext(entry.name)[1].lower() not in config.VIDEO_EXTENSIONS:
                    continue
                st = entry.stat()
                if st.st_size == 0:
                    # Fichier de 0 octet : téléchargement/synchronisation
                    # encore en cours (ex. fichier "en ligne uniquement"
                    # d'un client cloud comme OneDrive/Google Drive tant
                    # qu'il n'a pas encore de contenu local), copie tout
                    # juste démarrée, ou fichier corrompu/vidé. ffprobe ne
                    # peut de toute façon jamais rien en tirer : indexer
                    # quand même produirait une vidéo sans durée, sans
                    # dimensions et sans vignette (case "cassée" dans la
                    # grille) qui resterait ainsi tant que personne ne
                    # relance manuellement un scan. On l'ignore simplement
                    # pour cette passe — si le fichier était déjà connu, la
                    # réconciliation "manquant" ci-dessous le retire
                    # proprement de la grille (comme n'importe quel fichier
                    # introuvable) ; dès qu'il a un contenu réel (taille non
                    # nulle) lors d'un prochain scan, il est retraité
                    # normalement et récupère sa vignette automatiquement.
                    continue
                yield entry.path, st
            except OSError:
                continue


def scan_all(folders, thumbnail_time_seconds=10, mark_missing=True):
    """Scan complet, synchrone. À appeler dans un thread séparé depuis Flask.
    Le travail ffmpeg/ffprobe est parallélisé (plusieurs vidéos à la fois)
    pour accélérer nettement l'indexation de plusieurs milliers de fichiers.
    """
    if _status["running"]:
        return
    _set_status(
        running=True, phase="listing", total_found=0, processed=0,
        new=0, updated=0, current_file="", error="", started_at=time.time(),
        finished_at=0,
    )
    conn = db.get_db()
    try:
        # Listage des dossiers configurés EN PARALLÈLE (un thread par
        # dossier) : le parcours de répertoires est presque uniquement de
        # l'attente disque, quasiment aucun CPU. Sur plusieurs dossiers
        # (a fortiori sur disque dur ou lecteur réseau), les attentes se
        # recouvrent au lieu de s'additionner.
        roots = [f for f in folders if f and os.path.isdir(f)]
        all_files = []
        if roots:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(roots)) as lister:
                for folder, found in zip(
                    roots,
                    lister.map(lambda r: list(_iter_video_files(r)), roots),
                ):
                    all_files.extend((folder, f, st) for f, st in found)

        _set_status(phase="processing", total_found=len(all_files))
        seen_paths = {p for _f, p, _st in all_files}

        # On marque les vidéos disparues comme 'manquantes' DÈS MAINTENANT
        # (avant de traiter les nouveaux fichiers), pour que la
        # réconciliation puisse les retrouver même si le déplacement a eu
        # lieu et est scanné en une seule fois (cas le plus courant : on
        # déplace des fichiers puis on relance un seul scan).
        newly_missing = 0
        if mark_missing:
            all_rows = conn.execute("SELECT id, path FROM videos WHERE missing = 0").fetchall()
            for r in all_rows:
                if r["path"] not in seen_paths:
                    conn.execute("UPDATE videos SET missing=1 WHERE id=?", (r["id"],))
                    newly_missing += 1
            if newly_missing:
                conn.commit()

        tasks = []

        # Index des vignettes déjà présentes sur le disque, construit UNE
        # SEULE FOIS pour tout le scan (voir _build_thumbs_index ci-dessus) :
        # remplace, pour chaque vidéo déjà connue et inchangée, deux appels
        # système par un simple accès mémoire — le gain principal sur un
        # rescan d'une bibliothèque déjà en place, où la quasi-totalité des
        # vidéos passent par cette vérification.
        thumbs_index = _build_thumbs_index()

        # Passe rapide et séquentielle : on écarte tout de suite les
        # fichiers déjà indexés et inchangés (aucun appel ffmpeg requis).
        # Toutes les vidéos déjà connues sont chargées en UNE seule requête,
        # puis comparées en mémoire. Auparavant, une requête SQL était
        # exécutée par fichier trouvé (soit des milliers d'allers-retours
        # avec la base pour un scan qui, la plupart du temps, ne trouve
        # aucune nouveauté) : c'est ce qui rendait un "scan à vide" long
        # alors qu'aucune vignette n'était à générer. Résultat identique,
        # simplement obtenu d'un coup.
        known = {
            r["path"]: r
            for r in conn.execute(
                "SELECT id, path, size_bytes, mtime, thumbnail, missing FROM videos"
            ).fetchall()
        }

        unchanged = 0
        relocated_fast = 0
        for folder_root, path, stat in all_files:
            row = known.get(path)
            # Une vidéo déjà indexée est ignorée SAUF si sa vignette manque
            # réellement sur le disque (vignette jamais produite, fichier
            # supprimé...) : ces vidéos-là — typiquement les HEVC qui
            # échouaient avant — sont retraitées au prochain scan pour
            # récupérer leur vignette, sans jamais retoucher les autres.
            thumb_present = True
            if row:
                thumb_present = _thumb_ok_in_index(row["thumbnail"], thumbs_index)
            if (row and thumb_present and row["size_bytes"] == stat.st_size
                    and abs(row["mtime"] - stat.st_mtime) < 1 and row["missing"] == 0):
                unchanged += 1
                continue

            # RECONNAISSANCE RAPIDE D'UN FICHIER DÉPLACÉ (changement de
            # dossier ou de lettre de lecteur, SANS renommage) : avant de
            # lancer le traitement lourd ffmpeg (ffprobe + vignette +
            # aperçus) réservé aux fichiers vraiment nouveaux, on regarde
            # si ce chemin inconnu correspond en fait à une vidéo déjà
            # connue marquée "manquante", via nom de fichier + taille
            # exacts — un simple repli en mémoire/SQL, sans lire le
            # contenu du fichier ni lancer un seul appel ffmpeg. Si c'est
            # le cas, sans la moindre ambiguïté (un seul candidat) et que
            # sa vignette existe toujours sur le disque, on met à jour son
            # chemin directement ici (voir db.relocate_video : conserve
            # vignette, tags, playlists, vues, catégorie) et on passe au
            # fichier suivant — exactement aussi rapide qu'un fichier
            # inchangé. Ne s'applique qu'aux chemins totalement inconnus
            # (row is None) : un chemin déjà connu mais dont la taille ou
            # la date a changé n'est jamais un déplacement, il continue de
            # suivre le traitement lourd normal ci-dessous.
            if row is None:
                filename = os.path.basename(path)
                cheap_candidates = db.find_missing_candidates(filename, stat.st_size)
                if len(cheap_candidates) == 1:
                    candidate = cheap_candidates[0]
                    thumb_row = conn.execute(
                        "SELECT thumbnail FROM videos WHERE id = ?", (candidate["id"],)
                    ).fetchone()
                    candidate_thumb_ok = bool(thumb_row) and _thumb_ok_in_index(
                        thumb_row["thumbnail"], thumbs_index
                    )
                    if candidate_thumb_ok:
                        try:
                            content_hash = compute_content_hash(path, stat.st_size)
                        except Exception:
                            content_hash = None
                        db.relocate_video(
                            candidate["id"], path, folder_root, stat.st_mtime,
                            content_hash=content_hash,
                        )
                        relocated_fast += 1
                        continue
                    # Vignette manquante sur le disque pour ce candidat
                    # (cas rare) : on ne peut pas se contenter du repli
                    # rapide, direction le traitement lourd normal
                    # ci-dessous pour la régénérer.

            existing_thumb = row["thumbnail"] if row else None
            tasks.append((folder_root, path, thumbnail_time_seconds, existing_thumb,
                          stat.st_size, stat.st_mtime))
        if unchanged:
            _set_status(processed=_status["processed"] + unchanged)
        if relocated_fast:
            conn.commit()
            _set_status(
                processed=_status["processed"] + relocated_fast,
                updated=_status["updated"] + relocated_fast,
            )


        # CORRECTIF RALENTISSEMENT EN FIN DE SCAN (~90 %) : deux causes
        # cumulées expliquaient la perte de vitesse observée en fin de
        # scan, corrigées ensemble ici.
        #
        # 1) Effet d'optique en début de scan : les vidéos déjà indexées et
        #    INCHANGÉES (aucun ffmpeg à relancer, voir la boucle
        #    ci-dessus) sont créditées d'un coup, en une fraction de
        #    seconde, avant même que la passe lourde ne démarre. Sur une
        #    bibliothèque de 1000 vidéos dont 900 déjà connues et 100
        #    nouvelles, la barre saute quasi instantanément à 90 % — puis
        #    la vraie extraction ffmpeg (vignette + aperçus, seule tâche
        #    coûteuse en CPU) commence pour les 100 restantes. Ce n'est pas
        #    un ralentissement : c'est le moment où le travail réel
        #    commence enfin, après un crédit "gratuit" qui n'en était pas
        #    un. Rien à corriger ici, c'est inhérent au principe même du
        #    scan incrémental (ne jamais retraiter ce qui n'a pas changé).
        #
        # 2) Effet de fin de file (bien réel, celui-ci) : avec SCAN_WORKERS
        #    tâches traitées en parallèle, le parallélisme retombe
        #    mécaniquement à 1 seul fichier à la fois dès qu'il reste moins
        #    de tâches que de workers disponibles — c'est inévitable pour
        #    n'importe quel traitement par lots parallèle. Le problème est
        #    que les vidéos les plus récemment ajoutées (souvent les plus
        #    volumineuses : 4K/HEVC) se retrouvent généralement groupées en
        #    fin de liste (ordre du système de fichiers), pile au moment où
        #    ce parallélisme s'effondre : la toute fin du scan traite alors
        #    en quasi-série les fichiers les plus lents à décoder, ce qui
        #    donne exactement l'impression d'un ralentissement brutal vers
        #    90 %. En triant les tâches restantes par taille DÉCROISSANTE
        #    avant de les soumettre au pool (les plus gros fichiers en
        #    premier, ordonnancement "LPT" classique pour minimiser le
        #    temps total d'un traitement parallèle), les fichiers coûteux
        #    sont traités en tout début de passe lourde — quand le
        #    parallélisme est maximal — et ce sont les petits fichiers,
        #    rapides à traiter même un par un, qui composent la toute fin
        #    du scan. Résultat identique (mêmes vignettes produites),
        #    seul l'ORDRE de traitement change ; le temps total du scan
        #    diminue et la fin de barre ne s'effondre plus.
        tasks.sort(key=lambda t: t[4], reverse=True)

        # Passe lourde, parallélisée : ffprobe + vignette + aperçus.
        # Plafonné à la moitié des cœurs logiques : au-delà, le scan ne va
        # pas plus vite (le disque et le décodage saturent) mais le reste du
        # PC devient inutilisable pendant toute sa durée.
        max_workers = min(SCAN_WORKERS, max(1, len(tasks)))
        pending_commits = 0
        if tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_process_file, t): t for t in tasks}
                # CORRECTIF SCAN QUI SE FIGE SUR LES TRÈS GROSSES
                # BIBLIOTHÈQUES (dizaines de milliers de vidéos et plus) :
                # l'ancienne boucle rappelait concurrent.futures.wait(...)
                # sur l'ENSEMBLE des tâches encore en cours à CHAQUE fichier
                # terminé. Chaque appel réinscrit un "waiter" sur TOUTES les
                # tâches restantes (même celles qui ne viennent pas de se
                # terminer) avant de les désinscrire aussitôt après : sur
                # une bibliothèque de quelques centaines de vidéos, ce
                # surcoût de comptabilité interne est invisible, mais il
                # croît avec le CARRÉ du nombre de fichiers (coût total
                # proportionnel à N x N au lieu de N, N étant le nombre de
                # vidéos à traiter). Au-delà de plusieurs dizaines de
                # milliers de vidéos, ce seul surcoût finissait par
                # dépasser largement le temps de traitement réel
                # (ffmpeg/ffprobe) : le scan semblait "figé", n'avançait
                # plus, alors que le processus était seulement occupé, en
                # boucle, à réinscrire des dizaines de milliers de callbacks
                # pour chaque fichier terminé.
                #
                # Remplacé par un callback ("add_done_callback"), posé UNE
                # SEULE FOIS par tâche au moment de sa soumission : chaque
                # tâche prévient elle-même une file d'attente (`done_q`) dès
                # qu'elle se termine, sans jamais retoucher aux tâches
                # encore en cours. Coût total proportionnel au nombre de
                # fichiers (N), quelle que soit la taille de la
                # bibliothèque — plus aucun palier au-delà duquel le scan
                # ralentit anormalement. Mêmes vignettes, mêmes métadonnées,
                # même détection de blocage (aucune progression pendant
                # _SCAN_FFMPEG_TIMEOUT * 3 secondes -> annulation des tâches
                # pas encore démarrées) : seule la mécanique d'attente
                # change.
                done_q = queue.SimpleQueue()
                for _fut in futures:
                    _fut.add_done_callback(done_q.put)
                remaining = set(futures.keys())
                while remaining:
                    try:
                        future = done_q.get(timeout=_SCAN_FFMPEG_TIMEOUT * 3)
                    except queue.Empty:
                        # Délai expiré sans la moindre progression : on
                        # annule les tâches restantes (celles pas encore
                        # démarrées ; celles déjà en cours de décodage ne
                        # peuvent pas être interrompues en route, exactement
                        # comme avant) pour que le scan ne reste jamais
                        # bloqué indéfiniment.
                        for f in remaining:
                            f.cancel()
                        _set_status(processed=_status["processed"] + len(remaining))
                        remaining.clear()
                        break
                    remaining.discard(future)
                    try:
                        result = future.result()
                    except (concurrent.futures.CancelledError, Exception):
                        result = None
                    if not result:
                        _set_status(processed=_status["processed"] + 1)
                        continue
                    _set_status(current_file=result["path"])

                    row = conn.execute(
                        "SELECT id FROM videos WHERE path = ?", (result["path"],)
                    ).fetchone()

                    if not row:
                        # Cherche si ce fichier est une vidéo connue mais
                        # déplacée (autre dossier ou lettre de lecteur).
                        candidates = db.find_missing_candidates(
                            result["filename"], result["size_bytes"], result["duration"],
                            width=result["width"], height=result["height"],
                        )
                        if len(candidates) == 1:
                            candidate_id = candidates[0]["id"]
                            # Puisque ce fichier n'a pas été reconnu par son
                            # ancien chemin (row is None ci-dessus), _process_file
                            # vient de générer une vignette neuve sous un nom
                            # dérivé du NOUVEAU chemin (voir plus haut,
                            # existing_thumb=None dans ce cas). Si l'ancienne
                            # vignette de ce candidat est toujours valide sur le
                            # disque, elle reste la référence (on ignore la
                            # neuve, économie d'espace) ; sinon (cas rare :
                            # vignette disparue entre deux scans) on bascule sur
                            # la neuve pour que la base ne pointe jamais vers un
                            # fichier de vignette inexistant.
                            old_thumb_row = conn.execute(
                                "SELECT thumbnail FROM videos WHERE id = ?", (candidate_id,)
                            ).fetchone()
                            old_thumb_ok = bool(old_thumb_row) and _thumb_ok_in_index(
                                old_thumb_row["thumbnail"], thumbs_index
                            )
                            db.relocate_video(
                                candidate_id, result["path"],
                                result["folder_root"], result["mtime"],
                                content_hash=result["content_hash"],
                                thumbnail=None if old_thumb_ok else result["thumb_name"],
                                preview_count=None if old_thumb_ok else result["preview_count"],
                            )
                            _set_status(updated=_status["updated"] + 1)
                            _set_status(processed=_status["processed"] + 1)
                            continue
                        title_base = os.path.splitext(result["filename"])[0]
                        title = db.make_unique_title(title_base)
                        # "Date de la vidéo" : pré-remplie automatiquement à
                        # partir du "Modifié le" de l'explorateur Windows
                        # pour toute nouvelle vidéo (le champ démarre vide,
                        # donc jamais d'écrasement possible ici). Reste
                        # ensuite 100% modifiable normalement par l'utilisateur.
                        auto_video_date = mtime_to_date_str(result["mtime"])
                        conn.execute(
                            """INSERT INTO videos
                               (path, folder_root, filename, title, duration_seconds,
                                width, height, size_bytes, mtime, thumbnail, preview_count,
                                content_hash, video_date, video_codec, codec_risky)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (result["path"], result["folder_root"], result["filename"], title,
                             result["duration"], result["width"], result["height"],
                             result["size_bytes"], result["mtime"], result["thumb_name"],
                             result["preview_count"], result["content_hash"], auto_video_date,
                             result["video_codec"], result["codec_risky"]),
                        )
                        _set_status(new=_status["new"] + 1)
                    else:
                        # video_date n'est complétée automatiquement que si
                        # elle est encore vide : une date déjà saisie ou
                        # modifiée manuellement par l'utilisateur n'est
                        # jamais touchée (CASE ... ELSE video_date).
                        auto_video_date = mtime_to_date_str(result["mtime"])
                        conn.execute(
                            """UPDATE videos SET size_bytes=?, mtime=?, duration_seconds=?,
                               width=?, height=?, thumbnail=?, preview_count=?, missing=0,
                               content_hash=?, video_codec=?, codec_risky=?,
                               video_date=(CASE WHEN video_date IS NULL OR video_date=''
                                           THEN ? ELSE video_date END)
                               WHERE id=?""",
                            (result["size_bytes"], result["mtime"], result["duration"],
                             result["width"], result["height"], result["thumb_name"],
                             result["preview_count"], result["content_hash"],
                             result["video_codec"], result["codec_risky"],
                             auto_video_date, row["id"]),
                        )
                        _set_status(updated=_status["updated"] + 1)

                    # Enregistrement groupé : SQLite écrit sur le disque
                    # (et attend la confirmation matérielle) à chaque
                    # commit. Grouper par paquets de 50 vidéos supprime
                    # l'essentiel de ces attentes sur une grosse
                    # bibliothèque, pour des données strictement
                    # identiques ; un commit final ci-dessous garantit que
                    # rien n'est jamais perdu.
                    pending_commits += 1
                    if pending_commits >= 50:
                        conn.commit()
                        pending_commits = 0
                    _set_status(processed=_status["processed"] + 1)

        if pending_commits:
            conn.commit()
            pending_commits = 0

        # CORRECTIF CPU APRÈS SCAN D'UNE BIBLIOTHÈQUE DÉJÀ EN PLACE (suite) :
        # ces deux opérations n'ont de sens QUE si le scan a réellement
        # changé quelque chose (nouvelle vidéo, vidéo mise à jour, ou vidéo
        # devenue manquante) — jamais si la bibliothèque était déjà
        # entièrement à jour. Auparavant, les deux tournaient
        # INCONDITIONNELLEMENT à la fin de CHAQUE scan, y compris un simple
        # "vérifier s'il y a du nouveau" qui ne trouve rien : c'est la
        # dernière fraction de charge CPU qui persistait après un scan de
        # vidéos déjà en place, une fois la vérification des vignettes
        # elle-même passée en mémoire ci-dessus (_build_thumbs_index).
        any_change = bool(_status["new"] or _status["updated"] or newly_missing)

        if any_change:
            # CORRECTIF "Disques actifs" : une vidéo tout juste INSÉRÉE
            # (nouveau fichier trouvé pendant ce scan) part toujours avec
            # disk_off=0 par défaut (valeur par défaut de la colonne), et
            # une vidéo RELOCALISÉE (db.relocate_video, déplacement détecté
            # par empreinte de contenu) conserve l'ancien disk_off de sa
            # ligne existante — dans les deux cas, sans lien avec l'état
            # actuel de "Disques actifs" (Réglages). Un disque décoché AVANT
            # ce scan pouvait donc se retrouver avec des vidéos de nouveau
            # lisibles juste après un scan, sans que personne n'ait recoché
            # quoi que ce soit — le réglage semblait "se réinitialiser tout
            # seul". On réapplique donc le filtre de disques courant à
            # TOUTES les vidéos juste après le scan, exactement comme le
            # fait la case à cocher elle-même (voir db.set_disabled_folders
            # et app.py:set_disk_filter) : résultat identique, que la vidéo
            # existait déjà ou vienne d'apparaître/se déplacer.
            try:
                disabled_folders = config.load_settings().get("disabled_folders", [])
                db.set_disabled_folders(disabled_folders)
            except Exception:
                pass

            # Une fois toutes les empreintes de contenu connues pour les
            # fichiers traités dans ce scan, on numérote (ou complète la
            # numérotation) des groupes de vidéos strictement identiques en
            # contenu mais nommées différemment : 'N°1 ...', 'N°2 ...', etc.
            # Purement additif : ne touche à rien d'autre.
            try:
                db.reconcile_duplicate_titles()
            except Exception:
                pass

            # Réattribue les métadonnées d'un import JSON encore en attente
            # (vidéos absentes au moment de l'import, ou chemin différent).
            # Indépendant du chemin : content_hash / nom / taille.
            try:
                db.apply_pending_library_metadata(conn=conn)
            except Exception:
                pass

            # Met à jour les statistiques internes de SQLite (utilisées par
            # son planificateur de requêtes pour choisir les index à
            # utiliser) juste après un scan qui a réellement fait évoluer la
            # bibliothèque. Rapide (quelques ms) et sans effet sur les
            # données ; inutile de le refaire à chaque rescan à vide.
            try:
                conn.execute("PRAGMA optimize")
            except Exception:
                pass
        else:
            # Même sans "nouveau fichier", un content_hash vient peut-être
            # d'être calculé (backfill) — on retente la file d'attente.
            try:
                db.apply_pending_library_metadata(conn=conn)
            except Exception:
                pass

        _set_status(phase="done", running=False, finished_at=time.time(), current_file="")
    except Exception as e:
        # Filet de sécurité pour l'enregistrement groupé : tout travail déjà
        # effectué avant l'erreur est conservé en base.
        try:
            conn.commit()
        except Exception:
            pass
        _set_status(phase="error", running=False, error=str(e), finished_at=time.time())


def backfill_content_hashes():
    """Calcule l'empreinte de contenu (content_hash) des vidéos déjà
    indexées AVANT l'ajout de cette fonctionnalité (donc encore sans
    empreinte) — SANS relancer ffmpeg/ffprobe ni régénérer de vignettes :
    seulement une lecture rapide de quelques Mo en début/fin de chaque
    fichier. Une fois les empreintes calculées, les doublons de contenu
    déjà présents dans la bibliothèque (même vidéo sous des noms
    différents) sont automatiquement repérés et numérotés dans l'appli.
    Conçu pour tourner une seule fois en tâche de fond, de façon
    totalement transparente (voir start_backfill_content_hashes_async)."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, path, size_bytes FROM videos
           WHERE content_hash IS NULL OR content_hash = ''"""
    ).fetchall()
    changed = False
    for r in rows:
        if not r["path"] or not os.path.exists(r["path"]):
            continue
        h = compute_content_hash(r["path"], r["size_bytes"])
        if h:
            conn.execute("UPDATE videos SET content_hash=? WHERE id=?", (h, r["id"]))
            changed = True
    if changed:
        conn.commit()
        db.reconcile_duplicate_titles()
        try:
            db.apply_pending_library_metadata(conn=conn)
        except Exception:
            pass


def backfill_video_dates():
    """Pour les vidéos déjà indexées AVANT l'ajout de cette fonctionnalité
    (donc avec 'Date de la vidéo' encore vide), la pré-remplit à partir du
    mtime déjà stocké en base (identique au 'Modifié le' de l'explorateur
    Windows) — sans aucun accès disque, sans relancer ffmpeg/ffprobe.
    Ne touche jamais une date déjà renseignée manuellement."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, mtime FROM videos WHERE video_date IS NULL OR video_date = ''"
    ).fetchall()
    changed = False
    for r in rows:
        date_str = mtime_to_date_str(r["mtime"])
        if date_str:
            conn.execute(
                """UPDATE videos SET video_date=?
                   WHERE id=? AND (video_date IS NULL OR video_date='')""",
                (date_str, r["id"]),
            )
            changed = True
    if changed:
        conn.commit()


def backfill_risky_codecs():
    """Pour les vidéos déjà indexées AVANT l'ajout de la détection de codec
    à risque (donc encore avec video_codec vide), lance un ffprobe léger
    (pas de vignette, pas de hash) pour renseigner video_codec/codec_risky.
    Conçu pour tourner une seule fois en tâche de fond (voir
    start_backfill_risky_codecs_async), sans jamais relancer de génération
    de vignette ni de calcul d'empreinte."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, path FROM videos
           WHERE missing = 0 AND (video_codec IS NULL OR video_codec = '')"""
    ).fetchall()
    changed = False
    for r in rows:
        if not r["path"] or not os.path.exists(r["path"]):
            continue
        _duration, _w, _h, codec_name, pix_fmt = probe_video(r["path"])
        # CORRECTIF "gel de quelques secondes sur codec lourd" (bibliothèque
        # scannée AVANT l'ajout de codec_risky) : quand ffprobe échoue ou
        # n'aboutit pas (fichier volumineux/HEVC 10 bits justement lent à
        # analyser, en-tête abîmé, disque réseau lent...), codec_name revient
        # vide. Cette passe l'ignorait alors complètement avec un `continue`
        # : la ligne restait avec codec_risky=0 (valeur par défaut de la
        # migration, voir db.py) — soit exactement l'inverse du choix fait
        # partout ailleurs. Au moment du scan initial d'une vidéo neuve
        # (voir plus haut dans ce fichier), is_risky_codec() traite déjà un
        # codec_name vide comme À RISQUE par prudence (mieux vaut préparer
        # un secours inutilement que laisser passer un vrai codec lourd).
        # Cette passe de rattrapage doit suivre exactly la même règle,
        # sinon toute vidéo ancienne dont l'analyse échoue reste
        # indéfiniment non protégée et gèle en lecture, précisément le
        # symptôme que codec_risky a été introduit pour éliminer. On ne
        # "continue" donc plus jamais : si codec_name est vide,
        # is_risky_codec(...) renvoie True et la vidéo est marquée à
        # risque, comme un scan classique l'aurait fait.
        risky = 1 if is_risky_codec(codec_name, pix_fmt, _w, _h) else 0
        conn.execute(
            "UPDATE videos SET video_codec=?, codec_risky=? WHERE id=?",
            (codec_name, risky, r["id"]),
        )
        changed = True
    if changed:
        conn.commit()


def start_backfill_risky_codecs_async():
    """Lance backfill_risky_codecs() dans un thread séparé, sans jamais
    bloquer le démarrage de l'application."""
    t = threading.Thread(target=backfill_risky_codecs, daemon=True)
    t.start()


def start_backfill_video_dates_async():
    """Lance backfill_video_dates() dans un thread séparé, sans jamais
    bloquer le démarrage de l'application."""
    t = threading.Thread(target=backfill_video_dates, daemon=True)
    t.start()


def start_backfill_content_hashes_async():
    """Lance backfill_content_hashes() dans un thread séparé, pour ne
    jamais bloquer le démarrage de l'application (bibliothèques de
    plusieurs milliers de vidéos)."""
    t = threading.Thread(target=backfill_content_hashes, daemon=True)
    t.start()


def start_scan_async(folders, thumbnail_time_seconds=10):
    if _status["running"]:
        return False
    t = threading.Thread(
        target=scan_all, args=(folders, thumbnail_time_seconds), daemon=True
    )
    t.start()
    return True
