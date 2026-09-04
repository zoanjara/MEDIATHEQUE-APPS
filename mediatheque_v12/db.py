# -*- coding: utf-8 -*-
"""
Couche base de données (SQLite) pour la Médiathèque locale.
Aucune dépendance externe : sqlite3 est inclus dans Python standard.
"""
import json
import re
import sqlite3
import threading
import os
import config

# Métadonnées JSON non encore rattachées (vidéos absentes au moment de
# l'import, ou chemin totalement différent). Réappliquées automatiquement
# dès qu'un scan indexe la vidéo correspondante — indépendamment du chemin.
PENDING_LIBRARY_PATH = os.path.join(config.DATA_DIR, "pending_library_import.json")

_local = threading.local()

# Vrai si la table virtuelle FTS5 (recherche plein texte) a bien pu être
# créée par init_db() - le module sqlite3 de Python doit avoir été compilé
# avec le support FTS5, ce qui est le cas de très loin le plus courant
# (Python officiel Windows/Linux/macOS depuis plusieurs années) mais pas
# garanti à 100% sur toutes les distributions. Si indisponible, la
# recherche retombe automatiquement sur l'ancien LIKE '%...%' (voir
# build_filter_where dans app.py et search_suggestions) plutôt que de
# faire planter l'application.
FTS5_AVAILABLE = False


def get_db():
    """Une connexion par thread (Flask utilise plusieurs threads)."""
    # Après un rollback forcé (notamment lors d'un import JSON mal formé),
    # la connexion thread-local peut avoir été invalidée. Tester seulement
    # hasattr() renverrait alors None aux requêtes suivantes et ferait
    # planter l'application après l'import.
    if getattr(_local, "conn", None) is None:
        # timeout=30 : si la base est momentanément verrouillée par une
        # autre connexion (le scan de médias, qui écrit en continu en
        # arrière-plan, en particulier), on attend jusqu'à 30 secondes en
        # retentant automatiquement plutôt que d'échouer immédiatement avec
        # une erreur "database is locked" — c'est cette erreur qui causait
        # des bugs imprévisibles sur d'autres fonctionnalités pendant un
        # scan.
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Mode WAL (Write-Ahead Logging) : les lectures ne sont plus jamais
        # bloquées par une écriture en cours (et réciproquement) — au lieu
        # d'un verrou exclusif sur tout le fichier à chaque écriture, comme
        # avec le mode journal par défaut. C'est la solution standard et
        # moderne pour une application SQLite à plusieurs threads/écritures
        # concurrentes comme celle-ci (scan en arrière-plan + requêtes web
        # simultanées). Idempotent : sans effet si déjà activé.
        conn.execute("PRAGMA journal_mode = WAL")
        # Accompagne le mode WAL (combinaison officiellement recommandée) :
        # bien plus rapide que le mode "FULL" par défaut, sans risque de
        # corruption (seule une transaction déjà validée pourrait être
        # perdue en cas de coupure de courant brutale, jamais la base
        # elle-même).
        conn.execute("PRAGMA synchronous = NORMAL")
        # Filet de sécurité supplémentaire au niveau de SQLite lui-même (en
        # plus du paramètre timeout= ci-dessus, qui agit côté Python).
        conn.execute("PRAGMA busy_timeout = 30000")
        # Cache mémoire SQLite : valeur négative = kibioctets.
        # Défaut SQLite ~2 Mo : trop petit pour une bibliothèque de plusieurs
        # milliers de vidéos (index + pages chaudes évincés en permanence).
        # 64 Mo suffit largement en usage local mono-utilisateur sans
        # saturer la RAM du PC.
        conn.execute("PRAGMA cache_size = -65536")
        # Tables temporaires / tris intermédiaires en RAM plutôt que sur
        # disque (COUNT, DISTINCT, ORDER BY sur de gros ensembles).
        conn.execute("PRAGMA temp_store = MEMORY")
        # mmap : laisse l'OS servir les pages de la base depuis le cache
        # fichier système (lectures répétées quasi gratuites). 256 Mo max.
        conn.execute("PRAGMA mmap_size = 268435456")
        _local.conn = conn
    return _local.conn


