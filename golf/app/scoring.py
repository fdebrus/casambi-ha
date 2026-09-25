"""Pure scoring logic: Stableford points, team handicap, pace, ranking.

The competition format is a team scramble: every flight (team) plays one
ball per hole, so there is one gross score per hole per flight.  Net
points are computed with the flight's team handicap allocated over the
holes by stroke index, exactly like an individual Stableford card.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import math

HOLES = 18

# Share of the summed player handicaps that becomes the team handicap,
# keyed by the number of players in the flight.  Defaults to "average
# handicap divided by two", which reproduces the reference event
# (26 + 18.5 + 27.5 + 36 = 108 -> 108 / 8 = 13.5 -> 14).
DEFAULT_TEAM_HCP_SHARE = {1: 1.0, 2: 0.25, 3: 1 / 6, 4: 0.125, 5: 0.1}


def round_half_up(value: float) -> int:
    """Golf rounding: .5 always rounds up."""
    return int(math.floor(value + 0.5))


def team_handicap(
    handicaps: Iterable[float], share: dict[int, float] | None = None
) -> int:
    """Team playing handicap from the players' handicaps."""
    hcps = [float(h) for h in handicaps if h is not None]
    if not hcps:
        return 0
    share = share or DEFAULT_TEAM_HCP_SHARE
    n = len(hcps)
    pct = share.get(n, share.get(max(share), 0.1))
    return max(0, round_half_up(sum(hcps) * pct))


def strokes_on_hole(team_hcp: int, stroke_index: int) -> int:
    """Return the handicap strokes received on a hole with this stroke index."""
    if team_hcp <= 0:
        return 0
    base, extra = divmod(team_hcp, HOLES)
    return base + (1 if stroke_index <= extra else 0)


def stableford(par: int, strokes: int | None, received: int = 0) -> int | None:
    """Stableford points for one hole (None if the hole was not played)."""
    if strokes is None:
        return None
    net = strokes - received
    return max(0, par - net + 2)


def score_label(par: int, strokes: int | None) -> str | None:
    """Return the gross result name for the feed (hole_in_one, eagle, birdie...)."""
    if strokes is None:
        return None
    if strokes == 1:
        return "hole_in_one"
    diff = strokes - par
    return {
        -3: "albatross",
        -2: "eagle",
        -1: "birdie",
        0: "par",
        1: "bogey",
        2: "double_bogey",
    }.get(diff, "other")


@dataclass
class HoleResult:
    hole: int
    par: int
    stroke_index: int
    received: int
    strokes: int | None
    drive_player_id: int | None
    net_points: int | None
    gross_points: int | None
    label: str | None
    updated_at: str | None = None


@dataclass
class FlightScore:
    flight_id: int
    team_hcp: int
    holes: list[HoleResult]
    net_total: int = 0
    gross_total: int = 0
    holes_played: int = 0
    net_pace: int = 0
    gross_pace: int = 0
    status: str = "not_started"  # not_started | playing | finished
    current_hole: int | None = None
    last_hole: int | None = None
    last_update: str | None = None
    drives: dict[int, int] = field(default_factory=dict)


def play_order(start_hole: int) -> list[int]:
    """Holes in the order a flight plays them (shotgun start)."""
    start = max(1, min(HOLES, start_hole or 1))
    return [((start - 1 + i) % HOLES) + 1 for i in range(HOLES)]


def compute_flight(
    flight_id: int,
    team_hcp: int,
    start_hole: int,
    course: dict[int, dict],
    scores: dict[int, dict],
) -> FlightScore:
    """Compute a full card for one flight.

    ``course`` maps hole number -> {"par", "stroke_index"}.
    ``scores`` maps hole number -> {"strokes", "drive_player_id", "updated_at"}.
    """
    holes: list[HoleResult] = []
    for n in range(1, HOLES + 1):
        c = course.get(n, {"par": 4, "stroke_index": n})
        s = scores.get(n) or {}
        strokes = s.get("strokes")
        received = strokes_on_hole(team_hcp, c["stroke_index"])
        holes.append(
            HoleResult(
                hole=n,
                par=c["par"],
                stroke_index=c["stroke_index"],
                received=received,
                strokes=strokes,
                drive_player_id=s.get("drive_player_id"),
                net_points=stableford(c["par"], strokes, received),
                gross_points=stableford(c["par"], strokes, 0),
                label=score_label(c["par"], strokes),
                updated_at=s.get("updated_at"),
            )
        )
    fs = FlightScore(flight_id=flight_id, team_hcp=team_hcp, holes=holes)
    played = [h for h in holes if h.strokes is not None]
    fs.holes_played = len(played)
    fs.net_total = sum(h.net_points or 0 for h in played)
    fs.gross_total = sum(h.gross_points or 0 for h in played)
    fs.net_pace = fs.net_total - 2 * fs.holes_played
    fs.gross_pace = fs.gross_total - 2 * fs.holes_played
    for h in played:
        if h.drive_player_id:
            fs.drives[h.drive_player_id] = fs.drives.get(h.drive_player_id, 0) + 1
    order = play_order(start_hole)
    if fs.holes_played == 0:
        fs.status = "not_started"
        fs.current_hole = order[0]
    elif fs.holes_played >= HOLES:
        fs.status = "finished"
    else:
        fs.status = "playing"
        remaining = [n for n in order if scores.get(n, {}).get("strokes") is None]
        fs.current_hole = remaining[0] if remaining else None
    with_time = [h for h in played if h.updated_at]
    if with_time:
        last = max(with_time, key=lambda h: h.updated_at or "")
        fs.last_hole, fs.last_update = last.hole, last.updated_at
    return fs


def countback_key(fs: FlightScore, start_hole: int, net: bool = True) -> tuple:
    """Sort key: total, then last 9 / 6 / 3 / 1 holes played (countback)."""
    order = play_order(start_hole)
    pts = {h.hole: (h.net_points if net else h.gross_points) or 0 for h in fs.holes}
    total = fs.net_total if net else fs.gross_total
    tail = [pts[n] for n in order]
    return (
        total,
        sum(tail[-9:]),
        sum(tail[-6:]),
        sum(tail[-3:]),
        tail[-1],
    )


def rank_flights(
    entries: list[tuple[FlightScore, int]], net: bool = True
) -> list[dict]:
    """Rank flights with countback. ``entries`` are (score, start_hole).

    Flights with identical countback keys share a rank; otherwise the
    countback decides and ``tie_broken`` flags that the total alone tied.
    """
    keyed = sorted(entries, key=lambda e: countback_key(e[0], e[1], net), reverse=True)
    out: list[dict] = []
    prev_key: tuple | None = None
    rank = 0
    for i, (fs, start) in enumerate(keyed, start=1):
        key = countback_key(fs, start, net)
        if key != prev_key:
            rank = i
        tie_broken = prev_key is not None and prev_key[0] == key[0] and prev_key != key
        prev_key = key
        out.append({"flight_id": fs.flight_id, "rank": rank, "tie_broken": tie_broken})
    return out
