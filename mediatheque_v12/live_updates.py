# -*- coding: utf-8 -*-
"""
Diffusion en temps réel (Server-Sent Events).

Chaque fois qu'une donnée de l'application change quelque part (édition de
métadonnées, note, tags, catégories, playlists, suppression, scan qui
détecte de nouvelles vidéos, paramètres...), tous les onglets/fenêtres du
navigateur actuellement ouverts sur l'application en sont informés
instantanément via ce canal, et rafraîchissent d'eux-mêmes ce qu'ils
affichent (voir static/js/live-sync.js) — sans qu'il soit jamais nécessaire
de retaper/recharger manuellement l'adresse dans le navigateur.

Ce module ne modifie et ne connaît RIEN de la logique métier : il se
contente d'incrémenter un compteur de version et de le transmettre à des
abonnés. bump() peut être appelée depuis n'importe où (route Flask, tâche de
scan en arrière-plan...) sans aucune dépendance circulaire.
"""
import json
import queue
import threading
import time

_lock = threading.Lock()
_subscribers = set()
_version = 0
# Vrai pendant qu'un scan de bibliothèque est en cours (mis à jour par
# watch_scanner ci-dessous) : permet à live-sync.js d'espacer davantage ses
# rafraîchissements pendant un scan (potentiellement des dizaines de
# nouvelles vidéos détectées coup sur coup), pour ne pas surcharger le
# serveur avec une page complète re-générée à chaque nouvelle vidéo
# trouvée, ce qui ralentissait perceptiblement toute l'application pendant
# qu'un scan tournait.
_scanning = False


def bump(reason="", scanning=None):
    """Signale qu'une donnée a changé. Sans effet si personne n'écoute.

    CORRECTIF "gel juste après un scan" : l'état "scanning" à transmettre
    est désormais figé ICI, dans le message lui-même, au moment précis de
    bump() — plutôt que relu plus tard (via is_scanning()) au moment où
    chaque abonné SSE émet réellement le message (voir _payload/stream()
    ci-dessous). L'ancien fonctionnement relisait le drapeau global
    _scanning à l'émission, dans un thread séparé : une vraie course entre
    watch_scanner() (qui repassait _scanning à False dès la fin détectée
    du scan) et l'émission du DERNIER message de ce même scan — souvent le
    plus volumineux (bilan final) — pouvait donc annoncer "scanning: false"
    pour CE message précis. Côté client (static/js/live-sync.js), cela
    déclenchait alors le rafraîchissement au débit RAPIDE (hors scan) sur
    le lot de changements le plus lourd, d'où le gel observé juste après
    la fin d'un scan. scanning=None (valeur par défaut, tous les appels
    hors scan) : comportement d'origine, lit l'état courant."""
    global _version
    if scanning is None:
        scanning = is_scanning()
    with _lock:
        _version += 1
        version = _version
        subs = list(_subscribers)
    item = (version, bool(scanning))
    for q in subs:
        try:
            q.put_nowait(item)
        except queue.Full:
            pass  # cet onglet recevra la version suivante ; pas bloquant


def _set_scanning(value):
    global _scanning
    with _lock:
        _scanning = bool(value)


def is_scanning():
    with _lock:
        return _scanning


def _payload(item):
    version, scanning = item
    return json.dumps({"v": version, "scanning": scanning})


def _subscribe():
    q = queue.Queue(maxsize=20)
    with _lock:
        _subscribers.add(q)
    return q


def _unsubscribe(q):
    with _lock:
        _subscribers.discard(q)


def current_version():
    with _lock:
        return _version


def stream():
    """Générateur SSE : un flux par onglet connecté à /api/live."""
    q = _subscribe()
    try:
        # Message initial : la version actuelle, pour que le client sache
        # d'où repartir sans devoir attendre un premier vrai changement.
        # Pas de course possible ici (pas de bump() concurrent à figer) :
        # l'état "scanning" courant convient pour ce seul message initial.
        yield "retry: 2000\n\ndata: {}\n\n".format(_payload((current_version(), is_scanning())))
        while True:
            try:
                item = q.get(timeout=15)
                yield "data: {}\n\n".format(_payload(item))
            except queue.Empty:
                yield ": keep-alive\n\n"  # évite qu'un proxy/navigateur ne coupe la connexion
    except GeneratorExit:
        pass
    finally:
        _unsubscribe(q)


def watch_scanner(get_status, interval=1.0):
    """À lancer dans un thread daemon : signale aux onglets ouverts chaque
    fois que l'état du scan évolue (nouveaux fichiers détectés, progression,
    fin de scan...), pour que la médiathèque reflète les nouvelles vidéos
    en temps réel pendant qu'un scan tourne, sans jamais avoir à recharger
    la page à la main.

    CORRECTIF "gel juste après un scan" (voir bump() ci-dessus pour le
    détail de la course évitée) : on retient l'état "en cours" de la
    boucle PRÉCÉDENTE (was_running) pour pouvoir encore marquer
    "scanning=True" le bump correspondant à la toute dernière transition
    (running → plus running) — c'est ce message précis qui porte
    généralement le bilan final, le lot de changements le plus volumineux.
    Le drapeau global _scanning, lui, n'est repassé à False qu'APRÈS ce
    bump, pour que tout message suivant (heartbeat ou futur changement)
    reflète bien le nouvel état "scan terminé"."""
    last_key = None
    was_running = False
    while True:
        try:
            s = get_status() or {}
            running = bool(s.get("running"))
            # On ignore volontairement "processed" (avance à chaque fichier
            # simplement examiné, très fréquent) pour ne signaler que ce qui
            # change réellement le contenu affiché : nouvelle vidéo, vidéo
            # mise à jour, ou changement de phase (démarrage/fin/erreur).
            key = (s.get("phase"), s.get("new"), s.get("updated"))
            if key != last_key:
                last_key = key
                bump("scan", scanning=(running or was_running))
            was_running = running
            _set_scanning(running)
        except Exception:
            pass
        time.sleep(interval)