def backup_to(dest_path):
    """Écrit une copie cohérente de la base (via l'API de sauvegarde native
    de SQLite) vers dest_path, quel que soit l'état du mode WAL au moment de
    l'appel (contrairement à une simple copie du fichier .db, qui risquerait
    de ne pas inclure les dernières écritures encore dans le fichier -wal).
    Utilisé par /settings/backup pour inclure une base saine dans le zip de
    sauvegarde complète du projet, sans avoir à inclure les fichiers
    -wal/-shm séparément."""
    conn = get_db()
    dest_conn = sqlite3.connect(dest_path)
    try:
        conn.backup(dest_conn)
    finally:
        dest_conn.close()


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            folder_root TEXT,
            filename TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            duration_seconds REAL DEFAULT 0,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            size_bytes INTEGER DEFAULT 0,
            mtime REAL DEFAULT 0,
            thumbnail TEXT,
            category_id INTEGER,
            views INTEGER DEFAULT 0,
            added_at TEXT DEFAULT (datetime('now')),
            missing INTEGER DEFAULT 0,
            disk_off INTEGER DEFAULT 0,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            image1 TEXT, image2 TEXT, image3 TEXT,
            rating INTEGER DEFAULT 0,
            comment TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS video_tags (
            video_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (video_id, tag_id),
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            image1 TEXT, image2 TEXT, image3 TEXT,
            rating INTEGER DEFAULT 0,
            comment TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS playlist_items (
            playlist_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            PRIMARY KEY (playlist_id, video_id),
            FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_videos_title ON videos(title);
        CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category_id);
        CREATE INDEX IF NOT EXISTS idx_videos_added ON videos(added_at);
        """
    )
    conn.commit()
    # Migration douce pour les bases créées avant l'ajout de l'aperçu au survol.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
    if "preview_count" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN preview_count INTEGER DEFAULT 0")
        conn.commit()
    if "featured" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN featured INTEGER DEFAULT 0")
        conn.commit()
    if "rating" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN rating INTEGER DEFAULT 0")
        conn.commit()
    if "video_date" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN video_date TEXT DEFAULT ''")
        conn.commit()
    if "comment" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN comment TEXT DEFAULT ''")
        conn.commit()
    if "disk_off" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN disk_off INTEGER DEFAULT 0")
        conn.commit()
    if "title_is_custom" not in cols:
        # Mémorise si le titre a été modifié manuellement par l'utilisateur
        # (via le panneau "Modifier les métadonnées"). Tant que ce n'est pas
        # le cas, le titre affiché reste dérivé automatiquement du nom de
        # fichier physique, et suit donc un renommage/déplacement détecté
        # sur le disque (voir relocate_video). Dès que l'utilisateur modifie
        # le titre à la main, celui-ci devient figé et n'est plus jamais
        # réécrit automatiquement par un scan.
        conn.execute("ALTER TABLE videos ADD COLUMN title_is_custom INTEGER DEFAULT 0")
        conn.commit()
    if "content_hash" not in cols:
        # Empreinte du CONTENU réel du fichier (taille + échantillon
        # d'octets en début/fin, voir scanner.compute_content_hash) :
        # permet de repérer deux vidéos strictement identiques même sous
        # des noms de fichiers totalement différents, pour la numérotation
        # automatique des doublons (voir reconcile_duplicate_titles).
        conn.execute("ALTER TABLE videos ADD COLUMN content_hash TEXT")
        conn.commit()
    if "favorite" not in cols:
        # Favori/like : accès rapide indépendant de la notation par
        # étoiles (rating). Purement booléen (0/1), affiché via un cœur
        # sur la carte et la page vidéo, filtrable comme les autres champs
        # (voir favorite_only dans app.py).
        conn.execute("ALTER TABLE videos ADD COLUMN favorite INTEGER DEFAULT 0")
        conn.commit()
    if "video_codec" not in cols:
        # Codec vidéo détecté par ffprobe au scan (voir scanner.probe_video)
        # — ex. "h264", "hevc", "vp9". Purement informatif, utilisé pour
        # calculer codec_risky ci-dessous.
        conn.execute("ALTER TABLE videos ADD COLUMN video_codec TEXT DEFAULT ''")
        conn.commit()
    if "codec_risky" not in cols:
        # CORRECTIF ARRÊTS ~5s / CPU ÉLEVÉ SUR GROS FICHIERS : certains
        # codecs (HEVC, 10-bit...) ne se décodent pas en matériel sur toutes
        # les machines et basculent le navigateur en décodage logiciel
        # (CPU à fond, lecture qui gèle). Avant ce correctif, le problème
        # n'était détecté qu'EN RÉACTION à un blocage constaté pendant la
        # lecture (voir player-smooth.js, délais de prudence de 4 à 10s) —
        # exactement l'arrêt de quelques secondes observé. Ce indicateur,
        # calculé une fois au scan (voir scanner.is_risky_codec), permet de
        # pré-convertir ces vidéos EN AVANCE (voir app._precode_risky_videos)
        # pour ne plus jamais subir cette fenêtre de détection en lecture.
        conn.execute("ALTER TABLE videos ADD COLUMN codec_risky INTEGER DEFAULT 0")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_content_hash ON videos(content_hash)")
    conn.commit()

    # ---- Recherche plein texte (FTS5) -----------------------------------
    # Migration de la recherche libre "q" (barre du bandeau, panneau
    # "Filtrer par...", suggestions pendant la frappe) : remplace le LIKE
    # '%...%' sur title/description/comment (lent sur une grosse
    # bibliothèque, correspondance uniquement par sous-chaîne brute) par un
    # vrai index plein texte SQLite FTS5.
    #
    # Table "à contenu externe" (content='videos', content_rowid='id') :
    # elle ne duplique pas les données de videos, seulement l'index de
    # recherche, et reste synchronisée automatiquement via les 3 triggers
    # ci-dessous à chaque INSERT/UPDATE/DELETE sur videos - AUCUN code
    # d'écriture ailleurs dans le projet (scanner.py, édition de
    # métadonnées, suppression...) n'a besoin d'être modifié : il continue
    # à écrire dans videos exactement comme avant, la synchronisation de
    # l'index est entièrement prise en charge ici.
    global FTS5_AVAILABLE
    try:
        # Détecté AVANT la création : permet de savoir si videos_fts vient
        # tout juste d'être créée (première mise à jour de l'appli vers
        # cette version, ou base restaurée depuis une sauvegarde antérieure
        # à FTS5) - dans ce cas seulement, un rebuild complet de l'index
        # est nécessaire juste après (voir plus bas).
        fts_existed_before = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'videos_fts'"
        ).fetchone() is not None
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
                title, description, comment,
                content='videos', content_rowid='id', tokenize='unicode61'
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS videos_fts_ai AFTER INSERT ON videos BEGIN
              INSERT INTO videos_fts(rowid, title, description, comment)
              VALUES (new.id, new.title, new.description, new.comment);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS videos_fts_ad AFTER DELETE ON videos BEGIN
              INSERT INTO videos_fts(videos_fts, rowid, title, description, comment)
              VALUES ('delete', old.id, old.title, old.description, old.comment);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS videos_fts_au AFTER UPDATE ON videos BEGIN
              INSERT INTO videos_fts(videos_fts, rowid, title, description, comment)
              VALUES ('delete', old.id, old.title, old.description, old.comment);
              INSERT INTO videos_fts(rowid, title, description, comment)
              VALUES (new.id, new.title, new.description, new.comment);
            END
            """
        )
        conn.commit()
        if not fts_existed_before:
            # Migration initiale : la commande spéciale 'rebuild' de FTS5
            # reconstruit entièrement l'index à partir de la table de
            # contenu externe (videos) - c'est la méthode officielle et
            # fiable pour peupler une table FTS5 "external content" créée
            # après coup sur des données déjà existantes. Note : un simple
            # "INSERT INTO videos_fts(...) SELECT ... WHERE id NOT IN
            # (SELECT rowid FROM videos_fts)" ne fonctionne PAS ici, car un
            # SELECT sans MATCH sur une table FTS5 à contenu externe lit
            # directement la table de contenu (donc "déjà indexé" ne peut
            # pas être détecté ainsi) - seul 'rebuild' est fiable.
            conn.execute("INSERT INTO videos_fts(videos_fts) VALUES ('rebuild')")
            conn.commit()
        FTS5_AVAILABLE = True
    except sqlite3.OperationalError:
        # Ce build de SQLite/Python n'a pas FTS5 : on continue sans, la
        # recherche retombera automatiquement sur l'ancien LIKE '%...%'
        # (voir build_filter_where dans app.py et search_suggestions).
        conn.rollback()
        FTS5_AVAILABLE = False

    # ---- Index de performance ------------------------------------------
    # Chaque page de vidéos (accueil, catégorie, playlist, tag) exécute
    # plusieurs requêtes filtrées sur "missing"/"disk_off" (toujours
    # présents) ainsi que sur la note, la durée et la date, en plus de
    # calculer les "facettes" de filtre dynamique (get_filter_facets) —
    # jusqu'à 7 requêtes par page. Sans index dédiés, SQLite doit parcourir
    # la table entière à chaque fois, ce qui devient de plus en plus lent
    # à mesure que la bibliothèque grossit : c'est ce qui rendait l'appli
    # perceptiblement plus lente et saccadée après un scan ayant ajouté
    # beaucoup de nouvelles vidéos. Ces index accélèrent ces requêtes sans
    # rien changer au comportement ni aux données.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_missing_diskoff ON videos(missing, disk_off)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_rating ON videos(rating)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_favorite ON videos(favorite)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_duration ON videos(duration_seconds)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_video_date ON videos(video_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_height ON videos(height)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_folder_root ON videos(folder_root)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_missing ON videos(missing)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_views ON videos(views)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_size ON videos(size_bytes)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag_id)")
    # playlist_items a pour clé primaire (playlist_id, video_id) : une
    # recherche par video_id seul (attach_video_collections, facette
    # playlist...) ne profitait donc d'aucun index et forçait un parcours
    # complet de la table à chaque page. Cet index dédié corrige ce point.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items(video_id)"
    )
    # ---- Index COUVRANTS (covering) pour query_videos -------------------
    #
    # Requête type (étape 2 de la pagination) :
    #   SELECT v.id FROM videos v
    #   WHERE v.missing = 0 AND v.disk_off = 0
    #   ORDER BY v.added_at DESC, v.id ASC
    #   LIMIT 20
    #
    # Un index est "couvrant" si toutes les colonnes lues y figurent :
    # SQLite peut répondre SANS rouvrir la table videos (index-only scan).
    #
    # Index PARTIELS (WHERE missing=0 AND disk_off=0) :
    #   - plus petits (seulement les vidéos visibles)
    #   - parfaitement alignés sur le filtre permanent de l'appli
    #   - idéaux pour COUNT(*) et SELECT id … LIMIT/OFFSET/curseur
    #
    # EXISTS tag/playlist : (tag_id, video_id) / (playlist_id, video_id)
    # couvrent entièrement le sous-SELECT 1 → semi-join index-only.
    #
    # Chaque CREATE est isolé : un échec ne plante jamais init_db.
    for _ddl in (
        # --- Jointures / EXISTS (couvrants) ---
        "CREATE INDEX IF NOT EXISTS idx_cov_tags_tag_video "
        "ON video_tags(tag_id, video_id)",
        "CREATE INDEX IF NOT EXISTS idx_cov_pl_pl_video "
        "ON playlist_items(playlist_id, video_id)",
        "CREATE INDEX IF NOT EXISTS idx_cov_pl_pl_pos_video "
        "ON playlist_items(playlist_id, position, video_id)",
        # --- Pagination : SELECT id … ORDER BY clé, id (index partiels) ---
        "CREATE INDEX IF NOT EXISTS idx_cov_added "
        "ON videos(added_at DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_views "
        "ON videos(views DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_size "
        "ON videos(size_bytes DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_duration "
        "ON videos(duration_seconds DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_rating "
        "ON videos(rating DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_title "
        "ON videos(title COLLATE NOCASE ASC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_vdate "
        "ON videos(video_date DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        # --- Page catégorie (filtre category_id + tri récent) ---
        "CREATE INDEX IF NOT EXISTS idx_cov_cat_added "
        "ON videos(category_id, added_at DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_cat_title "
        "ON videos(category_id, title COLLATE NOCASE ASC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        "CREATE INDEX IF NOT EXISTS idx_cov_cat_views "
        "ON videos(category_id, views DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0",
        # --- Favoris uniquement ---
        "CREATE INDEX IF NOT EXISTS idx_cov_favorite_added "
        "ON videos(added_at DESC, id ASC) "
        "WHERE missing = 0 AND disk_off = 0 AND favorite = 1",
        # --- COUNT(*) vidéos visibles (très fréquent) ---
        "CREATE INDEX IF NOT EXISTS idx_cov_visible_id "
        "ON videos(id) "
        "WHERE missing = 0 AND disk_off = 0",
        # Repli non partiel (si le planificateur ignore un index partiel)
        "CREATE INDEX IF NOT EXISTS idx_videos_vis_added_id "
        "ON videos(missing, disk_off, added_at DESC, id ASC)",
        "CREATE INDEX IF NOT EXISTS idx_videos_vis_cat_added "
        "ON videos(missing, disk_off, category_id, added_at DESC, id ASC)",
    ):
        try:
            conn.execute(_ddl)
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass

    # Migration douce : catégories/playlists créées avant l'ajout des
    # images / notation / commentaire.
    cat_cols = [r["name"] for r in conn.execute("PRAGMA table_info(categories)").fetchall()]
    for col, ddl in [
        ("image1", "ALTER TABLE categories ADD COLUMN image1 TEXT"),
        ("image2", "ALTER TABLE categories ADD COLUMN image2 TEXT"),
        ("image3", "ALTER TABLE categories ADD COLUMN image3 TEXT"),
        ("rating", "ALTER TABLE categories ADD COLUMN rating INTEGER DEFAULT 0"),
        ("comment", "ALTER TABLE categories ADD COLUMN comment TEXT DEFAULT ''"),
    ]:
        if col not in cat_cols:
            conn.execute(ddl)
            conn.commit()

    pl_cols = [r["name"] for r in conn.execute("PRAGMA table_info(playlists)").fetchall()]
    for col, ddl in [
        ("image1", "ALTER TABLE playlists ADD COLUMN image1 TEXT"),
        ("image2", "ALTER TABLE playlists ADD COLUMN image2 TEXT"),
        ("image3", "ALTER TABLE playlists ADD COLUMN image3 TEXT"),
        ("rating", "ALTER TABLE playlists ADD COLUMN rating INTEGER DEFAULT 0"),
        ("comment", "ALTER TABLE playlists ADD COLUMN comment TEXT DEFAULT ''"),
        # Date de création d'une sous-playlist, saisie manuellement par
        # l'utilisateur (distincte de created_at, qui est horodaté
        # automatiquement à l'insertion en base). Stockée au format
        # YYYY-MM-DD (celui de <input type="date">), affichée via le
        # filtre Jinja |frdate. Uniquement exposée côté interface pour les
        # sous-playlists (playlists avec parent_id renseigné).
        ("manual_creation_date", "ALTER TABLE playlists ADD COLUMN manual_creation_date TEXT DEFAULT ''"),
        # Sous-playlists : une playlist peut être rattachée à une autre
        # playlist (sa "playlist parente"). NULL = playlist principale.
        # D'anciennes bases pouvaient déjà avoir une colonne parent_id
        # héritée d'une ancienne version : dans ce cas on la réutilise
        # simplement telle quelle plutôt que de la recréer.
        ("parent_id", "ALTER TABLE playlists ADD COLUMN parent_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL"),
    ]:
        if col not in pl_cols:
            conn.execute(ddl)
            conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_playlists_parent ON playlists(parent_id)")
    conn.commit()

    # Migration douce : tags créés avant l'ajout des images. Un tag peut
    # désormais porter jusqu'à 3 images (même schéma que catégories /
    # playlists), affichées dans la fenêtre "🏷 Tags" et sa vue détaillée.
    tag_cols = [r["name"] for r in conn.execute("PRAGMA table_info(tags)").fetchall()]
    for col, ddl in [
        ("image1", "ALTER TABLE tags ADD COLUMN image1 TEXT"),
        ("image2", "ALTER TABLE tags ADD COLUMN image2 TEXT"),
        ("image3", "ALTER TABLE tags ADD COLUMN image3 TEXT"),
    ]:
        if col not in tag_cols:
            conn.execute(ddl)
            conn.commit()

    # "Females" par défaut : liste de playlists pré-créées, disponibles dès le
    # premier lancement dans la section "Females" du panneau latéral
    # (ex-"Playlists"). INSERT OR IGNORE évite tout doublon si elles existent
    # déjà (relance de l'appli, nom déjà présent).
    default_playlists = [
        "FEMALES", "LES NEWS", "MAN", "SITE", "LATINA SXMX",
        "GASY", "FRENCH", "NIVEAU D'ADRENALINE",
    ]
    for name in default_playlists:
        conn.execute("INSERT OR IGNORE INTO playlists(name) VALUES (?)", (name,))
    conn.commit()


