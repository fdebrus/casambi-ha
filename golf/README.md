# Fairway Live — live scoring for a team golf competition

Mobile-first web app for a one-day team scramble: flights enter their
scores from the course with a PIN, everybody follows the live ranking, the
feed posts birdies, prize signs and photos, and players can call the
marshal or ask for water with one tap.

Stack: Python 3.11+ / FastAPI / SQLite, a dependency-free vanilla JS
front-end (no build step), one admin page.

## Features

| Tab | What it does |
|---|---|
| **Classement** | Net / gross Stableford ranking with countback tie-break, podium for the top 3, day programme card, "how it works" and "your day" panels. Tapping a flight opens its full card (par, strokes received, score, drive used, net and gross points, drives per player). |
| **Carte** | Flight PIN login (works on any number of phones), one card per hole with a stroke stepper, drive-used chips, live points, and prize sign declaration (longest drive, nearest to pin) on the relevant hole. Read-only once scoring is closed. |
| **Rookies** | Intro text and a station-points ranking for the beginners' clinic. |
| **Fil** | Photo + text posts with replies and four reactions (Bravo, Feu, Rire, Beau coup), automatic posts for birdies / eagles / hole-in-one, prize signs and the final result, unread badge, optional browser notifications. |
| Header | Live badge, FR/EN, light/dark theme, "Prix & Parcours" (prizes with current sign holders + course card). |
| Floating buttons | **Marshal** and **Eau** requests, queued for the organisers with flight and hole. |
| `/admin` | Event settings, course (par / stroke index / length), flights & players with PINs, score corrections, prizes, rookies, feed moderation, requests queue, JSON import/export, reset tools. |

### Scoring rules (see `app/scoring.py`)

- Team scramble: one gross score per hole per flight.
- Team handicap = share of the players' summed handicaps by team size
  (default 4 players → 12.5 %, i.e. average ÷ 2; 26 + 18.5 + 27.5 + 36 → 14).
  Editable per event, or overridden per flight.
- Strokes allocated by stroke index; Stableford net and gross points;
  pace = points − 2 × holes played.
- Ties broken on the last 9 / 6 / 3 / 1 holes played (shotgun order aware).

## Run locally

```bash
cd golf
pip install -r requirements-dev.txt
GOLF_ADMIN_TOKEN=change-me uvicorn app.main:app --reload
# app:   http://localhost:8000
# admin: http://localhost:8000/admin
pytest
```

A fresh database is seeded with fictional demo flights (PINs 1111 … 6666).
Set `GOLF_SEED_DEMO=0` to start empty, then load the real flights via
*Admin → Outils → Importer* (JSON) or the Flights tab.

## Deploy (Docker)

```bash
cd golf
GOLF_ADMIN_TOKEN='a-long-secret' docker compose up -d --build
```

The container listens on port 8000 and keeps the database and uploaded
photos in the `golf-data` volume. Put it behind Caddy / Traefik / nginx for
HTTPS (needed for browser notifications and camera upload on iOS).

Environment variables: `GOLF_ADMIN_TOKEN` (required in production),
`GOLF_DB`, `GOLF_UPLOADS`, `GOLF_SEED_DEMO`.

## Before the day

1. Admin → **Parcours**: enter the real par, stroke index and length of
   each hole (the defaults are placeholders; only the pars visible on the
   reference scorecard were copied).
2. Admin → **Événement**: title, date, course,
   programme, texts.
3. Admin → **Flights** or **Outils → Importer**: flights, players,
   handicaps, start holes. Print each flight's PIN on its registration card.
4. Admin → **Outils**: reset demo scores / posts.
5. On the day, keep the **Demandes** tab open for marshal and water calls.
   Close scoring from **Événement** once every card is checked.
