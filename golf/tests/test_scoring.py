from app.scoring import (
    compute_flight,
    countback_key,
    play_order,
    rank_flights,
    score_label,
    stableford,
    strokes_on_hole,
    team_handicap,
)


def test_team_handicap_matches_reference_event():
    # 26 + 18.5 + 27.5 + 36 = 108 -> average 27 / 2 = 13.5 -> 14
    assert team_handicap([26, 18.5, 27.5, 36]) == 14


def test_team_handicap_three_players_and_empty():
    assert team_handicap([12, 18, 24]) == 9
    assert team_handicap([]) == 0


def test_strokes_allocation():
    assert strokes_on_hole(14, 1) == 1
    assert strokes_on_hole(14, 14) == 1
    assert strokes_on_hole(14, 15) == 0
    assert strokes_on_hole(20, 2) == 2
    assert strokes_on_hole(20, 3) == 1
    assert strokes_on_hole(0, 1) == 0


def test_stableford_values_from_screenshot():
    assert stableford(5, 5, 0) == 2  # hole 1: gross par, no stroke
    assert stableford(4, 4, 1) == 3  # hole 2: par with a stroke -> net birdie
    assert stableford(5, 4, 0) == 3  # hole 5: gross birdie
    assert stableford(4, 5, 1) == 2  # hole 10: bogey with a stroke
    assert stableford(4, 5, 0) == 1  # hole 11: bogey, no stroke
    assert stableford(5, 4, 1) == 4  # hole 13: birdie with a stroke
    assert stableford(4, 9, 0) == 0
    assert stableford(4, None) is None


def test_score_labels():
    assert score_label(4, 3) == "birdie"
    assert score_label(5, 3) == "eagle"
    assert score_label(3, 1) == "hole_in_one"
    assert score_label(4, 4) == "par"
    assert score_label(4, 6) == "double_bogey"


def test_play_order_shotgun():
    assert play_order(1)[:3] == [1, 2, 3]
    assert play_order(17) == [17, 18, *range(1, 17)]


def _course():
    return {n: {"par": 4, "stroke_index": n} for n in range(1, 19)}


def test_compute_flight_pace_and_status():
    scores = {
        n: {"strokes": 4, "updated_at": f"2026-09-24T12:{n:02d}:00+00:00"}
        for n in range(1, 10)
    }
    fs = compute_flight(1, 9, 1, _course(), scores)
    assert fs.holes_played == 9
    assert fs.net_total == 27  # 9 net birdies
    assert fs.gross_total == 18
    assert fs.net_pace == 9 and fs.gross_pace == 0
    assert fs.status == "playing" and fs.current_hole == 10
    assert fs.last_hole == 9
    full = compute_flight(1, 0, 5, _course(), {n: {"strokes": 4} for n in range(1, 19)})
    assert full.status == "finished" and full.holes_played == 18
    assert compute_flight(1, 0, 5, _course(), {}).status == "not_started"


def test_countback_breaks_ties():
    course = _course()
    a = {n: {"strokes": 4} for n in range(1, 19)}
    a[1]["strokes"] = 3  # birdie on the first hole played
    b = {n: {"strokes": 4} for n in range(1, 19)}
    b[18]["strokes"] = 3  # birdie on the last hole played
    fa = compute_flight(1, 0, 1, course, a)
    fb = compute_flight(2, 0, 1, course, b)
    assert fa.net_total == fb.net_total == 37
    assert countback_key(fb, 1) > countback_key(fa, 1)
    ranked = rank_flights([(fa, 1), (fb, 1)])
    assert [r["flight_id"] for r in ranked] == [2, 1]
    assert ranked[1]["rank"] == 2 and ranked[1]["tie_broken"] is True


def test_identical_cards_share_rank():
    course = _course()
    s = {n: {"strokes": 4} for n in range(1, 19)}
    fa = compute_flight(1, 0, 1, course, s)
    fb = compute_flight(2, 0, 1, course, s)
    ranked = rank_flights([(fa, 1), (fb, 1)])
    assert [r["rank"] for r in ranked] == [1, 1]