def build_fts_match_query(q):
    """Construit une expression MATCH FTS5 sûre à partir d'une recherche
    libre saisie par l'utilisateur (recherche plein texte sur
    titre/description/commentaire, voir videos_fts dans init_db).

    - Chaque mot est mis entre guillemets (littéral, les caractères
      spéciaux de la syntaxe FTS5 comme -, *, : ne sont ainsi jamais
      interprétés) et suivi de * pour un préfixe (ex: "cha"* trouve aussi
      "chat", "chateau"...), reproduisant la tolérance d'une recherche par
      sous-chaîne du LIKE '%...%' précédent tout en profitant de l'index.
    - Plusieurs mots sont implicitement combinés en ET (comportement FTS5
      par défaut), comme c'était déjà le cas visuellement avec LIKE sur la
      chaîne complète.
    - Renvoie None si q est vide ou ne contient aucun caractère
      exploitable (ex: uniquement de la ponctuation) : dans ce cas
      l'appelant ne doit trouver aucun résultat plutôt que d'ignorer le
      filtre silencieusement."""
    if not q:
        return None
    tokens = re.findall(r"\w+", q, re.UNICODE)
    if not tokens:
        return None
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"*' for t in tokens)


def get_or_create_category(name):
    name = name.strip()
    if not name:
        return None
    conn = get_db()
    cur = conn.execute("SELECT id FROM categories WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO categories(name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def get_or_create_tag(name):
    name = name.strip().lower()
    if not name:
        return None
    conn = get_db()
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO tags(name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def set_video_tags(video_id, tag_names):
    conn = get_db()
    conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
    for name in tag_names:
        name = name.strip()
        if not name:
            continue
        tag_id = get_or_create_tag(name)
        conn.execute(
            "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
            (video_id, tag_id),
        )
    conn.commit()


def _normalize_folder_path(path):
    """Normalise un chemin pour comparaison robuste Windows/Linux :
    - retire le préfixe long-path Windows \\\\?\\
    - unifie / et \\
    - ignore barres finales, casse, espaces de bord
    """
    if not path:
        return ""
    p = str(path).strip()
    # Préfixe Windows long path
    if p.startswith("\\\\?\\"):
        p = p[4:]
    elif p.startswith("//?/"):
        p = p[4:]
    p = p.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/").lower()


# Cache mémoire des dossiers désactivés. Source de vérité runtime :
# - stream / heartbeat / mini → path_is_disabled()
# - grilles → clause SQL get_disabled_roots_clause() (immédiat, sans attendre disk_off)
# disk_off n'est qu'un cache SQL mis à jour en arrière-plan (jamais sur le thread web).
_disabled_lock = threading.Lock()
_disabled_normalized_cache = frozenset()
_disabled_roots_exact = tuple()  # valeurs folder_root à exclure en SQL
_disk_off_bg_lock = threading.Lock()


def get_disabled_normalized():
    """Ensemble des chemins désactivés normalisés (thread-safe)."""
    with _disabled_lock:
        return _disabled_normalized_cache


def get_disabled_roots_exact():
    """Tuple des folder_root exacts à exclure des grilles (thread-safe)."""
    with _disabled_lock:
        return _disabled_roots_exact


def get_disabled_roots_clause(alias="v"):
    """Retourne (sql_fragment, params) pour exclure les disques désactivés.

    Utilisé dans build_filter_where : effet immédiat dès le cocher/décocher,
    sans attendre la mise à jour asynchrone de disk_off.
    """
    roots = get_disabled_roots_exact()
    if not roots:
        return "", []
    col = f"{alias}.folder_root" if alias else "folder_root"
    ph = ",".join("?" for _ in roots)
    # NULL folder_root reste visible (ne peut pas être rattaché à un disque off)
    sql = f"({col} IS NULL OR {col} NOT IN ({ph}))"
    return sql, list(roots)


def path_is_disabled(path, folder_root=None):
    """True si ce chemin (ou son folder_root) est sous un disque désactivé."""
    norms = get_disabled_normalized()
    if not norms:
        return False
    path_n = _normalize_folder_path(path or "")
    root_n = _normalize_folder_path(folder_root or "")
    for norm in norms:
        if not norm:
            continue
        if path_n == norm or path_n.startswith(norm + "/"):
            return True
        if root_n == norm or root_n.startswith(norm + "/"):
            return True
    return False


def set_disabled_folders(disabled_folders, on_done=None):
    """Cache mémoire + racines SQL immédiats ; disk_off en arrière-plan.

    DISTINCT folder_root est quasi instantané (quelques lignes) : les grilles
    excluent tout de suite les bons chemins, sans attendre le thread SQL.
    """
    global _disabled_normalized_cache, _disabled_roots_exact
    disabled_folders = [f for f in (disabled_folders or []) if f]
    disabled_normalized = frozenset(
        _normalize_folder_path(f) for f in disabled_folders if _normalize_folder_path(f)
    )

    matched = []
    try:
        conn = get_db()
        db_roots = [
            r["folder_root"]
            for r in conn.execute(
                "SELECT DISTINCT folder_root FROM videos WHERE folder_root IS NOT NULL"
            ).fetchall()
        ]
        for r in db_roots:
            rn = _normalize_folder_path(r)
            for nrm in disabled_normalized:
                if not nrm:
                    continue
                if rn == nrm or rn.startswith(nrm + "/") or nrm.startswith(rn + "/"):
                    matched.append(r)
                    break
    except Exception:
        matched = list(disabled_folders)

    roots_exact = tuple(matched) if matched else tuple(disabled_folders)

    with _disabled_lock:
        _disabled_normalized_cache = disabled_normalized
        _disabled_roots_exact = roots_exact

    def _bg():
        n = 0
        try:
            import sqlite3
            with _disk_off_bg_lock:
                conn = sqlite3.connect(config.DB_PATH, timeout=60)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 60000")
                try:
                    conn.execute("UPDATE videos SET disk_off = 0 WHERE disk_off = 1")
                    if matched:
                        ph = ",".join("?" for _ in matched)
                        conn.execute(
                            f"UPDATE videos SET disk_off = 1 WHERE folder_root IN ({ph})",
                            list(matched),
                        )
                    conn.commit()
                    n = int(conn.execute(
                        "SELECT COUNT(*) AS c FROM videos WHERE disk_off = 1"
                    ).fetchone()["c"])
                finally:
                    conn.close()
        except Exception:
            n = 0
        if on_done is not None:
            try:
                on_done(n)
            except Exception:
                pass

    t = threading.Thread(target=_bg, name="disk-off-bg", daemon=True)
    t.start()
    return len(matched)


def is_video_allowed(video_id):
    """True si la vidéo peut être lue (existe, pas missing, disque actif)."""
    if video_id is None:
        return True
    try:
        video_id = int(video_id)
    except (TypeError, ValueError):
        return True
    conn = get_db()
    row = conn.execute(
        "SELECT path, folder_root, disk_off, missing FROM videos WHERE id = ?",
        (video_id,),
    ).fetchone()
    if not row or row["missing"]:
        return False
    if row["disk_off"] or path_is_disabled(row["path"], row["folder_root"]):
        return False
    return True


def find_missing_candidates(filename, size_bytes, duration_seconds=None, tolerance=1.5,
                             width=None, height=None):
    """Cherche parmi les vidéos marquées 'manquantes' celles qui correspondent
    au fichier retrouvé. Retourne la liste des candidats (peut être vide, un
    seul, ou plusieurs si ambigu).

    Deux niveaux de recherche :
      1. Nom de fichier + taille identiques (déplacement simple, sans
         renommage) — signal le plus fort, comportement d'origine inchangé.
      2. Repli additif, seulement si le niveau 1 ne trouve rien : le fichier
         a été RENOMMÉ (en plus d'être éventuellement déplacé). Dans ce cas
         le nom ne correspond plus, mais le contenu du fichier, lui, n'a pas
         changé : on retrouve la vidéo via sa taille en octets (un
         renommage seul ne modifie jamais la taille), affinée par la durée,
         puis par la résolution si plusieurs candidats restent ambigus.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT id, path, duration_seconds FROM videos
           WHERE missing = 1 AND filename = ? AND size_bytes = ?""",
        (filename, size_bytes),
    ).fetchall()
    if rows:
        if duration_seconds is None or len(rows) <= 1:
            return rows
        # Si plusieurs candidats, on affine avec la durée (tolérance en secondes)
        refined = [
            r for r in rows
            if abs((r["duration_seconds"] or 0) - duration_seconds) <= tolerance
        ]
        return refined if refined else rows

    # --- Repli : correspondance par taille (+ durée, + résolution) -------
    fallback_rows = conn.execute(
        """SELECT id, path, duration_seconds, width, height FROM videos
           WHERE missing = 1 AND size_bytes = ?""",
        (size_bytes,),
    ).fetchall()
    if not fallback_rows or len(fallback_rows) <= 1:
        return fallback_rows
    if duration_seconds is not None:
        refined = [
            r for r in fallback_rows
            if abs((r["duration_seconds"] or 0) - duration_seconds) <= tolerance
        ]
        if refined:
            fallback_rows = refined
    if len(fallback_rows) > 1 and width and height:
        refined2 = [
            r for r in fallback_rows
            if r["width"] == width and r["height"] == height
        ]
        if refined2:
            fallback_rows = refined2
    return fallback_rows


def relocate_video(video_id, new_path, new_folder_root, mtime, content_hash=None,
                    thumbnail=None, preview_count=None):
    """Met à jour le chemin d'une vidéo retrouvée (déplacée et/ou renommée)
    sans toucher à sa vignette, ses tags, ses vues ni sa catégorie. Le nom de
    fichier stocké est aussi remis à jour s'il a changé (renommage détecté
    via le repli ci-dessus), pour que la base reste cohérente avec le disque
    et que les prochains scans continuent de reconnaître ce fichier.

    thumbnail / preview_count : SEULEMENT renseignés par l'appelant quand
    l'ancienne vignette de cette vidéo a été constatée absente du disque
    (voir scanner.py) — une vignette neuve vient alors d'être générée sous
    un nouveau nom (dérivé du nouveau chemin) et doit remplacer l'ancienne
    référence, sans quoi la base pointerait indéfiniment vers un fichier de
    vignette qui n'existe plus. Si l'ancienne vignette est toujours valide
    sur le disque, l'appelant laisse ces deux paramètres à None : elle est
    alors conservée telle quelle, comme avant.

    content_hash : empreinte fraîchement recalculée par le scan en cours
    (voir scanner._process_file). Le contenu du fichier n'a normalement pas
    changé lors d'un simple déplacement/renommage, donc réécrire cette
    empreinte est sans risque — et c'est même nécessaire pour une vidéo dont
    l'empreinte n'avait encore jamais été calculée (bibliothèque plus
    ancienne, colonne ajoutée après coup, jamais rescannée depuis) : sans
    cette mise à jour, elle resterait invisible au repli par empreinte de
    contenu d'un futur import JSON, indéfiniment.

    Si le fichier a été RENOMMÉ (nom physique différent de l'ancien) et que
    le titre affiché dans l'appli n'a jamais été personnalisé à la main
    (title_is_custom = 0), le titre est régénéré à partir du nouveau nom de
    fichier : l'appli affiche alors le nouveau nom physique, sans jamais
    toucher aux playlists / sous-playlists / catégorie / tags déjà
    enregistrés pour cette vidéo (ils restent attachés à son id, inchangé)."""
    conn = get_db()
    new_filename = os.path.basename(new_path)
    row = conn.execute(
        "SELECT filename, title_is_custom, content_hash FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    renamed = bool(row) and row["filename"] != new_filename
    set_clauses = ["path=?", "folder_root=?", "filename=?", "mtime=?", "missing=0"]
    params = [new_path, new_folder_root, new_filename, mtime]
    if content_hash and row and not row["content_hash"]:
        set_clauses.append("content_hash=?")
        params.append(content_hash)
    if thumbnail is not None:
        set_clauses.append("thumbnail=?")
        params.append(thumbnail)
    if preview_count is not None:
        set_clauses.append("preview_count=?")
        params.append(preview_count)
    params.append(video_id)
    conn.execute(
        f"UPDATE videos SET {', '.join(set_clauses)} WHERE id=?",
        params,
    )
    if renamed and row and not row["title_is_custom"]:
        new_title_base = os.path.splitext(new_filename)[0]
        conn.execute("UPDATE videos SET title=? WHERE id=?", (new_title_base, video_id))
    conn.commit()


_DUP_TITLE_RE = re.compile(r"^N°(\d+) (.*)$")


def make_unique_title(base_title):
    """Titre de départ d'une vidéo nouvellement indexée : simplement son nom
    de fichier (sans extension). La numérotation des doublons ('N°1 ...',
    'N°2 ...') n'est PAS décidée ici : elle est basée sur le contenu réel du
    fichier (content_hash), pas sur le nom, et appliquée séparément par
    reconcile_duplicate_titles() une fois l'empreinte connue — deux vidéos
    strictement identiques mais nommées différemment sont ainsi bien
    détectées comme doublons, ce qu'une simple comparaison de titre ne
    permettait pas."""
    return base_title


def reconcile_duplicate_titles():
    """Numérote automatiquement, dans l'appli uniquement (jamais sur le
    disque), les COPIES d'un groupe de vidéos strictement identiques en
    CONTENU (même content_hash) mais présentes sous des noms de fichiers
    différents : 'N°1 ...', 'N°2 ...', etc. — chacune gardant son propre
    nom après le numéro.

    Seules les vidéos PRÉSENTES (missing = 0) sont prises en compte, à
    la fois pour former les groupes et pour choisir l'originale : une
    vidéo 'manquante' (fichier introuvable sur le disque, par exemple un
    reliquat resté en base après un scan qui n'a pas pu la relocaliser
    automatiquement) ne doit jamais faire numéroter à tort une vidéo bien
    réelle et unique sur le disque, ni être choisie comme originale à sa
    place — c'est ce que corrige ce filtre.

    La vidéo la plus ancienne du groupe (id le plus petit = celle indexée
    en premier, avant l'apparition de ses copies) est considérée comme
    L'ORIGINALE : elle n'est JAMAIS numérotée, y compris si un ancien
    passage l'avait numérotée par erreur (le préfixe est alors retiré).
    Seules ses copies reçoivent un préfixe.

    Ne touche jamais à un titre personnalisé (title_is_custom = 1), que ce
    soit pour numéroter une copie ou retirer le préfixe de l'originale :
    celui-ci est ignoré et jamais réécrit. Idempotente : ne renumérote pas
    ce qui l'est déjà, et complète simplement un groupe existant si une
    nouvelle copie apparaît. Peut être appelée à tout moment (fin de scan,
    ou tâche de fond une fois les empreintes de l'ancienne bibliothèque
    calculées)."""
    conn = get_db()
    groups = conn.execute(
        """SELECT content_hash FROM videos
           WHERE content_hash IS NOT NULL AND content_hash != '' AND missing = 0
           GROUP BY content_hash HAVING COUNT(*) > 1"""
    ).fetchall()
    changed = False
    for g in groups:
        rows = conn.execute(
            """SELECT id, title, title_is_custom FROM videos
               WHERE content_hash = ? AND missing = 0 ORDER BY id""",
            (g["content_hash"],),
        ).fetchall()
        original, duplicates = rows[0], rows[1:]

        # L'originale ne doit jamais rester numérotée : si elle porte
        # encore un préfixe 'N°x ' (numérotée avant l'introduction de
        # cette règle), on le retire pour lui rendre son titre d'origine.
        if not original["title_is_custom"]:
            m = _DUP_TITLE_RE.match(original["title"])
            if m:
                conn.execute("UPDATE videos SET title = ? WHERE id = ?", (m.group(2), original["id"]))
                changed = True

        max_n = 0
        for r in duplicates:
            m = _DUP_TITLE_RE.match(r["title"])
            if m:
                max_n = max(max_n, int(m.group(1)))
        next_n = max_n + 1
        for r in duplicates:
            if _DUP_TITLE_RE.match(r["title"]):
                continue  # déjà numérotée : on n'y touche pas
            if r["title_is_custom"]:
                continue  # titre personnalisé : jamais modifié automatiquement
            conn.execute(
                "UPDATE videos SET title = ? WHERE id = ?",
                (f"N°{next_n} {r['title']}", r["id"]),
            )
            next_n += 1
            changed = True
    if changed:
        conn.commit()


def get_missing_videos():
    conn = get_db()
    return conn.execute(
        "SELECT id, title, path, filename FROM videos WHERE missing = 1 ORDER BY title"
    ).fetchall()


def delete_videos(video_ids):
    """Supprime définitivement les entrées listées (et leurs vignettes ne
    sont pas effacées du disque ici — ça reste un détail mineur, on peut
    les laisser ou les nettoyer séparément)."""
    conn = get_db()
    conn.executemany("DELETE FROM videos WHERE id = ?", [(vid,) for vid in video_ids])
    conn.commit()


def get_video_tags(video_id):
    conn = get_db()
    rows = conn.execute(
        """SELECT t.id, t.name, t.image1 FROM tags t
           JOIN video_tags vt ON vt.tag_id = t.id
           WHERE vt.video_id = ? ORDER BY t.name COLLATE NOCASE""",
        (video_id,),
    ).fetchall()
    return rows


def get_video_playlist_ids(video_id):
    """Liste des ids de playlists (ex-catégorie "Females") contenant cette
    vidéo. Utilisé pour pré-cocher les cases dans "✎ Modifier les
    métadonnées"."""
    conn = get_db()
    rows = conn.execute(
        "SELECT playlist_id FROM playlist_items WHERE video_id = ?", (video_id,)
    ).fetchall()
    return [r["playlist_id"] for r in rows]


def set_video_playlists(video_id, playlist_ids):
    """Synchronise l'appartenance d'une vidéo à un ensemble de playlists
    (cases cochées dans "✎ Modifier les métadonnées") : ajoute les nouvelles,
    retire celles décochées. Une vidéo peut appartenir à plusieurs playlists
    à la fois (contrairement à la catégorie, qui est unique)."""
    conn = get_db()
    wanted = {int(pid) for pid in (playlist_ids or [])}
    current = {
        r["playlist_id"] for r in conn.execute(
            "SELECT playlist_id FROM playlist_items WHERE video_id = ?", (video_id,)
        ).fetchall()
    }
    for pid in current - wanted:
        conn.execute(
            "DELETE FROM playlist_items WHERE playlist_id = ? AND video_id = ?",
            (pid, video_id),
        )
    for pid in wanted - current:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 as n FROM playlist_items WHERE playlist_id = ?",
            (pid,),
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO playlist_items(playlist_id, video_id, position) VALUES (?, ?, ?)",
            (pid, video_id, row["n"]),
        )
    conn.commit()


# ---------- Favoris (indépendants de la note par étoiles) ----------

def toggle_favorite(video_id):
    """Bascule l'état favori d'une vidéo (0<->1) et renvoie la nouvelle
    valeur (0 ou 1), ou None si la vidéo n'existe pas. Volontairement
    indépendant de 'rating' (notation 0-5 étoiles) : le favori est un
    simple marqueur binaire d'accès rapide, sans dégrader ni toucher à la
    note existante."""
    conn = get_db()
    row = conn.execute("SELECT favorite FROM videos WHERE id = ?", (video_id,)).fetchone()
    if row is None:
        return None
    new_value = 0 if row["favorite"] else 1
    conn.execute("UPDATE videos SET favorite = ? WHERE id = ?", (new_value, video_id))
    conn.commit()
    return new_value


# ---------- Vidéo à la une ----------

def set_featured(video_id):
    conn = get_db()
    conn.execute("UPDATE videos SET featured = 0")  # une seule à la fois
    conn.execute("UPDATE videos SET featured = 1 WHERE id = ?", (video_id,))
    conn.commit()


def unset_featured(video_id):
    conn = get_db()
    conn.execute("UPDATE videos SET featured = 0 WHERE id = ?", (video_id,))
    conn.commit()


def get_featured_video():
    conn = get_db()
    return conn.execute(
        "SELECT * FROM videos WHERE featured = 1 AND missing = 0 AND disk_off = 0 LIMIT 1"
    ).fetchone()


# ---------- Édition en masse ----------

def bulk_apply(video_ids, category_name=None, add_tag_names=None, remove_tag_names=None):
    conn = get_db()
    category_id = get_or_create_category(category_name) if category_name else None
    for vid in video_ids:
        if category_name:
            conn.execute("UPDATE videos SET category_id=? WHERE id=?", (category_id, vid))
        if add_tag_names:
            for name in add_tag_names:
                tag_id = get_or_create_tag(name)
                if tag_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
                        (vid, tag_id),
                    )
        if remove_tag_names:
            for name in remove_tag_names:
                trow = conn.execute(
                    "SELECT id FROM tags WHERE name = ?", (name.strip().lower(),)
                ).fetchone()
                if trow:
                    conn.execute(
                        "DELETE FROM video_tags WHERE video_id = ? AND tag_id = ?",
                        (vid, trow["id"]),
                    )
    conn.commit()


