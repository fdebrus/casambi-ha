"""SQLite storage for the Fairway Live app (stdlib sqlite3, no ORM)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3

DB_PATH = Path(
    os.environ.get(
        "GOLF_DB", Path(__file__).resolve().parent.parent / "data" / "golf.db"
    )
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holes (
    number INTEGER PRIMARY KEY,
    par INTEGER NOT NULL,
    stroke_index INTEGER NOT NULL,
    length_m INTEGER
);
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pin TEXT NOT NULL UNIQUE,
    start_hole INTEGER NOT NULL DEFAULT 1,
    team_hcp_override INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_id INTEGER REFERENCES flights(id) ON DELETE SET NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    handicap REAL,
    rookie INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scores (
    flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    hole INTEGER NOT NULL,
    strokes INTEGER,
    drive_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (flight_id, hole)
);
CREATE TABLE IF NOT EXISTS prizes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- longest_drive | nearest_pin | custom
    hole INTEGER,
    title_fr TEXT NOT NULL,
    title_en TEXT NOT NULL,
    description_fr TEXT NOT NULL DEFAULT '',
    description_en TEXT NOT NULL DEFAULT '',
    claimable INTEGER NOT NULL DEFAULT 1,
    holder_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    holder_name TEXT,
    holder_flight_id INTEGER REFERENCES flights(id) ON DELETE SET NULL,
    updated_at TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    author_type TEXT NOT NULL,       -- user | system
    author_name TEXT NOT NULL,
    flight_id INTEGER REFERENCES flights(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'post', -- post | reply | birdie | eagle | albatross | hole_in_one | prize | final | info
    text TEXT NOT NULL DEFAULT '',
    hole INTEGER,
    image TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    client_id TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    client_id TEXT NOT NULL,
    PRIMARY KEY (post_id, kind, client_id)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- marshal | water
    flight_id INTEGER REFERENCES flights(id) ON DELETE SET NULL,
    hole INTEGER,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', -- open | done
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS rookie_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    station TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (player_id, station)
);
"""

DEFAULT_EVENT = {
    "title": "Fairway Live 2026",
    "org_name": "Fairway Live",
    "org_logo": "",
    "date": "2026-09-24",
    "course_name": "Golf Château de la Tournette, English course",
    "status": "open",  # open | closed
    "live": True,
    "team_hcp_share": {"1": 1.0, "2": 0.25, "3": 0.1667, "4": 0.125, "5": 0.1},
    "min_drives_per_player": 3,
    "photographer_notice": True,
    "program": [
        {
            "time": "10:00",
            "fr": "Accueil, échauffement possible",
            "en": "Welcome, warm-up available",
        },
        {"time": "11:00", "fr": "Briefing des équipes", "en": "Team briefing"},
        {"time": "11:30", "fr": "Lunch sandwich", "en": "Sandwich lunch"},
        {"time": "12:30", "fr": "Départ en shotgun", "en": "Shotgun start"},
        {"time": "13:00", "fr": "Initiation rookies", "en": "Rookies clinic"},
        {"time": "17:30", "fr": "Retour au club-house", "en": "Back at the clubhouse"},
        {"time": "18:30", "fr": "Remise des prix", "en": "Prize giving"},
        {"time": "19:00", "fr": "Walking dinner", "en": "Walking dinner"},
    ],
    "how_it_works_fr": (
        "Formule scramble : chaque flight joue une seule balle. Sur chaque trou, vous choisissez le "
        "meilleur coup et tout le monde rejoue de là. Notez le nombre de coups du flight et le joueur "
        "dont le drive a été retenu (chacun doit en fournir au moins 3).\n\n"
        "Le classement se fait en points Stableford nets : le handicap d'équipe (moyenne des "
        "handicaps divisée par deux) donne des coups rendus selon l'index des trous. Par net = 2 points, "
        "birdie net = 3, eagle net = 4, bogey net = 1.\n\n"
        "En cas d'égalité, on compare les 9, 6, 3 puis le dernier trou joués."
    ),
    "how_it_works_en": (
        "Scramble format: each flight plays a single ball. On every hole you pick the best shot and "
        "everyone plays from there. Enter the flight's strokes and the player whose drive was used "
        "(everyone must contribute at least 3).\n\n"
        "The ranking uses net Stableford points: the team handicap (average handicap divided by two) "
        "gives strokes by hole index. Net par = 2 points, net birdie = 3, net eagle = 4, net bogey = 1.\n\n"
        "Ties are broken on the last 9, 6, 3 and final hole played."
    ),
    "rookies_intro_fr": "Les rookies découvrent le golf avec un pro pendant que les flights jouent. "
    "Trois ateliers : putting, chipping et practice. Les points de chaque atelier font le classement.",
    "rookies_intro_en": "Rookies discover golf with a pro while the flights are out. "
    "Three stations: putting, chipping and driving range. Station points make the ranking.",
    "rookie_stations": ["Putting", "Chipping", "Practice"],
}

# Par values 1-8 and 10-17 are read from the reference scorecard; holes 9 and
# 18 and every stroke index are placeholders to be corrected in the admin page.
DEFAULT_HOLES = [
    (1, 5, 15, 470),
    (2, 4, 3, 360),
    (3, 4, 7, 350),
    (4, 3, 13, 150),
    (5, 5, 17, 460),
    (6, 3, 11, 165),
    (7, 4, 1, 400),
    (8, 4, 5, 375),
    (9, 4, 9, 340),
    (10, 4, 2, 390),
    (11, 4, 16, 320),
    (12, 3, 14, 160),
    (13, 5, 4, 480),
    (14, 5, 10, 450),
    (15, 3, 12, 170),
    (16, 4, 6, 380),
    (17, 4, 18, 300),
    (18, 4, 8, 365),
]

