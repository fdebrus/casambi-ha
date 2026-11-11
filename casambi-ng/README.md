# casambi-ng — the integration, rewritten

A rewrite of the Casambi Bluetooth integration in this repository: **same behaviour, same entities, cleaner structure.** It is a drop-in replacement, not a second integration.

> [!WARNING]
> Not yet run on the pergola. It passes the whole test suite of the current integration (ported to the new structure) plus tests of its own, but the motors, the wall buttons and the Bluetooth proxy have only seen the old code. Try it when you can watch the pergola.

## What stays the same

- The domain (`casambi_bt`), the config entry and its options.
- Every entity's unique id, so entity ids, dashboards, automations, history and restored settings carry over.
- What the integration does: see the [main README](../README.md).

## What changed

| Before | Now |
|---|---|
| `__init__.py` held the connection, the reconnect loop, a proxy around the library and the shared state of the automations (944 lines) | `hub.py` holds the connection only; commands go through one explicit `hub.async_command(...)` |
| The louvre and screen motion logic lived in the cover entities (`cover.py`, 1170 lines) | `motor_model.py` holds it as plain Python, with no Home Assistant in it: fed the unit's state and the time, it says where the motor is and what to send. `cover.py` only shows it and applies the locks |
| The weather protection, sun tracking and frost lock lived in switch entities and talked through dicts and callback registries on the api | `pergola.py`, `weather.py` and `sun_tracking.py` hold them; the switches and dials only turn them on and set their values |
| The weather protection asked the cover entity where the motor rests, through registries, and ran "late" to see the cover's view | `motor.py`: each louvre and screen has a `Motor` that exists with or without its cover entity; the weather protection watches it directly |
| Knowledge of the library's internals was spread out | `library.py` and `switch_decoder.py` are the only places that reach into casambi-bt |
| Entities subscribed and unsubscribed by hand | Subscriptions return an unsubscribe callback, handed to `async_on_remove` |

One behaviour differs on purpose: a motor whose cover entity is disabled keeps its motion model, so the weather protection still knows when it rests and still reverses a screen that is extending when the wind rises. Before, it fell back to the reported position.

## Layout

```
custom_components/casambi_bt/
  __init__.py        setup and unload
  hub.py             the Bluetooth link: connect, reconnect, pushes, commands
  library.py         what is known about casambi-bt beyond its public API
  switch_decoder.py  wall button frames of input-only modules
  classify.py        what kind of hardware a unit is
  louvre_channels.py platform_packets.py   decoding of raw states
  motor_model.py     how louvres and screens move (pure Python)
  motor.py           one motor: unit + model + timer + commands
  pergola.py         automation settings, frost lock, weather lock
  weather.py         weather protection
  sun_tracking.py    sun tracking;  suntrack.py: its geometry
  entity.py          entity base classes
  cover.py light.py switch.py number.py sensor.py binary_sensor.py
  event.py scene.py  the platforms
  config_flow.py diagnostics.py
tests/               the ported suite, plus test_models.py (pure model tests)
```

## Trying it

1. Make a backup, or at least note the installed version in HACS.
2. Copy `casambi-ng/custom_components/casambi_bt/` over `/config/custom_components/casambi_bt/` (replace the folder, don't merge).
3. Restart Home Assistant. Nothing needs to be reconfigured.

To go back: in HACS, *Redownload* the integration, then restart.

Worth checking on the pergola: open/close/stop from Home Assistant and from the wall buttons, a position from the Casambi app, the weather protection (set the wind entity above the threshold while a screen closes), a Bluetooth proxy restart, and the diagnostics download, which now has a `motors` section.

## Development

Run everything from this directory:

```
pytest -q
mypy
ruff check . && ruff format --check .
```

`tests/test_models.py` replays captures on the motor models without Home Assistant; the other tests go through Home Assistant with the library mocked.

## Not done yet

- **Learned travel time.** The louvres' 22 s is a constant (a parameter of `LouvreModel`); the motor clears its bit at the end of a full run, which would give the real time.
- **A new target during a screen's button run.** The screen was seen to follow one; the model still ignores it, as the current integration does.
- The private attributes of casambi-bt that `library.py` reads are still needed until the library releases a stale client itself.