# ---------- Gestion des catégories ----------

def rename_category(category_id, new_name):
    new_name = new_name.strip()
    if not new_name:
        return False, "Le nom ne peut pas être vide."
    conn = get_db()
    try:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, category_id))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Une catégorie porte déjà ce nom."


def delete_category(category_id):
    """Supprime la catégorie. Les vidéos qui l'utilisaient repassent
    'sans catégorie' (elles ne sont jamais supprimées)."""
    conn = get_db()
    conn.execute("UPDATE videos SET category_id = NULL WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()


# ---------- Gestion des playlists ----------

def rename_playlist(playlist_id, new_name):
    new_name = new_name.strip()
    if not new_name:
        return False, "Le nom ne peut pas être vide."
    conn = get_db()
    try:
        conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (new_name, playlist_id))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Une playlist porte déjà ce nom."


def delete_playlist(playlist_id):
    """Supprime la playlist. Ses éventuelles sous-playlists ne sont pas
    supprimées : elles sont remontées d'un cran (rattachées à la playlist
    parente de celle qu'on supprime, ou promues playlists principales s'il
    n'y en avait pas)."""
    conn = get_db()
    row = conn.execute("SELECT parent_id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    grandparent_id = row["parent_id"] if row else None
    conn.execute("UPDATE playlists SET parent_id = ? WHERE parent_id = ?", (grandparent_id, playlist_id))
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()


# ---------- Gestion des tags ----------

def rename_tag(tag_id, new_name):
    new_name = new_name.strip().lower()
    if not new_name:
        return False, "Le nom ne peut pas être vide."
    conn = get_db()
    try:
        conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Un tag porte déjà ce nom."


def delete_tag(tag_id):
    conn = get_db()
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()


def get_tag(tag_id):
    conn = get_db()
    return conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()


def get_all_tags_full():
    """Tous les tags avec leur image de couverture et leur nombre de vidéos,
    triés par nom — utilisé par l'interface de gestion des tags (fenêtre
    "🏷 Tags") pour permettre le tri/filtre côté client (nom ou image)."""
    conn = get_db()
    return conn.execute(
        """SELECT t.id, t.name, t.image1, t.image2, t.image3,
                  COUNT(vt.video_id) as n
           FROM tags t
           LEFT JOIN video_tags vt ON vt.tag_id = t.id
           GROUP BY t.id ORDER BY t.name COLLATE NOCASE"""
    ).fetchall()


def update_tag_images(tag_id, images):
    """images: dict optionnel {'image1': nom_fichier, ...} — ne modifie que
    les clés présentes (même logique que update_category_full), pour
    permettre d'associer une image (tag visuel/image) à un tag existant."""
    conn = get_db()
    for key, value in images.items():
        if key in ("image1", "image2", "image3") and value is not None:
            conn.execute(f"UPDATE tags SET {key} = ? WHERE id = ?", (value, tag_id))
    conn.commit()


# ---------- Catégories : arborescence + CRUD complet ----------

def get_categories():
    """Retourne toutes les catégories (liste plate, pas de sous-catégories),
    triées par nom, avec le nombre de vidéos associées."""
    conn = get_db()
    rows = conn.execute(
        """SELECT c.*, COUNT(v.id) as n FROM categories c
           LEFT JOIN videos v ON v.category_id = c.id AND v.missing = 0 AND v.disk_off = 0
           GROUP BY c.id ORDER BY c.name COLLATE NOCASE"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_category(category_id):
    conn = get_db()
    return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


def create_category(name):
    name = name.strip()
    if not name:
        return None, "Le nom ne peut pas être vide."
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO categories(name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "Une catégorie porte déjà ce nom."


def update_category_full(category_id, name, rating, comment, images):
    """images: dict optionnel {'image1': path_ou_None, ...} — ne modifie que
    les clés présentes (permet de ne remplacer qu'une image à la fois)."""
    name = name.strip()
    if not name:
        return False, "Le nom ne peut pas être vide."
    conn = get_db()
    try:
        conn.execute(
            "UPDATE categories SET name=?, rating=?, comment=? WHERE id=?",
            (name, rating, comment, category_id),
        )
        for key, value in images.items():
            if key in ("image1", "image2", "image3") and value is not None:
                conn.execute(f"UPDATE categories SET {key} = ? WHERE id = ?", (value, category_id))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Une catégorie porte déjà ce nom."


# ---------- Playlists : arborescence + CRUD complet ----------

def get_playlists():
    """Retourne toutes les playlists (liste plate, pas de sous-playlists),
    triées par nom, avec le nombre de vidéos associées."""
    conn = get_db()
    rows = conn.execute(
        """SELECT p.*, COUNT(v.id) as n FROM playlists p
           LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
           LEFT JOIN videos v ON v.id = pi.video_id AND v.missing = 0 AND v.disk_off = 0
           GROUP BY p.id ORDER BY p.name COLLATE NOCASE"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_playlist(playlist_id):
    conn = get_db()
    return conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()


def _playlist_descendant_ids(conn, playlist_id):
    """ids de playlist_id et de toutes ses sous-playlists (à n'importe quelle
    profondeur), pour empêcher de créer une boucle parent/enfant quand on
    déplace une playlist sous une autre."""
    ids = {playlist_id}
    frontier = [playlist_id]
    while frontier:
        current = frontier.pop()
        rows = conn.execute("SELECT id FROM playlists WHERE parent_id = ?", (current,)).fetchall()
        for r in rows:
            if r["id"] not in ids:
                ids.add(r["id"])
                frontier.append(r["id"])
    return ids


def create_playlist(name, parent_id=None, manual_creation_date=None):
    """manual_creation_date : obligatoire si parent_id est fourni (création
    d'une sous-playlist) — chaîne YYYY-MM-DD (celui de <input type="date">).
    Ignoré/non stocké pour une playlist principale (parent_id absent).

    Profondeur maximale volontairement limitée à 2 niveaux (playlist
    principale > sous-playlist) : on refuse de créer une sous-playlist à
    l'intérieur d'une sous-playlist ("sous-sous-playlist")."""
    name = name.strip()
    if not name:
        return None, "Le nom ne peut pas être vide."
    conn = get_db()
    if parent_id:
        prow = conn.execute("SELECT id, parent_id FROM playlists WHERE id = ?", (parent_id,)).fetchone()
        if not prow:
            parent_id = None
        elif prow["parent_id"]:
            return None, "Impossible de créer une sous-playlist à l'intérieur d'une sous-playlist (2 niveaux maximum : playlist > sous-playlist)."
    manual_creation_date = (manual_creation_date or "").strip()
    if parent_id and not manual_creation_date:
        return None, "La date de création (saisie manuelle) est obligatoire pour une sous-playlist."
    try:
        cur = conn.execute(
            "INSERT INTO playlists(name, parent_id, manual_creation_date) VALUES (?, ?, ?)",
            (name, parent_id, manual_creation_date if parent_id else ""),
        )
        conn.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "Une playlist porte déjà ce nom."


def update_playlist_full(playlist_id, name, rating, comment, images, parent_id=None, change_parent=False, manual_creation_date=None):
    """images: dict optionnel {'image1': path_ou_None, ...}.
    change_parent: si True, met à jour la playlist parente (parent_id=None
    pour en faire une playlist principale) ; si False, la parenté actuelle
    n'est pas touchée.
    manual_creation_date: si fourni (non None), met à jour la date de
    création saisie manuellement (chaîne YYYY-MM-DD, ou '' pour l'effacer).
    Obligatoire (non vide) dès lors que la playlist est (ou devient) une
    sous-playlist ; si l'appelant ne l'envoie pas (None) alors que la
    playlist est déjà une sous-playlist, la valeur déjà en base est
    réutilisée pour la vérification (elle n'est pas écrasée)."""
    name = name.strip()
    if not name:
        return False, "Le nom ne peut pas être vide."
    conn = get_db()
    if change_parent and parent_id:
        if parent_id in _playlist_descendant_ids(conn, playlist_id):
            return False, "Une playlist ne peut pas devenir sa propre sous-playlist."
        # Profondeur maximale volontairement limitée à 2 niveaux (playlist
        # principale > sous-playlist) : on refuse qu'une sous-playlist
        # devienne à son tour la parente d'une autre ("sous-sous-playlist"),
        # et qu'une playlist ayant déjà ses propres sous-playlists devienne
        # elle-même une sous-playlist (ce qui pousserait ses enfants à un
        # 3e niveau).
        parent_row = conn.execute("SELECT parent_id FROM playlists WHERE id = ?", (parent_id,)).fetchone()
        if parent_row and parent_row["parent_id"]:
            return False, "Impossible de choisir une sous-playlist comme playlist parente (2 niveaux maximum : playlist > sous-playlist)."
        has_children = conn.execute("SELECT 1 FROM playlists WHERE parent_id = ? LIMIT 1", (playlist_id,)).fetchone()
        if has_children:
            return False, "Cette playlist a ses propres sous-playlists : elle ne peut pas devenir elle-même une sous-playlist (2 niveaux maximum)."
    if change_parent:
        resulting_parent_id = parent_id
    else:
        current = conn.execute("SELECT parent_id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        resulting_parent_id = current["parent_id"] if current else None
    if resulting_parent_id:
        effective_date = manual_creation_date
        if effective_date is None:
            row = conn.execute("SELECT manual_creation_date FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            effective_date = row["manual_creation_date"] if row else ""
        if not (effective_date or "").strip():
            return False, "La date de création (saisie manuelle) est obligatoire pour une sous-playlist."
    try:
        conn.execute(
            "UPDATE playlists SET name=?, rating=?, comment=? WHERE id=?",
            (name, rating, comment, playlist_id),
        )
        if change_parent:
            conn.execute("UPDATE playlists SET parent_id = ? WHERE id = ?", (parent_id, playlist_id))
        if manual_creation_date is not None:
            conn.execute(
                "UPDATE playlists SET manual_creation_date = ? WHERE id = ?",
                (manual_creation_date.strip(), playlist_id),
            )
        for key, value in images.items():
            if key in ("image1", "image2", "image3") and value is not None:
                conn.execute(f"UPDATE playlists SET {key} = ? WHERE id = ?", (value, playlist_id))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Une playlist porte déjà ce nom."


def get_playlists_tree():
    """Retourne les playlists principales (parent_id NULL), chacune avec sa
    liste de sous-playlists imbriquée dans 'children' (récursif)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT p.*, COUNT(v.id) as n FROM playlists p
           LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
           LEFT JOIN videos v ON v.id = pi.video_id AND v.missing = 0 AND v.disk_off = 0
           GROUP BY p.id ORDER BY p.name COLLATE NOCASE"""
    ).fetchall()
    nodes = {r["id"]: dict(r, children=[]) for r in rows}
    roots = []
    for r in rows:
        node = nodes[r["id"]]
        pid = r["parent_id"]
        if pid and pid in nodes:
            nodes[pid]["children"].append(node)
        else:
            roots.append(node)
    return roots


def get_playlist_children(playlist_id):
    """Sous-playlists directes d'une playlist, triées par nom, avec le
    nombre de vidéos propres à chacune."""
    conn = get_db()
    rows = conn.execute(
        """SELECT p.*, COUNT(v.id) as n FROM playlists p
           LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
           LEFT JOIN videos v ON v.id = pi.video_id AND v.missing = 0 AND v.disk_off = 0
           WHERE p.parent_id = ?
           GROUP BY p.id ORDER BY p.name COLLATE NOCASE""",
        (playlist_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_playlist_breadcrumb(playlist_id):
    """Chaîne des playlists parentes (de la racine jusqu'au parent direct,
    sans inclure playlist_id lui-même) — pour le fil d'ariane."""
    conn = get_db()
    chain = []
    current = conn.execute(
        "SELECT id, name, parent_id FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    guard = 0
    while current and current["parent_id"] and guard < 20:
        parent = conn.execute(
            "SELECT id, name, parent_id FROM playlists WHERE id = ?", (current["parent_id"],)
        ).fetchone()
        if not parent:
            break
        chain.append({"id": parent["id"], "name": parent["name"]})
        current = parent
        guard += 1
    chain.reverse()
    return chain


# ---------- Métadonnées vidéo étendues (note / date / commentaire) ----------

def update_video_extended(video_id, rating, video_date, comment, views=None):
    conn = get_db()
    if views is None:
        conn.execute(
            "UPDATE videos SET rating=?, video_date=?, comment=? WHERE id=?",
            (rating, video_date, comment, video_id),
        )
    else:
        views = max(0, int(views))
        conn.execute(
            "UPDATE videos SET rating=?, video_date=?, comment=?, views=? WHERE id=?",
            (rating, video_date, comment, views, video_id),
        )
    conn.commit()

# ---------- Recommandations ----------

def get_recommendations(limit=12):
    """Suggère des vidéos partageant des tags avec les vidéos les plus
    vues, pour un effet 'basé sur ton historique' simple et local."""
    conn = get_db()
    top_tags = conn.execute(
        """SELECT t.id FROM tags t
           JOIN video_tags vt ON vt.tag_id = t.id
           JOIN videos v ON v.id = vt.video_id
           WHERE v.views > 0
           GROUP BY t.id ORDER BY SUM(v.views) DESC LIMIT 5"""
    ).fetchall()
    tag_ids = [r["id"] for r in top_tags]
    if not tag_ids:
        return []
    placeholders = ",".join("?" * len(tag_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT v.* FROM videos v
            JOIN video_tags vt ON vt.video_id = v.id
            WHERE vt.tag_id IN ({placeholders}) AND v.missing = 0 AND v.disk_off = 0
            ORDER BY RANDOM() LIMIT ?""",
        tag_ids + [limit],
    ).fetchall()
    return rows


# ---------- Export / import de bibliothèque (JSON) ----------
#
# Objectif : sauvegarder/migrer les métadonnées (catégories, tags,
# playlists + leur contenu, et les métadonnées par vidéo : titre
# personnalisé, description, note, favori, commentaire, date, catégorie,
# tags) dans un fichier JSON lisible et portable, SANS dépendre du fichier
# SQLite brut (qui n'est pas garanti stable d'une version à l'autre et
# contient aussi des chemins de vignettes/caches locaux non pertinents
# pour une sauvegarde de métadonnées).
#
# Le rattachement vidéo se fait par 'path' (chemin complet du fichier sur
# disque), qui est la clé UNIQUE de la table videos : c'est ce qui permet
# de réimporter proprement sur une bibliothèque déjà scannée (les fichiers
# eux-mêmes ne sont ni exportés ni requis dans le JSON).

EXPORT_FORMAT_VERSION = 3  # v3 : ajout de "content_hash" par vidéo, pour
# retrouver une vidéo renommée ET déplacée depuis l'export (le chemin ET
# le nom de fichier ont changé, mais le contenu du fichier, lui, n'a pas
# bougé) ; v2 : ajout de "images" (noms de fichiers de couverture) sur
# categories/tags/playlists. Un export v1 ou v2 (sans ces clés) reste
# importable normalement, avec un repli moins précis (voir import_library).


def export_library():
    """Construit un dict JSON-sérialisable représentant l'intégralité des
    métadonnées de la bibliothèque (hors fichiers vidéo, qui restent sur
    disque). Les catégories/tags/playlists incluent la liste des NOMS de
    fichiers de leurs images de couverture (clé "images") ; les fichiers
    image eux-mêmes ne sont pas lus ici — c'est /library/export (app.py)
    qui les joint physiquement dans le zip exporté, à côté de ce JSON.
    Purement une lecture : n'écrit rien en base."""
    conn = get_db()

    categories = [
        {
            "name": r["name"], "rating": r["rating"] or 0, "comment": r["comment"] or "",
            "images": [img for img in (r["image1"], r["image2"], r["image3"]) if img],
        }
        for r in conn.execute(
            "SELECT name, rating, comment, image1, image2, image3 FROM categories ORDER BY name"
        ).fetchall()
    ]

    tags = [
        {
            "name": r["name"],
            "images": [img for img in (r["image1"], r["image2"], r["image3"]) if img],
        }
        for r in conn.execute(
            "SELECT name, image1, image2, image3 FROM tags ORDER BY name"
        ).fetchall()
    ]

    playlist_rows = conn.execute(
        """SELECT p.id, p.name, p.rating, p.comment, p.manual_creation_date,
                  p.image1, p.image2, p.image3,
                  parent.name AS parent_name
           FROM playlists p
           LEFT JOIN playlists parent ON parent.id = p.parent_id
           ORDER BY p.name"""
    ).fetchall()
    playlists = []
    for p in playlist_rows:
        items = conn.execute(
            """SELECT v.path FROM playlist_items pi
               JOIN videos v ON v.id = pi.video_id
               WHERE pi.playlist_id = ? ORDER BY pi.position ASC""",
            (p["id"],),
        ).fetchall()
        playlists.append({
            "name": p["name"],
            "parent": p["parent_name"],
            "rating": p["rating"] or 0,
            "comment": p["comment"] or "",
            "manual_creation_date": p["manual_creation_date"] or "",
            "images": [img for img in (p["image1"], p["image2"], p["image3"]) if img],
            "items": [it["path"] for it in items],
        })

    video_rows = conn.execute(
        """SELECT v.id, v.path, v.title, v.title_is_custom, v.description,
                  v.rating, v.favorite, v.comment, v.video_date, v.content_hash,
                  v.size_bytes,
                  c.name AS category_name
           FROM videos v
           LEFT JOIN categories c ON c.id = v.category_id
           ORDER BY v.path"""
    ).fetchall()
    videos = []
    for v in video_rows:
        vtags = conn.execute(
            """SELECT t.name FROM tags t
               JOIN video_tags vt ON vt.tag_id = t.id
               WHERE vt.video_id = ? ORDER BY t.name""",
            (v["id"],),
        ).fetchall()
        videos.append({
            "path": v["path"],
            "title": v["title"],
            "title_is_custom": bool(v["title_is_custom"]),
            "description": v["description"] or "",
            "category": v["category_name"],
            "tags": [t["name"] for t in vtags],
            "rating": v["rating"] or 0,
            "favorite": bool(v["favorite"]),
            "comment": v["comment"] or "",
            "video_date": v["video_date"] or "",
            "content_hash": v["content_hash"] or "",
            # Taille en octets : signal supplémentaire utilisé à l'import
            # pour lever une ambiguïté de noms de fichiers identiques (voir
            # import_library). Absente des exports plus anciens : l'import
            # s'en passe alors simplement.
            "size_bytes": v["size_bytes"] or 0,
        })

    return {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "app": "Médiathèque",
        "categories": categories,
        "tags": tags,
        "playlists": playlists,
        "videos": videos,
    }


def import_library(data):
    """Réimporte un JSON produit par export_library() : opération
    purement ADDITIVE/de fusion — ne supprime jamais de catégorie, tag,
    playlist ou vidéo existante. Le rattachement des vidéos se fait par
    'path' exact, avec repli par empreinte de contenu (content_hash) puis
    par nom de fichier seul si la vidéo a été déplacée et/ou renommée
    depuis l'export (voir plus bas) : seule sa PRÉSENCE dans la
    bibliothèque actuelle (déjà scannée) compte, jamais son ancien chemin
    ni son ancien nom. Une vidéo du JSON introuvable dans la bibliothèque
    actuelle est simplement ignorée (comptée dans 'videos_not_found' mais
    jamais signalée à l'utilisateur) plutôt que de provoquer une erreur —
    utile pour migrer une bibliothèque partiellement présente sur la
    machine cible ; il suffit de relancer un scan puis un nouvel import
    plus tard pour que ses infos reviennent dès qu'elle est retrouvée.

    Doublons (plusieurs vidéos présentes partageant le même content_hash,
    numérotées 'N°1 ...', 'N°2 ...') : le repli par content_hash/nom ne
    peut jamais déterminer avec certitude à quelle copie précise une
    entrée du JSON correspondait à l'origine, donc il ne devine jamais —
    seule LA vidéo "originale" du groupe (celle pas encore numérotée, ou
    la plus ancienne à défaut) reçoit les infos réattribuées ; les autres
    copies sont traitées comme de nouvelles vidéos et n'en reçoivent
    jamais par ce biais (un chemin exact reste, lui, toujours honoré,
    puisqu'il ne laisse place à aucune ambiguïté).

    Images de couverture (categories/tags/playlists, clé "images", export
    v2+) : cette fonction ne fait qu'ENREGISTRER les noms de fichiers en
    base (image1/2/3) — elle ne touche elle-même à aucun fichier sur
    disque. C'est à l'appelant (voir /library/import dans app.py, pour un
    export au format zip) de placer les fichiers image correspondants dans
    data/category_images, data/playlist_images, data/tag_images AVANT
    d'appeler import_library(), avec exactement les noms de fichiers
    présents dans "images". Sans cette étape préalable (ex: un JSON nu
    réimporté seul, sans le zip), les noms sont bien enregistrés mais
    pointent vers des fichiers absents.

    Renvoie un dict de statistiques pour affichage à l'utilisateur."""
    if not isinstance(data, dict):
        raise ValueError("structure JSON racine invalide")

    # Les exports historiques n'ont pas toujours toutes ces clés. On
    # normalise aussi les valeurs présentes mais mal typées afin qu'un JSON
    # partiellement édité ne fasse pas planter l'import au milieu de la
    # bibliothèque.
    for key in ("categories", "tags", "playlists", "videos"):
        value = data.get(key, [])
        if value is None:
            data[key] = []
        elif not isinstance(value, list):
            raise ValueError(f"la clé « {key} » doit contenir une liste")

    def _safe_int(value, default=0, minimum=None, maximum=None):
        try:
            result = int(value or default)
        except (TypeError, ValueError, OverflowError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    conn = get_db()
    stats = {
        "categories": 0, "tags": 0, "playlists": 0,
        "videos_updated": 0, "videos_not_found": 0, "playlist_items_linked": 0,
    }

    # ---- Catégories ----
    for cat in data.get("categories", []):
        name = (cat.get("name") or "").strip()
        if not name:
            continue
        cat_id = get_or_create_category(name)
        if cat_id is None:
            continue
        conn.execute(
            "UPDATE categories SET rating = ?, comment = ? WHERE id = ?",
            (_safe_int(cat.get("rating"), maximum=5), cat.get("comment") or "", cat_id),
        )
        # Images de couverture : seulement si le JSON en fournit (export v2
        # et le fichier a bien été replacé sur disque par app.py avant cet
        # appel) — sinon on ne touche pas aux images déjà en place
        # (fusion additive, jamais de suppression).
        for i, fname in enumerate((cat.get("images") or [])[:3], start=1):
            if fname:
                conn.execute(f"UPDATE categories SET image{i} = ? WHERE id = ?", (fname, cat_id))
        stats["categories"] += 1
    conn.commit()

    # ---- Tags ----
    for tag in data.get("tags", []):
        name = (tag.get("name") or "").strip()
        if not name:
            continue
        tag_id = get_or_create_tag(name)
        if tag_id is None:
            continue
        for i, fname in enumerate((tag.get("images") or [])[:3], start=1):
            if fname:
                conn.execute(f"UPDATE tags SET image{i} = ? WHERE id = ?", (fname, tag_id))
        stats["tags"] += 1
    conn.commit()

    # ---- Playlists : d'abord toutes les créer/mettre à jour (sans lien
    # parent), puis rattacher les parentés une fois que tous les noms
    # existent en base — nécessaire car une sous-playlist peut apparaître
    # dans le JSON avant sa playlist parente. ----
    playlists_in = [p for p in data.get("playlists", []) if (p.get("name") or "").strip()]
    for pl in playlists_in:
        name = pl["name"].strip()
        row = conn.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()
        if row:
            pl_id = row["id"]
        else:
            pl_id, _err = create_playlist(name)
            if pl_id is None:
                continue
        conn.execute(
            "UPDATE playlists SET rating = ?, comment = ?, manual_creation_date = ? WHERE id = ?",
            (
                _safe_int(pl.get("rating"), maximum=5), pl.get("comment") or "",
                pl.get("manual_creation_date") or "", pl_id,
            ),
        )
        for i, fname in enumerate((pl.get("images") or [])[:3], start=1):
            if fname:
                conn.execute(f"UPDATE playlists SET image{i} = ? WHERE id = ?", (fname, pl_id))
        stats["playlists"] += 1
    conn.commit()

    for pl in playlists_in:
        parent_name = (pl.get("parent") or "").strip()
        if not parent_name:
            continue
        child = conn.execute("SELECT id FROM playlists WHERE name = ?", (pl["name"].strip(),)).fetchone()
        parent = conn.execute("SELECT id FROM playlists WHERE name = ?", (parent_name,)).fetchone()
        if child and parent and child["id"] != parent["id"]:
            conn.execute("UPDATE playlists SET parent_id = ? WHERE id = ?", (parent["id"], child["id"]))
    conn.commit()

    # ---- Vidéos : réattribution des métadonnées par correspondance
    # progressive, du signal le plus sûr au plus souple. Une vidéo peut
    # avoir changé de disque, de dossier, de casse ou de nom depuis
    # l'export : on essaie donc, dans l'ordre,
    #   1. le chemin exact ;
    #   2. le chemin "normalisé" (séparateurs \ / unifiés, casse ignorée,
    #      lettre de disque mise à part) — couvre D:\Films vs d:/films ;
    #   3. le content_hash (empreinte du CONTENU : insensible au
    #      déplacement ET au renommage, signal le plus fort quand il est
    #      disponible des deux côtés) ;
    #   4. nom de fichier + dossier parent (lève l'ambiguïté quand plusieurs
    #      vidéos portent le même nom de fichier dans des dossiers
    #      différents, cas très courant : "video.mp4", "01.mp4"...) ;
    #   5. nom de fichier + taille en octets ;
    #   6. nom de fichier seul, s'il est unique dans la bibliothèque.
    # Les vidéos actuellement marquées manquantes (disque débranché, dossier
    # filtré) restent candidates, mais en dernier recours seulement : leurs
    # infos sont ainsi réattribuées tout de suite plutôt que perdues jusqu'à
    # un prochain scan. Chaque vidéo de la base ne reçoit les infos que
    # d'une seule entrée du JSON par import.
    #
    # DOUBLONS (plusieurs vidéos présentes partagent le même content_hash,
    # numérotées 'N°1 ...', 'N°2 ...' par reconcile_duplicate_titles) :
    # un repli par content_hash/nom ne peut jamais savoir à laquelle des
    # copies une entrée du JSON correspondait à l'origine. On choisit donc
    # UNE SEULE fois, pour tout le groupe, LA vidéo "originale" à qui les
    # infos seront données (en priorité celle pas encore numérotée, sinon la
    # plus ancienne) ; les autres copies sont exclues de ces replis. Un
    # chemin exact, lui, reste toujours honoré : aucune ambiguïté.
    # Symétriquement, si le JSON contient plusieurs entrées de même
    # empreinte, c'est celle qui représentait alors l'originale (titre non
    # préfixé 'N°x ') qui est retenue, indépendamment de l'ordre du fichier.
    def _pick_original(candidates):
        unnumbered = [c for c in candidates if not _DUP_TITLE_RE.match(c["title"] or "")]
        pool = unnumbered if unnumbered else list(candidates)
        return min(pool, key=lambda c: c["id"])

    def _norm_path(path):
        """Chemin comparable d'une machine/plateforme à l'autre : séparateurs
        unifiés, casse ignorée (les systèmes de fichiers Windows ne la
        distinguent pas), séparateur final supprimé."""
        return (path or "").replace("\\", "/").strip().rstrip("/").lower()

    def _base_name(path):
        """basename indépendant de l'OS : un export fait sous Windows
        contient des '\', et os.path.basename ne les reconnaît pas quand
        l'application tourne sous Linux/macOS (et inversement)."""
        return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    def _parent_name(path):
        parts = (path or "").replace("\\", "/").rstrip("/").split("/")
        return parts[-2].lower() if len(parts) >= 2 else ""

    # Index en mémoire de la bibliothèque actuelle : une seule lecture, puis
    # toutes les correspondances se font sans requête supplémentaire.
    db_rows = conn.execute(
        """SELECT id, path, filename, title, size_bytes, content_hash, missing
           FROM videos"""
    ).fetchall()

    def _better(a, b):
        """Entre deux candidats équivalents : présent avant manquant, puis
        le plus ancien (id le plus petit)."""
        if a is None:
            return b
        if bool(a["missing"]) != bool(b["missing"]):
            return a if not a["missing"] else b
        return a if a["id"] <= b["id"] else b

    by_path, by_norm_path = {}, {}
    by_hash, by_parent_name, by_name_size, by_name, by_stem = {}, {}, {}, {}, {}
    rows_by_id = {}
    for r in db_rows:
        rows_by_id[r["id"]] = r
        path = r["path"] or ""
        name = (r["filename"] or _base_name(path)).lower()
        by_path[path] = _better(by_path.get(path), r)
        np = _norm_path(path)
        by_norm_path[np] = _better(by_norm_path.get(np), r)
        ch = (r["content_hash"] or "").strip()
        if ch:
            by_hash.setdefault(ch, []).append(r)
        if name:
            by_name.setdefault(name, []).append(r)
            by_parent_name.setdefault((_parent_name(path), name), []).append(r)
            if r["size_bytes"]:
                by_name_size.setdefault((name, int(r["size_bytes"])), []).append(r)
            stem = name.rsplit(".", 1)[0] if "." in name else name
            if stem:
                by_stem.setdefault(stem, []).append(r)
                if r["size_bytes"]:
                    by_name_size.setdefault((stem, int(r["size_bytes"])), []).append(r)

    videos_in = data.get("videos", [])
    entries_by_hash = {}
    for idx, v in enumerate(videos_in):
        ch = (v.get("content_hash") or "").strip()
        if ch:
            entries_by_hash.setdefault(ch, []).append(idx)
    winning_entry_by_hash = {}
    for ch, idxs in entries_by_hash.items():
        if len(idxs) == 1:
            winning_entry_by_hash[ch] = idxs[0]
        else:
            unnumbered_idxs = [
                i for i in idxs if not _DUP_TITLE_RE.match((videos_in[i].get("title") or ""))
            ]
            # Choix déterministe quand plusieurs entrées JSON partagent le
            # même hash : on privilégie l'entrée non numérotée (originale),
            # sinon l'entrée la plus ancienne (premier indice) du lot.
            winning_entry_by_hash[ch] = unnumbered_idxs[0] if len(unnumbered_idxs) == 1 else min(idxs)

    matched_video_ids = set()
    demoted_ids = set()      # copies de doublons écartées des replis ambigus
    resolved_hash = {}       # content_hash -> id de "l'originale" (ou None)
    path_to_video_id = {}    # ancien chemin du JSON -> id retenu en base

    def _unique(candidates):
        """Un candidat exploitable seulement si le groupe ne laisse aucune
        ambiguïté (une seule vidéo encore libre, doublons écartés).
        Quand plusieurs candidats restent, on choisit le meilleur via
        _better() (présent avant manquant, puis plus ancien par id)
        plutôt que de renoncer — évite la perte de métadonnées JSON."""
        pool = [
            c for c in candidates
            if c["id"] not in demoted_ids and c["id"] not in matched_video_ids
        ]
        if not pool:
            return None
        present = [c for c in pool if not c["missing"]]
        pool = present if present else pool
        if len(pool) == 1:
            return pool[0]
        # Plusieurs candidats : on prend le meilleur de façon déterministe
        best = pool[0]
        for c in pool[1:]:
            best = _better(best, c)
        return best

    def _resolve_entry(v, idx=None):
        """Retrouve la vidéo de la bibliothèque actuelle correspondant à une
        entrée du JSON, ou None si rien de certain."""
        path = v.get("path") or ""
        if not path:
            return None

        row = by_path.get(path)
        if row is None:
            row = by_norm_path.get(_norm_path(path))
        if row is not None and row["id"] not in matched_video_ids:
            return row

        content_hash = (v.get("content_hash") or "").strip()
        if content_hash and (idx is None or winning_entry_by_hash.get(content_hash) == idx):
            if content_hash not in resolved_hash:
                candidates = by_hash.get(content_hash) or []
                if not candidates:
                    resolved_hash[content_hash] = None
                elif len(candidates) == 1:
                    resolved_hash[content_hash] = candidates[0]["id"]
                else:
                    original = _pick_original(candidates)
                    resolved_hash[content_hash] = original["id"]
                    demoted_ids.update(c["id"] for c in candidates if c["id"] != original["id"])
            target_id = resolved_hash[content_hash]
            if target_id is not None and target_id not in matched_video_ids:
                return rows_by_id[target_id]

        name = _base_name(path).lower()
        if not name:
            return None
        size_b = int(v.get("size_bytes") or 0)
        stem = name.rsplit(".", 1)[0] if "." in name else name
        row = _unique(by_parent_name.get((_parent_name(path), name)) or [])
        if row is None and size_b:
            row = _unique(by_name_size.get((name, size_b)) or [])
        if row is None and size_b and stem:
            row = _unique(by_name_size.get((stem, size_b)) or [])
        if row is None:
            row = _unique(by_name.get(name) or [])
        # Repli sur le nom sans extension (.mp4 → .mkv, chemin totalement
        # différent mais même fichier logique).
        if row is None and stem:
            row = _unique(by_stem.get(stem) or [])
        return row

    def _apply_video_meta(video_id, v):
        category_id = None
        cat_name = (v.get("category") or "").strip()
        if cat_name:
            category_id = get_or_create_category(cat_name)

        rating = _safe_int(v.get("rating"), minimum=0, maximum=5)
        favorite = 1 if v.get("favorite") else 0
        title_is_custom = 1 if v.get("title_is_custom") else 0
        title = (v.get("title") or "").strip()

        if title_is_custom and title:
            conn.execute(
                """UPDATE videos SET title = ?, title_is_custom = 1, description = ?,
                       category_id = ?, rating = ?, favorite = ?, comment = ?, video_date = ?
                   WHERE id = ?""",
                (
                    title, v.get("description") or "", category_id, rating, favorite,
                    v.get("comment") or "", v.get("video_date") or "", video_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE videos SET description = ?, category_id = ?, rating = ?,
                       favorite = ?, comment = ?, video_date = ? WHERE id = ?""",
                (
                    v.get("description") or "", category_id, rating, favorite,
                    v.get("comment") or "", v.get("video_date") or "", video_id,
                ),
            )
        set_video_tags(video_id, v.get("tags") or [])

    # Boucle d'import des vidéos : try/commit/rollback pour éviter un état
    # incohérent en cas de crash. Commit intermédiaire toutes les 50 vidéos.
    _video_import_count = 0
    unmatched_for_pending = []
    try:
        for idx, v in enumerate(videos_in):
            row = _resolve_entry(v, idx)
            if row is None:
                # Absente pour l'instant : mise en file d'attente pour
                # réattribution automatique dès qu'un scan l'indexe
                # (chemin différent ou fichier pas encore présent).
                stats["videos_not_found"] += 1
                if isinstance(v, dict) and (v.get("path") or v.get("content_hash") or v.get("title")):
                    unmatched_for_pending.append(v)
                continue
            video_id = row["id"]
            matched_video_ids.add(video_id)
            if v.get("path"):
                path_to_video_id[v["path"]] = video_id
                path_to_video_id[_norm_path(v["path"])] = video_id

            _apply_video_meta(video_id, v)
            stats["videos_updated"] += 1
            _video_import_count += 1
            if _video_import_count % 50 == 0:
                conn.commit()
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
        raise ValueError(f"entrée vidéo invalide : {exc}") from exc

    # ---- Contenu des playlists ----
    for pl in playlists_in:
        row = conn.execute("SELECT id FROM playlists WHERE name = ?", (pl["name"].strip(),)).fetchone()
        if not row:
            continue
        pl_id = row["id"]
        position = 0
        seen_ids = set()
        for path in pl.get("items") or []:
            if not path:
                continue
            video_id = path_to_video_id.get(path) or path_to_video_id.get(_norm_path(path))
            if video_id is None:
                name = _base_name(path).lower()
                stem = name.rsplit(".", 1)[0] if "." in name else name
                candidate = (
                    by_path.get(path)
                    or by_norm_path.get(_norm_path(path))
                    or _unique(by_parent_name.get((_parent_name(path), name)) or [])
                    or _unique(by_name.get(name) or [])
                    or _unique(by_stem.get(stem) or [])
                )
                if candidate is None:
                    continue
                video_id = candidate["id"]
                path_to_video_id[path] = video_id
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            cur = conn.execute(
                "INSERT OR IGNORE INTO playlist_items(playlist_id, video_id, position) VALUES (?, ?, ?)",
                (pl_id, video_id, position),
            )
            position += 1
            if cur.rowcount:
                stats["playlist_items_linked"] += 1
    conn.commit()

    # File d'attente : vidéos du JSON non rattachées → réessai au prochain scan.
    try:
        _merge_pending_library_entries(unmatched_for_pending)
        # Tente aussi d'appliquer d'anciennes entrées en attente (nouveaux
        # hashes calculés, etc.).
        pending_stats = apply_pending_library_metadata(conn=conn)
        stats["videos_updated"] += pending_stats.get("videos_updated", 0)
        stats["pending_remaining"] = pending_stats.get("pending_remaining", 0)
    except Exception:
        # Ne jamais faire échouer l'import pour un problème de file d'attente.
        stats["pending_remaining"] = len(unmatched_for_pending)

    return stats


# ---------------------------------------------------------------------------
# File d'attente d'import : réattribution dès qu'une vidéo est scannée
# ---------------------------------------------------------------------------

def _load_pending_library():
    try:
        if not os.path.isfile(PENDING_LIBRARY_PATH):
            return []
        with open(PENDING_LIBRARY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            entries = data.get("videos") or []
        elif isinstance(data, list):
            entries = data
        else:
            entries = []
        return [e for e in entries if isinstance(e, dict)]
    except Exception:
        return []


def _save_pending_library(entries):
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        payload = {
            "version": 1,
            "videos": entries[:5000],  # plafond de sécurité
        }
        tmp = PENDING_LIBRARY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=0)
        os.replace(tmp, PENDING_LIBRARY_PATH)
    except Exception:
        pass


def _pending_entry_key(v):
    """Clé stable pour dédupliquer les entrées en attente."""
    ch = (v.get("content_hash") or "").strip()
    if ch:
        return "h:" + ch
    path = (v.get("path") or "").replace("\\", "/").strip().lower()
    if path:
        return "p:" + path
    name = path.rsplit("/", 1)[-1] if path else ""
    size = str(int(v.get("size_bytes") or 0))
    title = (v.get("title") or "").strip().lower()
    return "n:" + name + "|" + size + "|" + title


def _merge_pending_library_entries(new_entries):
    if not new_entries:
        return
    existing = _load_pending_library()
    by_key = {_pending_entry_key(e): e for e in existing}
    for e in new_entries:
        by_key[_pending_entry_key(e)] = e
    _save_pending_library(list(by_key.values()))


def apply_pending_library_metadata(conn=None):
    """Réattribue les métadonnées JSON en attente aux vidéos désormais
    présentes en base (après un scan, ou juste après un import partiel).
    Opération additive, sans jamais planter l'appelant.
    Retourne {videos_updated, pending_remaining}."""
    stats = {"videos_updated": 0, "pending_remaining": 0}
    pending = _load_pending_library()
    if not pending:
        return stats

    own_conn = conn is None
    if own_conn:
        try:
            conn = get_db()
        except Exception:
            return stats

    try:
        def _norm_path(path):
            return (path or "").replace("\\", "/").strip().rstrip("/").lower()

        def _base_name(path):
            return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

        def _parent_name(path):
            parts = (path or "").replace("\\", "/").rstrip("/").split("/")
            return parts[-2].lower() if len(parts) >= 2 else ""

        def _better(a, b):
            if a is None:
                return b
            if bool(a["missing"]) != bool(b["missing"]):
                return a if not a["missing"] else b
            return a if a["id"] <= b["id"] else b

        db_rows = conn.execute(
            """SELECT id, path, filename, title, size_bytes, content_hash, missing
               FROM videos"""
        ).fetchall()

        by_path, by_norm_path = {}, {}
        by_hash, by_name, by_stem, by_name_size, by_parent_name = {}, {}, {}, {}, {}
        for r in db_rows:
            path = r["path"] or ""
            name = (r["filename"] or _base_name(path)).lower()
            by_path[path] = _better(by_path.get(path), r)
            by_norm_path[_norm_path(path)] = _better(by_norm_path.get(_norm_path(path)), r)
            ch = (r["content_hash"] or "").strip()
            if ch:
                by_hash.setdefault(ch, []).append(r)
            if name:
                by_name.setdefault(name, []).append(r)
                stem = name.rsplit(".", 1)[0] if "." in name else name
                if stem:
                    by_stem.setdefault(stem, []).append(r)
                by_parent_name.setdefault((_parent_name(path), name), []).append(r)
                if r["size_bytes"]:
                    by_name_size.setdefault((name, int(r["size_bytes"])), []).append(r)
                    if stem:
                        by_name_size.setdefault((stem, int(r["size_bytes"])), []).append(r)

        def _unique(candidates):
            present = [c for c in candidates if not c["missing"]]
            pool = present if present else list(candidates)
            if not pool:
                return None
            best = pool[0]
            for c in pool[1:]:
                best = _better(best, c)
            return best

        def _resolve(v):
            path = v.get("path") or ""
            row = by_path.get(path) if path else None
            if row is None and path:
                row = by_norm_path.get(_norm_path(path))
            ch = (v.get("content_hash") or "").strip()
            if row is None and ch:
                cands = by_hash.get(ch) or []
                if len(cands) == 1:
                    row = cands[0]
                elif cands:
                    unnumbered = [c for c in cands if not _DUP_TITLE_RE.match(c["title"] or "")]
                    pool = unnumbered if unnumbered else cands
                    row = min(pool, key=lambda c: c["id"])
            name = _base_name(path).lower() if path else ""
            stem = name.rsplit(".", 1)[0] if "." in name else name
            size_b = int(v.get("size_bytes") or 0)
            if row is None and name:
                row = _unique(by_parent_name.get((_parent_name(path), name)) or [])
            if row is None and name and size_b:
                row = _unique(by_name_size.get((name, size_b)) or [])
            if row is None and stem and size_b:
                row = _unique(by_name_size.get((stem, size_b)) or [])
            if row is None and name:
                row = _unique(by_name.get(name) or [])
            if row is None and stem:
                row = _unique(by_stem.get(stem) or [])
            return row

        remaining = []
        updated = 0
        for v in pending:
            try:
                row = _resolve(v)
                if row is None:
                    remaining.append(v)
                    continue
                video_id = row["id"]
                category_id = None
                cat_name = (v.get("category") or "").strip()
                if cat_name:
                    category_id = get_or_create_category(cat_name)
                rating = 0
                try:
                    rating = max(0, min(5, int(v.get("rating") or 0)))
                except (TypeError, ValueError):
                    rating = 0
                favorite = 1 if v.get("favorite") else 0
                title_is_custom = 1 if v.get("title_is_custom") else 0
                title = (v.get("title") or "").strip()
                if title_is_custom and title:
                    conn.execute(
                        """UPDATE videos SET title = ?, title_is_custom = 1, description = ?,
                               category_id = ?, rating = ?, favorite = ?, comment = ?, video_date = ?
                           WHERE id = ?""",
                        (
                            title, v.get("description") or "", category_id, rating, favorite,
                            v.get("comment") or "", v.get("video_date") or "", video_id,
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE videos SET description = ?, category_id = ?, rating = ?,
                               favorite = ?, comment = ?, video_date = ? WHERE id = ?""",
                        (
                            v.get("description") or "", category_id, rating, favorite,
                            v.get("comment") or "", v.get("video_date") or "", video_id,
                        ),
                    )
                set_video_tags(video_id, v.get("tags") or [])
                updated += 1
            except Exception:
                remaining.append(v)
                continue

        conn.commit()
        _save_pending_library(remaining)
        stats["videos_updated"] = updated
        stats["pending_remaining"] = len(remaining)
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    return stats