DEFAULT_PRIZES = [
    (
        "longest_drive",
        8,
        "Longest drive",
        "Longest drive",
        "Le drive le plus long dans le fairway. Écrivez votre nom sur la pancarte et déclarez-le ici.",
        "Longest drive in the fairway. Write your name on the sign and declare it here.",
    ),
    (
        "nearest_pin",
        4,
        "Nearest to the pin",
        "Nearest to the pin",
        "La balle la plus proche du drapeau au premier coup.",
        "Closest ball to the flag from the tee.",
    ),
    (
        "nearest_pin",
        15,
        "Nearest to the pin",
        "Nearest to the pin",
        "La balle la plus proche du drapeau au premier coup.",
        "Closest ball to the flag from the tee.",
    ),
    (
        "custom",
        None,
        "Classement net",
        "Net ranking",
        "Les trois premiers flights au classement net.",
        "Top three flights in the net ranking.",
    ),
    (
        "custom",
        None,
        "Classement brut",
        "Gross ranking",
        "Le meilleur flight au classement brut.",
        "Best flight in the gross ranking.",
    ),
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(seed_demo: bool | None = None) -> None:
    """Create the schema, default settings and (optionally) demo data."""
    with tx() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT 1 FROM settings WHERE key='event'").fetchone() is None:
            conn.execute(
                "INSERT INTO settings VALUES ('event', ?)", (json.dumps(DEFAULT_EVENT),)
            )
        if conn.execute("SELECT COUNT(*) FROM holes").fetchone()[0] == 0:
            conn.executemany("INSERT INTO holes VALUES (?,?,?,?)", DEFAULT_HOLES)
        if conn.execute("SELECT COUNT(*) FROM prizes").fetchone()[0] == 0:
            for i, p in enumerate(DEFAULT_PRIZES):
                conn.execute(
                    "INSERT INTO prizes (kind, hole, title_fr, title_en, description_fr, description_en, claimable, sort_order)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (*p, 1 if p[1] else 0, i),
                )
        if seed_demo is None:
            seed_demo = os.environ.get("GOLF_SEED_DEMO", "1") == "1"
        if (
            seed_demo
            and conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0
        ):
            _seed_demo(conn)


def _seed_demo(conn: sqlite3.Connection) -> None:
    """Fictional flights so a fresh install shows something."""
    demo = [
        (
            "Flight I",
            "1111",
            1,
            [
                ("Alice", "Martin", 26),
                ("Bruno", "Lambert", 18.5),
                ("Chloé", "Dupont", 27.5),
                ("David", "Peeters", 36),
            ],
        ),
        (
            "Flight II",
            "2222",
            3,
            [
                ("Emma", "Janssens", 12),
                ("Farid", "Maes", 22),
                ("Gilles", "Jacobs", 30),
                ("Hana", "Mertens", 36),
            ],
        ),
        (
            "Flight III",
            "3333",
            5,
            [
                ("Igor", "Willems", 15),
                ("Julie", "Claes", 20),
                ("Karim", "Goossens", 25),
            ],
        ),
        (
            "Flight IV",
            "4444",
            7,
            [
                ("Léa", "Wouters", 9),
                ("Marc", "De Smet", 28),
                ("Nora", "Vermeulen", 33),
                ("Omar", "Dubois", 36),
            ],
        ),
        (
            "Flight V",
            "5555",
            10,
            [
                ("Paul", "Lemaire", 17),
                ("Quentin", "Simon", 19),
                ("Rosa", "Michel", 24),
                ("Sam", "Leroy", 31),
            ],
        ),
        (
            "Flight VI",
            "6666",
            12,
            [
                ("Théo", "Lefebvre", 14),
                ("Uma", "Dumont", 21),
                ("Victor", "Bertrand", 29),
                ("Wendy", "Renard", 36),
            ],
        ),
    ]
    for order, (name, pin, start, players) in enumerate(demo):
        cur = conn.execute(
            "INSERT INTO flights (name, pin, start_hole, sort_order) VALUES (?,?,?,?)",
            (name, pin, start, order),
        )
        fid = cur.lastrowid
        for i, (fn, ln, hcp) in enumerate(players):
            conn.execute(
                "INSERT INTO players (flight_id, first_name, last_name, handicap, sort_order) VALUES (?,?,?,?,?)",
                (fid, fn, ln, hcp, i),
            )
    for fn, ln in [("Yasmine", "Adam"), ("Zoé", "Baert"), ("Nils", "Coppens")]:
        conn.execute(
            "INSERT INTO players (flight_id, first_name, last_name, handicap, rookie) VALUES (NULL,?,?,NULL,1)",
            (fn, ln),
        )
    conn.execute(
        "INSERT INTO posts (author_type, author_name, kind, text, created_at) VALUES ('system','Fairway Live','info',?,?)",
        ("welcome", now_iso()),
    )


# --- settings ---------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_event(conn: sqlite3.Connection) -> dict:
    ev = dict(DEFAULT_EVENT)
    ev.update(get_setting(conn, "event", {}))
    return ev


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
