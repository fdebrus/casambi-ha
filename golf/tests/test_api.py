import os
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
import pytest

os.environ["GOLF_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["GOLF_UPLOADS"] = tempfile.mkdtemp()
os.environ["GOLF_ADMIN_TOKEN"] = "secret"
os.environ["GOLF_SEED_DEMO"] = "1"

from app.main import app

ADMIN = {"X-Admin-Token": "secret"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_state_and_seed(client):
    st = client.get("/api/state").json()
    assert st["event"]["title"] == "Fairway Live 2026"
    assert len(st["holes"]) == 18
    assert len(st["flights"]) == 6
    assert st["flights"][0]["team_hcp"] == 14
    assert st["flights"][0]["players"][0]["label"] == "Al"


def test_pin_login(client):
    assert (
        client.post("/api/flight/login", json={"pin": "1111"}).json()["name"]
        == "Flight I"
    )
    assert client.post("/api/flight/login", json={"pin": "0000"}).status_code == 403


def test_score_entry_creates_birdie_post_and_updates_card(client):
    st = client.get("/api/state").json()
    hole7 = st["holes"][6]
    r = client.put(
        "/api/flight/score",
        json={"pin": "1111", "hole": 7, "strokes": hole7["par"] - 1},
    )
    assert r.status_code == 200
    feed = client.get("/api/feed").json()["posts"]
    birdies = [p for p in feed if p["kind"] == "birdie"]
    assert len(birdies) == 1 and birdies[0]["hole"] == 7
    st = client.get("/api/state").json()
    f = st["flights"][0]
    assert f["score"]["holes_played"] == 1
    assert f["score"]["holes"][6]["gross_points"] == 3
    assert f["score"]["status"] == "playing"
    # correcting the score to a par removes the automatic post
    client.put(
        "/api/flight/score", json={"pin": "1111", "hole": 7, "strokes": hole7["par"]}
    )
    assert not [
        p for p in client.get("/api/feed").json()["posts"] if p["kind"] == "birdie"
    ]


def test_drive_must_belong_to_flight(client):
    st = client.get("/api/state").json()
    other = st["flights"][1]["players"][0]["id"]
    assert (
        client.put(
            "/api/flight/score",
            json={"pin": "1111", "hole": 1, "strokes": 5, "drive_player_id": other},
        ).status_code
        == 400
    )
    mine = st["flights"][0]["players"][1]["id"]
    assert (
        client.put(
            "/api/flight/score",
            json={"pin": "1111", "hole": 1, "strokes": 5, "drive_player_id": mine},
        ).status_code
        == 200
    )
    f = client.get("/api/state").json()["flights"][0]
    assert f["players"][1]["drives"] == 1


def test_prize_claim_posts_to_feed(client):
    st = client.get("/api/state").json()
    prize = next(p for p in st["prizes"] if p["claimable"])
    pid = st["flights"][0]["players"][0]["id"]
    r = client.post(
        f"/api/prizes/{prize['id']}/claim", json={"pin": "1111", "player_id": pid}
    )
    assert r.status_code == 200
    st = client.get("/api/state").json()
    assert st["prizes"][0]["holder_name"] == "Alice Martin"
    post = client.get("/api/feed").json()["posts"][0]
    assert post["kind"] == "prize" and post["meta"]["holder"] == "Alice Martin"


def test_feed_post_reply_and_reaction(client):
    r = client.post(
        "/api/feed",
        data={"author_name": "Gaëlle", "text": "Quel temps !", "client_id": "c1"},
    )
    pid = r.json()["id"]
    client.post(
        "/api/feed",
        data={
            "author_name": "Bart",
            "text": "Oui",
            "client_id": "c2",
            "parent_id": str(pid),
        },
    )
    assert (
        client.post(
            "/api/feed", data={"author_name": "", "text": "x", "client_id": "c1"}
        ).status_code
        == 400
    )
    r = client.post(
        f"/api/feed/{pid}/react", json={"kind": "bravo", "client_id": "c2"}
    ).json()
    assert r["active"] and r["count"] == 1
    r = client.post(
        f"/api/feed/{pid}/react", json={"kind": "bravo", "client_id": "c2"}
    ).json()
    assert not r["active"] and r["count"] == 0
    post = next(
        p
        for p in client.get("/api/feed?client_id=c1").json()["posts"]
        if p["id"] == pid
    )
    assert (
        post["mine"]
        and len(post["replies"]) == 1
        and post["replies"][0]["author_name"] == "Bart"
    )
    assert client.delete(f"/api/feed/{pid}?client_id=c2").status_code == 403
    assert client.delete(f"/api/feed/{pid}?client_id=c1").status_code == 200


def test_requests_and_admin_queue(client):
    assert (
        client.post(
            "/api/requests", json={"kind": "water", "pin": "2222", "hole": 5}
        ).status_code
        == 200
    )
    assert client.post("/api/requests", json={"kind": "pizza"}).status_code == 400
    assert client.get("/api/admin/overview").status_code == 401
    ov = client.get("/api/admin/overview", headers=ADMIN).json()
    assert (
        ov["requests"][0]["flight_name"] == "Flight II"
        and ov["requests"][0]["status"] == "open"
    )
    assert ov["flights"][0]["pin"] == "1111"
    rid = ov["requests"][0]["id"]
    client.put(f"/api/admin/requests/{rid}", headers=ADMIN, json={"status": "done"})
    assert (
        client.get("/api/admin/overview", headers=ADMIN).json()["requests"][0]["status"]
        == "done"
    )


def test_closing_scoring_blocks_entry(client):
    client.put("/api/admin/event", headers=ADMIN, json={"status": "closed"})
    assert (
        client.put(
            "/api/flight/score", json={"pin": "1111", "hole": 2, "strokes": 4}
        ).status_code
        == 409
    )
    client.put("/api/admin/event", headers=ADMIN, json={"status": "open"})
    assert (
        client.put(
            "/api/flight/score", json={"pin": "1111", "hole": 2, "strokes": 4}
        ).status_code
        == 200
    )


def test_final_post_when_all_flights_finish(client):
    st = client.get("/api/state").json()
    for f in st["flights"]:
        for h in range(1, 19):
            client.put(
                f"/api/admin/scores/{f['id']}/{h}", headers=ADMIN, json={"strokes": 4}
            )
    st = client.get("/api/state").json()
    assert st["all_finished"]
    finals = [
        p for p in client.get("/api/feed").json()["posts"] if p["kind"] == "final"
    ]
    assert len(finals) == 1
    # removing one score withdraws the final post
    client.put(
        f"/api/admin/scores/{st['flights'][0]['id']}/18",
        headers=ADMIN,
        json={"strokes": None},
    )
    assert not [
        p for p in client.get("/api/feed").json()["posts"] if p["kind"] == "final"
    ]


def test_holes_validation_and_import_export(client):
    holes = client.get("/api/state").json()["holes"]
    holes[0]["stroke_index"] = holes[1]["stroke_index"]
    assert client.put("/api/admin/holes", headers=ADMIN, json=holes).status_code == 400
    exp = client.get("/api/admin/export", headers=ADMIN).json()
    assert (
        len(exp["flights"]) == 6
        and exp["flights"][0]["players"][0]["first_name"] == "Alice"
    )
    r = client.post(
        "/api/admin/import",
        headers=ADMIN,
        json={
            "flights": [
                {
                    "name": "Flight X",
                    "players": [
                        {"first_name": "Marc", "last_name": "VDB", "handicap": 20}
                    ],
                }
            ],
            "rookies": [{"first_name": "Zoé", "last_name": "B"}],
        },
    )
    assert r.status_code == 200
    st = client.get("/api/state").json()
    assert [f["name"] for f in st["flights"]] == ["Flight X"]
    assert st["rookies"][0]["first_name"] == "Zoé"
    ov = client.get("/api/admin/overview", headers=ADMIN).json()
    assert len(ov["flights"][0]["pin"]) == 4


def test_spa_and_admin_pages(client):
    assert "Fairway Live" in client.get("/").text
    assert "Admin" in client.get("/admin").text
    assert client.get("/api/nothing").status_code == 404
    assert client.get("/#feed").status_code == 200
