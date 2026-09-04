# Médiathèque locale — 100% hors-ligne

Une bibliothèque vidéo personnelle façon MediaCMS/Jellyfin, qui tourne
entièrement sur ton PC Windows, sans aucune connexion Internet requise pour
fonctionner au quotidien (l'installation initiale des dépendances Python
nécessite Internet **une seule fois**).

Conçue pour gérer plusieurs milliers de vidéos (scan intelligent : les
fichiers déjà indexés et inchangés sont ignorés lors des scans suivants).

## Fonctionnalités

- Parcours en grille façon YouTube/MediaCMS, avec vignettes automatiques
- Recherche par titre, filtrage par catégorie et par tag
- Tri (récent, titre, durée, vues)
- Lecteur vidéo intégré (HTML5) + bouton "Ouvrir dans le lecteur par défaut"
  pour les formats que le navigateur ne sait pas lire nativement (ex. VLC)
- Catégories, tags, playlists
- Édition des métadonnées (titre, description, catégorie, tags)
- Scan en lecture seule : **aucun fichier vidéo n'est jamais modifié,
  déplacé ou supprimé**. "Retirer de la bibliothèque" ne supprime que
  l'entrée dans la base de données.
- Prend en charge les dossiers réseau (chemins UNC type `\\NAS\Videos`)

## Installation et utilisation (un seul script)

1. Installe Python 3.10 ou plus récent : https://www.python.org/downloads/
   (coche bien "Add python.exe to PATH" pendant l'installation)
2. Double-clique sur **run.bat**. Au tout premier lancement, il détecte
   qu'aucun environnement n'existe encore et installe automatiquement tout
   ce qu'il faut (nécessite Internet **une seule fois**) avant de démarrer
   directement l'application — un seul script suffit, à chaque fois.
   Le navigateur s'ouvre automatiquement sur `http://127.0.0.1:5000`.
3. Va dans **⚙ Paramètres**, ajoute le ou les dossiers contenant tes vidéos
   (ex. `D:\Videos` ou un chemin réseau `\\NAS\Partage\Videos`).
4. Clique sur **Lancer le scan**. Une barre de progression s'affiche ; pour
   plusieurs milliers de vidéos, ça peut prendre du temps la première fois
   (génération des vignettes), les scans suivants seront rapides.
5. Parcours, cherche, tague et organise tes vidéos en playlists.

Pour arrêter le serveur : ferme la fenêtre noire (invite de commandes) ou

> `install.bat` existe toujours mais n'est plus nécessaire au démarrage :
> il ne sert qu'à forcer une réinstallation complète et propre (environnement
> virtuel supprimé puis recréé), par exemple en cas de problème de
> dépendances.

### ffmpeg / ffprobe (recommandé, pour les vignettes et la durée des vidéos)

Le script utilise `imageio-ffmpeg`, qui embarque automatiquement un binaire
`ffmpeg` — donc **les vignettes fonctionnent dès l'installation**, sans rien
faire de plus.

En revanche, `ffprobe` (utilisé pour lire la durée et la résolution) n'est
pas inclus. Si tu as déjà ffmpeg installé sur ta machine (dossier `bin` dans
le PATH), tout fonctionnera automatiquement. Sinon :

1. Télécharge un build complet Windows ici :
   https://www.gyan.dev/ffmpeg/builds/ (choisis "release full")
2. Décompresse, et ajoute le dossier `bin` (qui contient `ffmpeg.exe` et
   `ffprobe.exe`) à la variable d'environnement PATH de Windows.
3. Sans ça, les vidéos seront quand même indexées, juste sans durée/
   résolution ni vignette précise à un instant donné.

appuie sur Ctrl+C dedans.

## Accès depuis d'autres appareils du réseau local

Par défaut (`HOST = "0.0.0.0"` dans `config.py`), l'appli est aussi
accessible depuis d'autres appareils connectés au même réseau Wi-Fi/local, à
l'adresse `http://<IP-de-ton-PC>:5000`. Pour la restreindre à cette seule
machine, mets `HOST = "127.0.0.1"` dans `config.py`.

## Limites connues

- La lecture intégrée dans le navigateur fonctionne bien avec les fichiers
  `.mp4`/`.webm`/`.m4v`. Pour les `.mkv`/`.avi`/`.wmv`, ça peut ne pas se
  lire selon les codecs : utilise alors le bouton "Ouvrir dans le lecteur
  par défaut" pour lancer VLC ou autre.
- Pas de transcodage automatique (contrairement à MediaCMS/Jellyfin en mode
  serveur) : les fichiers sont servis tels quels. Volontaire, pour rester
  léger et 100% local sans dépendance lourde.
- Pensé pour un usage mono-utilisateur local, pas pour de multi-comptes
  avec permissions.

## Structure du projet

```
mediatheque/
  app.py           -> serveur Flask (routes web)
  db.py            -> base de données SQLite
  scanner.py       -> scan des dossiers, ffmpeg/ffprobe
  config.py        -> configuration (chemins, port, etc.)
  requirements.txt
  install.bat      -> installation (1 seule fois)
  run.bat          -> lancement quotidien
  templates/       -> pages HTML (Jinja2)
  static/          -> CSS, JS, vignettes générées
  data/            -> base SQLite + réglages (créé automatiquement)
```

Tout le contenu (base de données, réglages, vignettes) reste dans le dossier
`data/` à côté de l'application — rien n'est envoyé où que ce soit.
