"""FastAPI application: public API, flight scoring, feed, admin."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import uuid

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .scoring import HOLES, compute_flight, rank_flights, score_label, team_handicap

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
UPLOADS = Path(os.environ.get("GOLF_UPLOADS", BASE.parent / "data" / "uploads"))
ADMIN_TOKEN = os.environ.get("GOLF_ADMIN_TOKEN") or "golf-admin"
MAX_UPLOAD = 8 * 1024 * 1024
REACTIONS = {"bravo", "fire", "laugh", "shot"}
SCORE_POST_KINDS = {"birdie", "eagle", "albatross", "hole_in_one"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Fairway Live", docs_url=None, redoc_url=None, lifespan=lifespan)


# --- helpers ------------------------------------------------------------------


def initials_for(players: list[dict]) -> dict[int, str]:
    """Short unique label per player for the 'Drive' row (Fr, St, Ol, Cl…)."""
    labels: dict[int, str] = {}
    for p in players:
        n = 2
        first = p["first_name"].strip() or p["last_name"].strip() or "?"
        label = first[:n].capitalize()
        while label in labels.values() and n < len(first):
            n += 1
            label = first[:n].capitalize()
        if label in labels.values():
            label = (first[:1] + p["last_name"][:1]).upper()
        labels[p["id"]] = label
    return labels


def short_name(p: dict) -> str:
    ln = p["last_name"].strip()
    if ln and " " not in ln and len(ln) > 3:
        ln = ln[0] + "."
    return f"{p['first_name']} {ln}".strip()


def load_course(conn: sqlite3.Connection) -> dict[int, dict]:
    return {
        h["number"]: h for h in db.rows(conn, "SELECT * FROM holes ORDER BY number")
    }


def flight_team_hcp(flight: dict, players: list[dict], event: dict) -> int:
    if flight.get("team_hcp_override") is not None:
        return int(flight["team_hcp_override"])
    share = {int(k): float(v) for k, v in event.get("team_hcp_share", {}).items()}
    return team_handicap(
        [p["handicap"] for p in players if p["handicap"] is not None], share or None
    )


def build_flights(
    conn: sqlite3.Connection, event: dict, include_pin: bool = False
) -> list[dict]:
    course = load_course(conn)
    flights = db.rows(conn, "SELECT * FROM flights ORDER BY sort_order, id")
    players = db.rows(
        conn,
        "SELECT * FROM players WHERE flight_id IS NOT NULL ORDER BY sort_order, id",
    )
    scores = db.rows(conn, "SELECT * FROM scores")
    by_flight_players: dict[int, list[dict]] = {}
    for p in players:
        by_flight_players.setdefault(p["flight_id"], []).append(p)
    by_flight_scores: dict[int, dict[int, dict]] = {}
    for s in scores:
        by_flight_scores.setdefault(s["flight_id"], {})[s["hole"]] = s
    out = []
    for f in flights:
        ps = by_flight_players.get(f["id"], [])
        labels = initials_for(ps)
        hcp = flight_team_hcp(f, ps, event)
        fs = compute_flight(
            f["id"], hcp, f["start_hole"], course, by_flight_scores.get(f["id"], {})
        )
        d = {
            "id": f["id"],
            "name": f["name"],
            "start_hole": f["start_hole"],
            "team_hcp": hcp,
            "team_hcp_override": f["team_hcp_override"],
            "players": [
                {
                    "id": p["id"],
                    "first_name": p["first_name"],
                    "last_name": p["last_name"],
                    "handicap": p["handicap"],
                    "short": short_name(p),
                    "label": labels[p["id"]],
                    "drives": fs.drives.get(p["id"], 0),
                }
                for p in ps
            ],
            "score": {
                "net_total": fs.net_total,
                "gross_total": fs.gross_total,
                "holes_played": fs.holes_played,
                "net_pace": fs.net_pace,
                "gross_pace": fs.gross_pace,
                "status": fs.status,
                "current_hole": fs.current_hole,
                "last_hole": fs.last_hole,
                "last_update": fs.last_update,
                "holes": [h.__dict__ for h in fs.holes],
            },
            "_fs": fs,
        }
        if include_pin:
            d["pin"] = f["pin"]
        out.append(d)
    return out


def with_rankings(flights: list[dict]) -> None:
    entries = [(f["_fs"], f["start_hole"]) for f in flights]
    for net in (True, False):
        for r in rank_flights(entries, net=net):
            f = next(x for x in flights if x["id"] == r["flight_id"])
            f["score"]["rank_net" if net else "rank_gross"] = r["rank"]
            f["score"]["tie_net" if net else "tie_gross"] = r["tie_broken"]
    for f in flights:
        f.pop("_fs", None)


def build_state(conn: sqlite3.Connection, include_pin: bool = False) -> dict:
    event = db.get_event(conn)
    flights = build_flights(conn, event, include_pin)
    with_rankings(flights)
    prizes = db.rows(conn, "SELECT * FROM prizes ORDER BY sort_order, id")
    rookies = db.rows(
        conn,
        "SELECT * FROM players WHERE rookie=1 ORDER BY sort_order, last_name, first_name",
    )
    rookie_scores = db.rows(conn, "SELECT * FROM rookie_scores")
    for r in rookies:
        r["stations"] = {
            s["station"]: s["points"]
            for s in rookie_scores
            if s["player_id"] == r["id"]
        }
        r["total"] = sum(r["stations"].values())
        r["short"] = short_name(r)
    rookies.sort(key=lambda r: (-r["total"], r["last_name"]))
    feed_count = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hidden=0 AND parent_id IS NULL"
    ).fetchone()[0]
    last_post = (
        conn.execute("SELECT MAX(id) FROM posts WHERE hidden=0").fetchone()[0] or 0
    )
    all_finished = bool(flights) and all(
        f["score"]["status"] == "finished" for f in flights
    )
    return {
        "event": event,
        "holes": db.rows(conn, "SELECT * FROM holes ORDER BY number"),
        "flights": flights,
        "prizes": prizes,
        "rookies": rookies,
        "feed_count": feed_count,
        "last_post_id": last_post,
        "all_finished": all_finished,
        "server_time": db.now_iso(),
    }


def flight_by_pin(conn: sqlite3.Connection, pin: str | None) -> dict:
    pin = (pin or "").strip()
    if not pin:
        raise HTTPException(401, "pin_required")
    row = conn.execute("SELECT * FROM flights WHERE pin=?", (pin,)).fetchone()
    if row is None:
        raise HTTPException(403, "bad_pin")
    return dict(row)


def require_open(event: dict) -> None:
    if event.get("status") == "closed":
        raise HTTPException(409, "scoring_closed")


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "admin_token_required")


# --- feed automation ------------------------------------------------------------


def sync_score_post(
    conn: sqlite3.Connection, flight: dict, hole: int, label: str | None
) -> None:
    """Create or remove the automatic birdie/eagle post for one hole."""
    existing = conn.execute(
        "SELECT id, kind FROM posts WHERE author_type='system' AND flight_id=? AND hole=? AND kind IN ('birdie','eagle','albatross','hole_in_one')",
        (flight["id"], hole),
    ).fetchone()
    wanted = label if label in SCORE_POST_KINDS else None
    if existing and existing["kind"] == wanted:
        return
    if existing:
        conn.execute("DELETE FROM posts WHERE id=?", (existing["id"],))
    if wanted:
        conn.execute(
            "INSERT INTO posts (author_type, author_name, flight_id, kind, hole, meta, created_at)"
            " VALUES ('system','Fairway Live',?,?,?,?,?)",
            (
                flight["id"],
                wanted,
                hole,
                json.dumps({"flight_name": flight["name"]}),
                db.now_iso(),
            ),
        )


def sync_final_post(conn: sqlite3.Connection) -> None:
    """Post the final result once every flight has finished 18 holes."""
    state = build_state(conn)
    existing = conn.execute("SELECT id FROM posts WHERE kind='final'").fetchone()
    if not state["all_finished"]:
        if existing:
            conn.execute("DELETE FROM posts WHERE id=?", (existing["id"],))
        return
    if existing:
        return
    winner = min(state["flights"], key=lambda f: f["score"]["rank_net"])
    conn.execute(
        "INSERT INTO posts (author_type, author_name, flight_id, kind, meta, created_at)"
        " VALUES ('system','Fairway Live',?,?,?,?)",
        (
            winner["id"],
            "final",
            json.dumps(
                {"flight_name": winner["name"], "points": winner["score"]["net_total"]}
            ),
            db.now_iso(),
        ),
    )


# --- public API ---------------------------------------------------------------


@app.get("/api/state")
def api_state():
    with db.tx() as conn:
        return build_state(conn)


class PinBody(BaseModel):
    pin: str


@app.post("/api/flight/login")
def api_flight_login(body: PinBody):
    with db.tx() as conn:
        f = flight_by_pin(conn, body.pin)
        return {"flight_id": f["id"], "name": f["name"]}


class ScoreBody(BaseModel):
    pin: str
    hole: int = Field(ge=1, le=HOLES)
    strokes: int | None = Field(default=None, ge=1, le=20)
    drive_player_id: int | None = None


@app.put("/api/flight/score")
def api_flight_score(body: ScoreBody):
    with db.tx() as conn:
        event = db.get_event(conn)
        require_open(event)
        f = flight_by_pin(conn, body.pin)
        if body.drive_player_id is not None:
            ok = conn.execute(
                "SELECT 1 FROM players WHERE id=? AND flight_id=?",
                (body.drive_player_id, f["id"]),
            ).fetchone()
            if not ok:
                raise HTTPException(400, "player_not_in_flight")
        if body.strokes is None and body.drive_player_id is None:
            conn.execute(
                "DELETE FROM scores WHERE flight_id=? AND hole=?", (f["id"], body.hole)
            )
        else:
            conn.execute(
                "INSERT INTO scores (flight_id, hole, strokes, drive_player_id, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(flight_id, hole) DO UPDATE SET strokes=excluded.strokes,"
                " drive_player_id=excluded.drive_player_id, updated_at=excluded.updated_at",
                (f["id"], body.hole, body.strokes, body.drive_player_id, db.now_iso()),
            )
        par = conn.execute(
            "SELECT par FROM holes WHERE number=?", (body.hole,)
        ).fetchone()
        sync_score_post(
            conn, f, body.hole, score_label(par["par"] if par else 4, body.strokes)
        )
        sync_final_post(conn)
        return {"ok": True}


class ClaimBody(BaseModel):
    pin: str
    player_id: int | None = None
    holder_name: str | None = None


@app.post("/api/prizes/{prize_id}/claim")
def api_prize_claim(prize_id: int, body: ClaimBody):
    with db.tx() as conn:
        event = db.get_event(conn)
        require_open(event)
        f = flight_by_pin(conn, body.pin)
        prize = conn.execute("SELECT * FROM prizes WHERE id=?", (prize_id,)).fetchone()
        if prize is None or not prize["claimable"]:
            raise HTTPException(404, "prize_not_claimable")
        name = (body.holder_name or "").strip()
        if body.player_id is not None:
            p = conn.execute(
                "SELECT * FROM players WHERE id=? AND flight_id=?",
                (body.player_id, f["id"]),
            ).fetchone()
            if p is None:
                raise HTTPException(400, "player_not_in_flight")
            name = f"{p['first_name']} {p['last_name']}".strip()
        if not name:
            raise HTTPException(400, "holder_required")
        conn.execute(
            "UPDATE prizes SET holder_player_id=?, holder_name=?, holder_flight_id=?, updated_at=? WHERE id=?",
            (body.player_id, name, f["id"], db.now_iso(), prize_id),
        )
        conn.execute(
            "INSERT INTO posts (author_type, author_name, flight_id, kind, hole, meta, created_at)"
            " VALUES ('system','Fairway Live',?,?,?,?,?)",
            (
                f["id"],
                "prize",
                prize["hole"],
                json.dumps(
                    {
                        "prize_id": prize_id,
                        "prize_kind": prize["kind"],
                        "title_fr": prize["title_fr"],
                        "title_en": prize["title_en"],
                        "holder": name,
                        "flight_name": f["name"],
                    }
                ),
                db.now_iso(),
            ),
        )
        return {"ok": True}


def serialize_posts(
    conn: sqlite3.Connection, client_id: str | None, include_hidden: bool = False
) -> list[dict]:
    posts = db.rows(
        conn,
        "SELECT p.*, f.name AS flight_name FROM posts p LEFT JOIN flights f ON f.id=p.flight_id"
        " WHERE (p.hidden=0 OR ?) ORDER BY p.id DESC",
        (1 if include_hidden else 0,),
    )
    counts: dict[int, dict[str, int]] = {}
    for r in db.rows(
        conn,
        "SELECT post_id, kind, COUNT(*) AS n FROM reactions GROUP BY post_id, kind",
    ):
        counts.setdefault(r["post_id"], {})[r["kind"]] = r["n"]
    mine: set[tuple[int, str]] = set()
    if client_id:
        mine = {
            (r["post_id"], r["kind"])
            for r in db.rows(
                conn,
                "SELECT post_id, kind FROM reactions WHERE client_id=?",
                (client_id,),
            )
        }
    players_by_flight: dict[int, list[str]] = {}
    for p in db.rows(
        conn,
        "SELECT * FROM players WHERE flight_id IS NOT NULL ORDER BY sort_order, id",
    ):
        players_by_flight.setdefault(p["flight_id"], []).append(short_name(p))
    by_id: dict[int, dict] = {}
    roots: list[dict] = []
    for p in posts:
        p["meta"] = json.loads(p.get("meta") or "{}")
        p["reactions"] = {
            k: counts.get(p["id"], {}).get(k, 0) for k in sorted(REACTIONS)
        }
        p["my_reactions"] = [k for k in REACTIONS if (p["id"], k) in mine]
        p["flight_players"] = (
            players_by_flight.get(p["flight_id"], []) if p["flight_id"] else []
        )
        p["replies"] = []
        p["mine"] = bool(client_id) and p.get("client_id") == client_id
        p.pop("client_id", None)
        by_id[p["id"]] = p
    for p in posts:
        if p["parent_id"] and p["parent_id"] in by_id:
            by_id[p["parent_id"]]["replies"].insert(0, p)
        elif not p["parent_id"]:
            roots.append(p)
    return roots


@app.get("/api/feed")
def api_feed(client_id: str | None = None):
    with db.tx() as conn:
        return {"posts": serialize_posts(conn, client_id)}


@app.post("/api/feed")
async def api_feed_post(
    author_name: str = Form(""),
    text: str = Form(""),
    parent_id: int | None = Form(None),
    client_id: str = Form(""),
    pin: str = Form(""),
    image: UploadFile | None = File(None),
):
    author = author_name.strip()[:60]
    text = text.strip()[:2000]
    if not author:
        raise HTTPException(400, "name_required")
    if not text and image is None:
        raise HTTPException(400, "empty_post")
    image_name = None
    if image is not None and image.filename:
        data = await image.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "image_too_large")
        ext = Path(image.filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}:
            raise HTTPException(400, "image_type")
        image_name = f"{uuid.uuid4().hex}{ext}"
        (UPLOADS / image_name).write_bytes(data)
    with db.tx() as conn:
        flight_id = None
        if pin.strip():
            row = conn.execute(
                "SELECT id FROM flights WHERE pin=?", (pin.strip(),)
            ).fetchone()
            flight_id = row["id"] if row else None
        if parent_id is not None:
            parent = conn.execute(
                "SELECT id, parent_id FROM posts WHERE id=? AND hidden=0", (parent_id,)
            ).fetchone()
            if parent is None:
                raise HTTPException(404, "parent_not_found")
            if parent["parent_id"]:
                parent_id = parent["parent_id"]
        cur = conn.execute(
            "INSERT INTO posts (parent_id, author_type, author_name, flight_id, kind, text, image, client_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                parent_id,
                "user",
                author,
                flight_id,
                "reply" if parent_id else "post",
                text,
                image_name,
                client_id[:64],
                db.now_iso(),
            ),
        )
        return {"ok": True, "id": cur.lastrowid}


class ReactBody(BaseModel):
    kind: str
    client_id: str


@app.post("/api/feed/{post_id}/react")
def api_react(post_id: int, body: ReactBody):
    if body.kind not in REACTIONS or not body.client_id:
        raise HTTPException(400, "bad_reaction")
    with db.tx() as conn:
        if (
            conn.execute(
                "SELECT 1 FROM posts WHERE id=? AND hidden=0", (post_id,)
            ).fetchone()
            is None
        ):
            raise HTTPException(404, "post_not_found")
        existing = conn.execute(
            "SELECT 1 FROM reactions WHERE post_id=? AND kind=? AND client_id=?",
            (post_id, body.kind, body.client_id[:64]),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM reactions WHERE post_id=? AND kind=? AND client_id=?",
                (post_id, body.kind, body.client_id[:64]),
            )
        else:
            conn.execute(
                "INSERT INTO reactions VALUES (?,?,?)",
                (post_id, body.kind, body.client_id[:64]),
            )
        n = conn.execute(
            "SELECT COUNT(*) FROM reactions WHERE post_id=? AND kind=?",
            (post_id, body.kind),
        ).fetchone()[0]
        return {"ok": True, "active": not existing, "count": n}


@app.delete("/api/feed/{post_id}")
def api_delete_own_post(post_id: int, client_id: str):
    with db.tx() as conn:
        row = conn.execute(
            "SELECT client_id FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        if row is None or not client_id or row["client_id"] != client_id:
            raise HTTPException(403, "not_owner")
        conn.execute(
            "UPDATE posts SET hidden=1 WHERE id=? OR parent_id=?", (post_id, post_id)
        )
        return {"ok": True}


class RequestBody(BaseModel):
    kind: str
    pin: str | None = None
    hole: int | None = Field(default=None, ge=1, le=HOLES)
    note: str = ""


@app.post("/api/requests")
def api_request(body: RequestBody):
    if body.kind not in {"marshal", "water"}:
        raise HTTPException(400, "bad_kind")
    with db.tx() as conn:
        flight_id = None
        if body.pin:
            row = conn.execute(
                "SELECT id FROM flights WHERE pin=?", (body.pin.strip(),)
            ).fetchone()
            flight_id = row["id"] if row else None
        cur = conn.execute(
            "INSERT INTO requests (kind, flight_id, hole, note, created_at) VALUES (?,?,?,?,?)",
            (body.kind, flight_id, body.hole, body.note.strip()[:300], db.now_iso()),
        )
        return {"ok": True, "id": cur.lastrowid}


# --- admin API ------------------------------------------------------------------

admin = Depends(require_admin)


@app.get("/api/admin/overview", dependencies=[admin])
def admin_overview():
    with db.tx() as conn:
        state = build_state(conn, include_pin=True)
        state["requests"] = db.rows(
            conn,
            "SELECT r.*, f.name AS flight_name FROM requests r LEFT JOIN flights f ON f.id=r.flight_id ORDER BY r.status DESC, r.id DESC",
        )
        state["unassigned_players"] = db.rows(
            conn, "SELECT * FROM players WHERE flight_id IS NULL ORDER BY last_name"
        )
        state["posts"] = serialize_posts(conn, None, include_hidden=True)
        state["admin_token_is_default"] = ADMIN_TOKEN == "golf-admin"
        return state


@app.put("/api/admin/event", dependencies=[admin])
def admin_event(body: dict):
    with db.tx() as conn:
        ev = db.get_event(conn)
        ev.update({k: v for k, v in body.items() if k in db.DEFAULT_EVENT})
        db.set_setting(conn, "event", ev)
        sync_final_post(conn)
        return ev


class HoleBody(BaseModel):
    number: int = Field(ge=1, le=HOLES)
    par: int = Field(ge=3, le=6)
    stroke_index: int = Field(ge=1, le=HOLES)
    length_m: int | None = None


@app.put("/api/admin/holes", dependencies=[admin])
def admin_holes(body: list[HoleBody]):
    if sorted(h.stroke_index for h in body) != list(range(1, len(body) + 1)):
        raise HTTPException(400, "stroke_index_must_be_unique_1_to_18")
    with db.tx() as conn:
        for h in body:
            conn.execute(
                "INSERT INTO holes VALUES (?,?,?,?) ON CONFLICT(number) DO UPDATE SET par=excluded.par,"
                " stroke_index=excluded.stroke_index, length_m=excluded.length_m",
                (h.number, h.par, h.stroke_index, h.length_m),
            )
        return {"ok": True}


class FlightBody(BaseModel):
    name: str
    pin: str | None = None
    start_hole: int = Field(default=1, ge=1, le=HOLES)
    team_hcp_override: int | None = None
    sort_order: int = 0


def _gen_pin(conn: sqlite3.Connection) -> str:
    while True:
        pin = f"{secrets.randbelow(10000):04d}"
        if conn.execute("SELECT 1 FROM flights WHERE pin=?", (pin,)).fetchone() is None:
            return pin


@app.post("/api/admin/flights", dependencies=[admin])
def admin_flight_create(body: FlightBody):
    with db.tx() as conn:
        pin = (body.pin or "").strip() or _gen_pin(conn)
        try:
            cur = conn.execute(
                "INSERT INTO flights (name, pin, start_hole, team_hcp_override, sort_order) VALUES (?,?,?,?,?)",
                (
                    body.name.strip(),
                    pin,
                    body.start_hole,
                    body.team_hcp_override,
                    body.sort_order,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "pin_in_use") from exc
        return {"ok": True, "id": cur.lastrowid, "pin": pin}


@app.put("/api/admin/flights/{flight_id}", dependencies=[admin])
def admin_flight_update(flight_id: int, body: FlightBody):
    with db.tx() as conn:
        try:
            conn.execute(
                "UPDATE flights SET name=?, pin=COALESCE(NULLIF(?, ''), pin), start_hole=?, team_hcp_override=?, sort_order=? WHERE id=?",
                (
                    body.name.strip(),
                    (body.pin or "").strip(),
                    body.start_hole,
                    body.team_hcp_override,
                    body.sort_order,
                    flight_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "pin_in_use") from exc
        sync_final_post(conn)
        return {"ok": True}


@app.delete("/api/admin/flights/{flight_id}", dependencies=[admin])
def admin_flight_delete(flight_id: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM flights WHERE id=?", (flight_id,))
        sync_final_post(conn)
        return {"ok": True}


class PlayerBody(BaseModel):
    flight_id: int | None = None
    first_name: str
    last_name: str = ""
    handicap: float | None = None
    rookie: bool = False
    sort_order: int = 0


@app.post("/api/admin/players", dependencies=[admin])
def admin_player_create(body: PlayerBody):
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO players (flight_id, first_name, last_name, handicap, rookie, sort_order) VALUES (?,?,?,?,?,?)",
            (
                body.flight_id,
                body.first_name.strip(),
                body.last_name.strip(),
                body.handicap,
                int(body.rookie),
                body.sort_order,
            ),
        )
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/admin/players/{player_id}", dependencies=[admin])
def admin_player_update(player_id: int, body: PlayerBody):
    with db.tx() as conn:
        conn.execute(
            "UPDATE players SET flight_id=?, first_name=?, last_name=?, handicap=?, rookie=?, sort_order=? WHERE id=?",
            (
                body.flight_id,
                body.first_name.strip(),
                body.last_name.strip(),
                body.handicap,
                int(body.rookie),
                body.sort_order,
                player_id,
            ),
        )
        return {"ok": True}


@app.delete("/api/admin/players/{player_id}", dependencies=[admin])
def admin_player_delete(player_id: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM players WHERE id=?", (player_id,))
        return {"ok": True}


class AdminScoreBody(BaseModel):
    strokes: int | None = Field(default=None, ge=1, le=20)
    drive_player_id: int | None = None


@app.put("/api/admin/scores/{flight_id}/{hole}", dependencies=[admin])
def admin_score(flight_id: int, hole: int, body: AdminScoreBody):
    with db.tx() as conn:
        f = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
        if f is None:
            raise HTTPException(404, "flight_not_found")
        if body.strokes is None:
            conn.execute(
                "DELETE FROM scores WHERE flight_id=? AND hole=?", (flight_id, hole)
            )
        else:
            conn.execute(
                "INSERT INTO scores (flight_id, hole, strokes, drive_player_id, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(flight_id, hole) DO UPDATE SET strokes=excluded.strokes, drive_player_id=excluded.drive_player_id, updated_at=excluded.updated_at",
                (flight_id, hole, body.strokes, body.drive_player_id, db.now_iso()),
            )
        par = conn.execute("SELECT par FROM holes WHERE number=?", (hole,)).fetchone()
        sync_score_post(
            conn, dict(f), hole, score_label(par["par"] if par else 4, body.strokes)
        )
        sync_final_post(conn)
        return {"ok": True}


@app.post("/api/admin/reset", dependencies=[admin])
def admin_reset(body: dict):
    """Wipe scores / feed / requests (each opt-in) before the real day."""
    with db.tx() as conn:
        if body.get("scores"):
            conn.execute("DELETE FROM scores")
            conn.execute(
                "DELETE FROM posts WHERE author_type='system' AND kind!='info'"
            )
            conn.execute(
                "UPDATE prizes SET holder_player_id=NULL, holder_name=NULL, holder_flight_id=NULL, updated_at=NULL"
            )
        if body.get("feed"):
            conn.execute("DELETE FROM posts WHERE author_type='user'")
        if body.get("requests"):
            conn.execute("DELETE FROM requests")
        if body.get("flights"):
            conn.execute("DELETE FROM players")
            conn.execute("DELETE FROM flights")
            conn.execute(
                "DELETE FROM posts WHERE author_type='system' AND kind!='info'"
            )
        return {"ok": True}


class PrizeBody(BaseModel):
    kind: str = "custom"
    hole: int | None = None
    title_fr: str
    title_en: str
    description_fr: str = ""
    description_en: str = ""
    claimable: bool = True
    sort_order: int = 0


@app.post("/api/admin/prizes", dependencies=[admin])
def admin_prize_create(body: PrizeBody):
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO prizes (kind, hole, title_fr, title_en, description_fr, description_en, claimable, sort_order) VALUES (?,?,?,?,?,?,?,?)",
            (
                body.kind,
                body.hole,
                body.title_fr,
                body.title_en,
                body.description_fr,
                body.description_en,
                int(body.claimable),
                body.sort_order,
            ),
        )
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/admin/prizes/{prize_id}", dependencies=[admin])
def admin_prize_update(prize_id: int, body: PrizeBody):
    with db.tx() as conn:
        conn.execute(
            "UPDATE prizes SET kind=?, hole=?, title_fr=?, title_en=?, description_fr=?, description_en=?, claimable=?, sort_order=? WHERE id=?",
            (
                body.kind,
                body.hole,
                body.title_fr,
                body.title_en,
                body.description_fr,
                body.description_en,
                int(body.claimable),
                body.sort_order,
                prize_id,
            ),
        )
        return {"ok": True}


class HolderBody(BaseModel):
    holder_name: str | None = None
    holder_flight_id: int | None = None


@app.put("/api/admin/prizes/{prize_id}/holder", dependencies=[admin])
def admin_prize_holder(prize_id: int, body: HolderBody):
    with db.tx() as conn:
        conn.execute(
            "UPDATE prizes SET holder_player_id=NULL, holder_name=?, holder_flight_id=?, updated_at=? WHERE id=?",
            (body.holder_name, body.holder_flight_id, db.now_iso(), prize_id),
        )
        return {"ok": True}


@app.delete("/api/admin/prizes/{prize_id}", dependencies=[admin])
def admin_prize_delete(prize_id: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM prizes WHERE id=?", (prize_id,))
        return {"ok": True}


@app.put("/api/admin/requests/{request_id}", dependencies=[admin])
def admin_request(request_id: int, body: dict):
    with db.tx() as conn:
        status = "done" if body.get("status") == "done" else "open"
        conn.execute(
            "UPDATE requests SET status=?, done_at=? WHERE id=?",
            (status, db.now_iso() if status == "done" else None, request_id),
        )
        return {"ok": True}


@app.put("/api/admin/posts/{post_id}", dependencies=[admin])
def admin_post(post_id: int, body: dict):
    with db.tx() as conn:
        conn.execute(
            "UPDATE posts SET hidden=? WHERE id=? OR parent_id=?",
            (int(bool(body.get("hidden"))), post_id, post_id),
        )
        return {"ok": True}


class AdminInfoPost(BaseModel):
    text: str


@app.post("/api/admin/posts", dependencies=[admin])
def admin_post_create(body: AdminInfoPost):
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO posts (author_type, author_name, kind, text, created_at) VALUES ('system','Fairway Live','info',?,?)",
            (body.text.strip()[:2000], db.now_iso()),
        )
        return {"ok": True, "id": cur.lastrowid}


class RookieScoreBody(BaseModel):
    player_id: int
    station: str
    points: int = Field(ge=0, le=1000)


@app.put("/api/admin/rookie_scores", dependencies=[admin])
def admin_rookie_score(body: RookieScoreBody):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO rookie_scores (player_id, station, points, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(player_id, station) DO UPDATE SET points=excluded.points, updated_at=excluded.updated_at",
            (body.player_id, body.station, body.points, db.now_iso()),
        )
        return {"ok": True}


@app.get("/api/admin/export", dependencies=[admin])
def admin_export():
    with db.tx() as conn:
        flights = db.rows(conn, "SELECT * FROM flights ORDER BY sort_order, id")
        for f in flights:
            f["players"] = db.rows(
                conn,
                "SELECT first_name, last_name, handicap FROM players WHERE flight_id=? ORDER BY sort_order, id",
                (f["id"],),
            )
            f.pop("id", None)
        rookies = db.rows(
            conn,
            "SELECT first_name, last_name FROM players WHERE rookie=1 ORDER BY last_name",
        )
        return {"flights": flights, "rookies": rookies}


@app.post("/api/admin/import", dependencies=[admin])
def admin_import(body: dict):
    """Replace flights, players and rookies from a JSON document.

    Format: {"flights": [{"name","pin"?,"start_hole"?,"team_hcp_override"?,
    "players":[{"first_name","last_name","handicap"}]}], "rookies":[{"first_name","last_name"}]}
    """
    flights = body.get("flights") or []
    with db.tx() as conn:
        conn.execute("DELETE FROM players")
        conn.execute("DELETE FROM flights")
        conn.execute("DELETE FROM posts WHERE author_type='system' AND kind!='info'")
        for order, f in enumerate(flights):
            pin = str(f.get("pin") or "").strip() or _gen_pin(conn)
            cur = conn.execute(
                "INSERT INTO flights (name, pin, start_hole, team_hcp_override, sort_order) VALUES (?,?,?,?,?)",
                (
                    str(f.get("name", f"Flight {order + 1}")).strip(),
                    pin,
                    int(f.get("start_hole") or 1),
                    f.get("team_hcp_override"),
                    order,
                ),
            )
            fid = cur.lastrowid
            for i, p in enumerate(f.get("players") or []):
                conn.execute(
                    "INSERT INTO players (flight_id, first_name, last_name, handicap, sort_order) VALUES (?,?,?,?,?)",
                    (
                        fid,
                        str(p.get("first_name", "")).strip(),
                        str(p.get("last_name", "")).strip(),
                        p.get("handicap"),
                        i,
                    ),
                )
        for p in body.get("rookies") or []:
            conn.execute(
                "INSERT INTO players (flight_id, first_name, last_name, rookie) VALUES (NULL,?,?,1)",
                (
                    str(p.get("first_name", "")).strip(),
                    str(p.get("last_name", "")).strip(),
                ),
            )
        return {"ok": True, "flights": len(flights)}


# --- static ---------------------------------------------------------------------

app.mount(
    "/uploads", StaticFiles(directory=str(UPLOADS), check_dir=False), name="uploads"
)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        STATIC / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/{path:path}")
def spa(path: str):
    """Everything else is the single-page app (deep links like /#carte)."""
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")
