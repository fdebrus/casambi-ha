# Development

This document explains how the fork is built and tested, what has been decoded about the Winsol pergola, and a possible setup for development. The integration talks to the network through the [casambi-bt](https://github.com/lkempf/casambi-bt) library.

## Architecture

The integration is `local_push`: the `casambi-bt` library keeps a BLE connection to the network open and pushes unit state changes via callbacks. `CasambiApi` (in `__init__.py`) owns the connection, handles automatic reconnects, and fans out per-unit change callbacks that entities subscribe to in `async_added_to_hass`.

**Why is there no `DataUpdateCoordinator`?** The coordinator pattern exists to centralize *polling* so that many entities share one fetch cycle. This integration never polls — state arrives as push callbacks from the BLE mesh — so a coordinator would add indirection without benefit. `CasambiApi` already fulfils the coordinator's other role (a single shared connection/state owner). All entity platforms therefore declare `PARALLEL_UPDATES = 0` and `should_poll = False`.

**State writes.** A unit pushes its whole state several times a minute — the louvre motor cycles through its four sensor channels every few seconds — and every entity of the unit is notified each time. `CasambiUnitEntity._change_callback` therefore writes to Home Assistant only when the entity's `_snapshot()` differs from what it last wrote: a channel sensor returns its value, a relay its bit, the automation switches and dials (`CasambiUnitConfigEntity`) only their availability, since their value is not in the pushed state. Entities that show the whole state (covers, lights) keep the default and write on every push. Entities that need to decode a push before deciding override `_apply_update()` rather than `_change_callback`.

The integration status against the Home Assistant [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) is tracked in `custom_components/casambi_bt/quality_scale.yaml`.

## Connection handling

A BLE mesh link drops often — the adapter is busy, the proxy reboots, the
pergola is at the end of the garden — so losing the connection is normal
operation, not an error.

* **One reconnect task.** A disconnect callback from the library, or a
  bluetooth advertisement for a network we are not connected to, calls
  `_schedule_reconnect()`. It starts `CasambiApi.reconnect()` as a single
  background task and never starts a second one while it runs, so
  simultaneous triggers cannot stack up connection attempts.
* **Exponential backoff.** The task retries with a delay that starts at
  `RECONNECT_BACKOFF_START` (2 s) and doubles up to
  `RECONNECT_BACKOFF_MAX` (5 min). When Home Assistant reports the device
  as out of range the delay jumps to `RECONNECT_BACKOFF_NO_DEVICE` (60 s)
  at once — retrying faster cannot help.
* **Protocol errors are retried slowly, not abandoned.** After two in a
  row the delay goes straight to `RECONNECT_BACKOFF_MAX`. A changed
  network key shows up here too: `Casambi.reconnect()` only redoes the BLE
  handshake and never logs in, so it cannot raise `AuthenticationError`;
  the reauth flow is only ever started from the initial connect (setup or
  a Home Assistant restart).
* **Writes trigger reconnects.** Entities reach the library through
  `CasambiProxy`, which wraps every coroutine call: a `BluetoothError`
  during a write with the library no longer reporting itself connected
  means it gave the link up without running its disconnect callbacks, so
  the entities are told the link is down and a reconnect is scheduled,
  exactly as after a reported disconnect. A write that fails while the
  library still says connected (a timeout) is only reported: the
  library's reconnect would return at once, and a real drop follows
  through the callbacks. The library also reconnects on its own inside a
  write; a write that succeeds while the link is marked down therefore
  marks it up again without waiting for the reconnect task. Unlike
  upstream the error is re-raised, so the failed command is still
  reported to the caller as a `HomeAssistantError` rather than silently
  doing nothing.
* **Entities follow the connection.** `CasambiApi` fans out connection
  state changes to entities (`register_connection_updates`), so the
  **Status** binary sensor and every entity's availability update the
  moment the link drops or comes back.
* **Long outages are logged.** After `RECONNECT_WARN_AFTER` (5 min) of
  failed attempts a single warning names the last reason (*no Bluetooth
  adapter or proxy in range sees it*, or the Bluetooth error); a second
  warning reports when the network is reachable again. Shorter failures
  stay at debug level.
* **Unloading wins.** `disconnect()` sets `_closing`, cancels a running
  reconnect task and waits up to 5 s for it; nothing schedules a reconnect
  afterwards, not even a write failing during the teardown.

### Pitfalls found on the pergola

Two outages on the real installation (the ESPHome proxy near the pergola
dropped off Wi-Fi) exposed two traps in how the library reports a
disconnect. Both are covered by tests that reproduce the library's order
of operations.

1. **The library calls back while it still says connected.**
   `CasambiClient._on_disconnect` marks every unit offline and runs the
   disconnect callbacks while `_connectionState` is still `AUTHENTICATED`,
   and only sets it to `NONE` afterwards. Reading `casa.connected` inside
   the callback therefore returns `True`: the Status sensor was written
   *on* and never refreshed, while every unit entity was unavailable.
   `CasambiApi` now tracks the link itself (`_link_up`): down in the
   disconnect callback, up after a successful connect or reconnect, and
   `available` is `_link_up and casa.connected`.
2. **The reconnect task starts inside that callback.** Home Assistant
   starts background tasks eagerly, so `reconnect()` ran synchronously
   from the callback, still before the library switched to `NONE`.
   `Casambi.reconnect()` begins with `if self.connected: return`, so it
   returned at once without connecting, and the loop took that for
   success and stopped. The units then stayed unavailable for hours even
   after the proxy was back: the advertisement callback did not wake us,
   presumably because Home Assistant skips advertisements whose content
   did not change. `reconnect()` now yields once (`asyncio.sleep(0)`)
   before its first attempt, and a reconnect that returns while
   `casa.connected` is still false counts as a failed attempt.

The motor covers also forget the direction of their last run and their
rest position when the link drops (`_connection_callback`), the screen
its motion estimate as well: the motor may have been driven by the wall
button meanwhile, so the first state pushed after a reconnect is taken as
rest, open and close send a position until a run is seen again, and a
button run already under way then is left alone until it ends and the
motor reports where it stopped.

The automation switches (sun tracking, temperature control, frost lock,
weather protection) keep their setting in the restore store's extra data
(`extra_restore_state_data`) rather than reading it back from the entity
state: that state is "unavailable" whenever the network is, and a restart
or reload during an outage used to turn them all off.

Two more traps, found by review rather than on the pergola:

3. **Every cloud answer but OK is an `AuthenticationError`.** The library
   raises it for a 5xx or 429 from api.casambi.com too, which happens when
   the pickled session has expired during a cloud outage. Taken for a wrong
   password it starts a reauth, and re-entering the password wipes the
   network cache (`invalidateCache`), which is what offline operation runs
   on. `connect()` reads the status out of the message and retries those
   like any other outage.
4. **Giving up on protocol errors moved the retries elsewhere.** With the
   reconnect task gone, every advertisement of the network started a fresh
   one, two attempts at a time and without any backoff. Repeated protocol
   errors now keep the task alive at the slowest backoff. A changed network
   key shows up here as well: `Casambi.reconnect()` only redoes the BLE
   handshake, it never logs in, so it can't raise `AuthenticationError`.

The units are marked offline by the library itself on a disconnect, and
come back only when the network pushes their state again after a real
reconnect, so a unit entity that stays unavailable while Status is on means
the reconnect did not really happen.

The backoff and reauth logic is ported from upstream `dev`; the link tracking, the eager-start guard and the outage warnings are this fork's. See `tests/test_reconnect.py`.

## Decoded Winsol hardware

Based on real fixture definitions and on protocol work by the
[superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass)
fork (whose library work has since been merged into upstream `casambi-bt`,
which this integration uses):

| Unit | Mode | Controls | Exposed as |
|---|---|---|---|
| Winsol Lamel Standard V4.1 (louvres) | `EXT/Elements` | slider `$pos` (0–142°, bits 28–35), onoff `$startstop` (bit 36), 4 multiplexed sensor channels (header bits 0–3, value bits 4–27): Temperatuur °C, #Overcurrent, Travel Distance, Module State | Cover (blind) with position + stop, plus a temperature sensor and three diagnostic sensors |
| Winsol SO! V4.1 (screen) | `EXT/1ch/Dim` | dimmer "Screen Position" (bits 28–35, reports the *target*), onoff `$toggle` (bit 36, **reverses** the motion), travel-time sensors *Time Up* (channel 1) and *Time Down* (channel 2), ms | Cover (shade) with position (estimated from the travel time while moving, with opening/closing), stop = clear the start/stop bit for a button run, SetLevel just ahead of the estimated position otherwise |
| Winsol On-Off (relay, e.g. heating) | `EXT/Elements` | a single writable onoff (bit 0, 1-byte state) | Switch (writes the ONOFF bit both ways; the library's `turnOn` sends `SetLevel ff05`, which the relay ignores) |
| Sensor Platform V4 (weather) | `EXT/Elements{Presence,Daylight}` | presence (bits 0–1), lux (bits 2–13), rotating wind/sun/PIR/rain (bits 14–33), 4 enable switches | Wind/solar/illuminance sensors, rain/motion/presence binary sensors |

The rotating sensor packets are accumulated by the library in
`unit.sensor_cache`, keyed by packet type: 0 = rain (1 dry, 5 raining),
1 = wind (raw/4), 2 = solar (raw/4), 3 = PIR. Unit classification lives in
`classify.py`.

### Screen position is inverted — confirmed

The SO! screen's dimmer counts how far the screen is *extended*: raw 0 is
fully retracted (open), raw 255 fully extended (closed) — the opposite of
Home Assistant's cover position, where 100 is open. `CasambiScreenCover`
inverts in both directions (`100 - dimmer * 100 / 255` on read,
`(100 - position) * 255 / 100` on write), and the weather protection
retracts a screen by writing raw 0.

This was first reported by the
[superkikim](https://github.com/superkikim/casambi-bt-hass) fork from a
nine-shutter installation, and confirmed here on a Winsol SO! Climate
pergola with three SO! V4.1 screens: before the inversion, *open* extended
the screen and *close* retracted it.

The louvre slider (0–142°) is **not** inverted: slider 0 is the louvres
closed flat into a sealed roof, 142 fully open, matching the Home Assistant
position straight through. Confirmed on the same pergola — with the roof
physically closed the unit reports `slider: 0` and the cover shows closed.

### Screen: the position reported after a button stop is not a run

When the wall button stops the screen, the integration freezes the
estimate and the screen then reports where it actually stopped, a few raw
units off the estimate. Read as a run, the sign of that correction set the
direction of the last run (`_last_raising`), which decides which way the
start/stop bit sends the motor next: open or close from Home Assistant
could then drive it the wrong way. The report after a button stop is
taken as the rest position (`_settling`) and says nothing about the
direction; a repeat of the stale target is ignored.

### Screen stop — only a target ahead of the screen is honoured

Five captures on the SO! V4.1 at device id 1 (5-byte states,
little-endian; dimmer = bits 28–35 = the *target*, `$toggle` = bit 36):

```text
# 1. Stop as the onoff toggle (what the integration first did)
17:43:39.846  sent      SetLevel ff               close
17:43:40.000  reported  818b08f00f                target 255, toggle 0
17:43:46.904  sent      SetState 818b08f01f       target 255, toggle 1
17:43:47.371  reported  818b080010                target 0 -> retracts

# 2. Stop as the estimated reached position (35 s full travel)
17:51:55.073  sent      SetLevel ff               close
17:52:02.070  sent      SetLevel 34               position 52 at 7 s
17:52:02.290  reported  818b084003                target 52 acknowledged
              ... the screen kept extending to the end

# 3. The Casambi app's own close then stop (nothing sent by HA)
17:58:21.169  reported  818b084003                target 52
17:58:28.575  reported  818b080000                target 0 -> retracts

# 4. Estimated position 8.8 s into a close (estimate ahead of the screen)
18:04:08.453  sent      SetLevel ff               close
18:04:17.299  sent      SetLevel 41               position 65
              ... the screen stopped there

# 5. The wall button (24VDC_4CH_PB, button 2 is bound to this screen)
19:19:17.516  press
19:19:17.745  reported  828b080010                target 0 (stale), bit 1 -> extends
19:19:23.352  press
19:19:23.586  reported  818b080000                bit 0 -> stops
19:19:23.757  reported  818b08a002                target 42 = where it stopped
19:19:26.558  press
19:19:26.931  reported  828b08a012                target 42, bit 1 -> retracts
```

Capture 5 explains the bit: it is a run flag driven like the wall button.
Setting it (0 -> 1) starts the motor in the direction opposite its last
run, clearing it (1 -> 0) stops the motor, which then reports where it
stopped. A position command leaves the bit at 0, which is why setting it
during such a run (capture 1) reversed the screen. The 35 s travel model is
confirmed by it too: 5.8 s of extension is raw 42.3, and the screen
reported 42.

The motor counts runs started by a position command too: after a SetLevel
close the bit retracted the screen (capture 1), and after a SetSlider close
it opened the louvres. While the bit is set the motor keeps reporting the
position it last rested at, and after it clears it repeats that once before
reporting where it stopped. `CasambiMotorCover` follows this to predict the
bit's direction (up from the closed end, down from the open end, otherwise
opposite the last run) and drives open/close with the bit whenever that is
the requested way, so stop can clear it like the button. Otherwise
open/close send a position, and the subclass handles stop (aim ahead on
screens, nothing on louvres). Open during a close (or the reverse) started
with the bit clears and sets it again, like pressing the button twice.

The library applies the bit to the unit the moment Home Assistant writes
it, so a run it starts is recorded then (`_bit_run_started`), and the
motor's echoes within `BIT_ECHO_SECONDS` (a clear with the stale rest
when reversing, then the set bit) are not read as a rest or a new run.
A position change at rest with no run in between is a position-driven
move whose direction is the way it went; the stop position reported after
a bit run is only read that way when the run's direction is unknown, since
after a reversal the stale rest it is compared with predates two runs.
On a screen, open or close during a position-driven move sends the end as
a position when it lies ahead and sets the bit, which reverses the motor,
when it lies behind.

So the toggle reverses, and the vendor app has no working stop either. A
position sent while the screen moves is honoured only when it lies ahead
of it: 52 at 7.1 s was just behind the screen and ignored, 65 at 8.8 s was
ahead and stopped it. `CasambiScreenCover` therefore stops by sending the
estimated position 1.5 s ahead in the direction of travel (rounded further
along), and sends nothing at rest or within that margin of the target. When
the bit is set — a run started by the wall button or the app — it clears
the bit instead, exactly like the button. Such a run is also tracked for
the displayed position: it heads for the end opposite the last run, the
reported target is ignored while the bit is set, and the estimate freezes
when the bit clears. Confirmed on hardware for both stops.

Because the reported dimmer is the target, a close used to show "closed"
at once. The cover estimates the position from the timestamp of each
target change and the travel time for that direction — *Time Up* on
channel 1 when retracting, *Time Down* on channel 2 when extending (35000 ms
both ways here,
the default until seen), reports opening or closing meanwhile, and
refreshes once a second until the estimate arrives. The louvres' onoff
bit (`$startstop`) behaves the same way: setting it while they moved after
a SetSlider reversed them (18:24:42 target 0 -> 255, 18:25:21 target
255 -> 0). Their stop therefore only clears the bit of a button run; a run
started from Home Assistant is left alone until their travel time is
known for an aim-ahead stop.

A run Home Assistant does not see — while it is disconnected or
restarting — leaves the reported target stale: after a wall-button run to
the end the unit can keep reporting the old target with the bit clear, so
the cover shows the wrong end. Resynchronise with a **position** command
to where the screen really is (at the end it does not move). *Open*/*close*
would set the bit, which runs opposite the motor's last run and can move
it the wrong way.

### SO! V4.1 fixture (27814)

The screen's fixture definition (`https://api.casambi.com/fixture/27814`,
vendor LEDsGo, firmware type `SABI`) names the two sensors **Time Up**
(tag 0, channel 1) and **Time Down** (tag 1, channel 2), in ms, 0–300000.
They are the travel times measured by the motor's calibration run, so they
follow a recalibration; the integration reads them live. It also declares
one local push-button input (`pushButtonCount: 1`, none configured) and two
alternative profiles (27815, 27816).

Its parameters, set in the Casambi app, as found on this pergola:

| Parameter | Value | Meaning, as far as known |
|---|---|---|
| Idle | 200 ms | pause before the motor can run again (presumably before reversing) |
| Back Impulse | 30 ms | short reverse pulse after a stop, to release the fabric tension |
| Operation Mode | Normal | *Calibration* re-measures the travel times |
| Motor Direction | Left | mounting side |
| Sensor ID | 7 | unit id of a sensor to obey; 7 is the louvre motor on this network, and the louvre declares 7 too, so most likely a factory default for a Winsol sensor platform |
| Service Mode | On | unknown |
| Rain Sensor | No | the screens ignore rain; no rain sensor on this pack |

### Real-world network layout

For reference, a Winsol SO! Climate pergola commissioned in 2026 exposed
nine units: three `SO! V4.1` screens (`EXT/1ch/Dim`, 5-byte state, DIMMER
bits 28–35 + ONOFF bit 36), one `Winsol Lamel Standard V4.1 TA16`
(`EXT/Elements`, 5-byte state, SLIDER bits 28–35 with max 142 + ONOFF bit
36 — the layout this integration was built on, with no native automation
controls), one `Winsol Mono` dimmer for the spots (`PWM/1ch/Dim`), one
`Winsol On-Off` relay (`EXT/Elements`, a single ONOFF — it switches the
pergola heating and is exposed as a `switch`), one
`24VDC_4CH_PB_Winsol` push-button interface (mode `PushButton`, one
read-only 8-bit UNKNOWN control — the source of the wall-switch events, see
below),
and two `Repeater` units (mode `Sensor`). **No sensor
platform was present**, so the pergola has no weather sensing of its own:
weather protection runs on Home Assistant entities chosen in the options
(here a nearby Netatmo station's rain gauge, anemometer and thermometer).
The network was only reachable through an ESPHome Bluetooth proxy near the
pergola; when that proxy dropped off Wi-Fi, the whole network went
unavailable.

All of its fixture definitions have been read
(`https://api.casambi.com/fixture/<id>`):

| Fixture | Model | Controls | Parameters | Notes |
|---|---|---|---|---|
| 38915 | Winsol Lamel Standard V4.1 | 4 sensors, slider `$pos`, onoff `$startstop` | orientation, current limit, sensor id, … (below) | the louvres |
| 27814 | SO! V4.1 | sensors *Time Up* / *Time Down*, dimmer, onoff `$toggle` | idle, back impulse, calibration, … (below) | the screens |
| 10025 | Winsol On-Off | one onoff "On/Off" (flags 6) | none | the heating; has a configurable start-up state |
| 7799 | Winsol Mono | one 8-bit dimmer | none | the spots; five alternative profiles, configurable start-up state |
| 39267 | 24VDC_4CH_PB_Winsol | read-only `pushButtonState` | none | 4 button inputs (below) |
| 15248 | Repeater | one 1-bit `placeholder` | none | only extends the mesh; its presence/light-sensor flags (`maxLux` 1000) come from the generic module and it never reports readings |

The relay and the spots match what the integration does with them — a
`switch` writing the ONOFF bit, a dimmable `light`. Two things stay
unknown: what the relay's on/off `flags` value 6 means (the screen's
`$toggle` has 0), and the *start-up state* both offer in the Casambi app.
The latter decides whether the heating comes back on after a power cut,
so it is worth checking in the app.

The Lamel itself declares four SENSOR controls and cycles through four
channels in its state every ~4 s. Casambi's fixture definition
(`https://api.casambi.com/fixture/38915`) names them by tag, and the
channel header is tag + 1:

| Channel | Tag | Fixture name | Unit / range | Observed on this pergola |
|---|---|---|---|---|
| 1 | 0 | Temperatuur | °C, 0–255 | 26 → 14 over an evening, ~2 °C above outdoor at night |
| 2 | 1 | #Overcurrent | counter | 0 |
| 3 | 2 | Travel Distance | counter | 20 → 31, stepping with louvre movements |
| 4 | 3 | Module State | 0–255 | 50 |

The same definition lists the motor's configuration parameters (set in the
Casambi app, not on the wire): Orientation (1 North … 8 North-West, this
unit: North), Current Limit (5000 mA), Zero Motor, Sensor ID (7),
Temperature Sensor Enable, Service Mode, Motor Direction (Left/Right) and
Error Step Percentage (5 %). Its `strings` also carry the labels of the
Intelligent variant's controls ($temp Cool/Warm, $auto, $intel,
$control_dimmer Shadow/Sun). Negative temperatures are untested; the
control is declared 0–255.

### Wired push buttons report on the input stream only

Switch events arrive as type-7 packets holding INVOCATION frames (flags u16,
opcode u8, origin u16, target u16, age u16, payload). EnOcean switches send
each action twice in one packet: on the 0x06 button stream (opcode 29–36)
and on the 0x12 input stream (opcode 64–71, payload[0] = 0x01 press,
0x02 release, 0x09 hold, 0x0C release after hold). The library therefore
ignores 0x12 presses and releases and takes them from 0x06.

The `24VDC_4CH_PB_Winsol` never uses 0x06, so every short press was lost.
Captured on unit 4 (button index = opcode − 0x40, payload[1] repeats it;
`age` grows on retransmits of the same `origin`):

```text
1002 40 04b4 0412 0003 0100   button 1 press
1002 40 04b4 0412 0006 0100   retransmit
1002 40 04b6 0412 0004 0200   button 1 release
1002 43 04cc 0412 0002 0103   button 4 press
```

`switch_decoder.py` subclasses the library decoder and swaps it into
`CasambiBt._client` at import: 0x12 presses and releases are accepted from
units that have never been seen on 0x06, and units that use 0x06 keep the
library behaviour. Pressing the buttons did not move any motor, so on this
pack they are free for Home Assistant automations. The override can go once
the library handles input-only units itself.

The module's fixture (39267, LEDsGO, mode `PushButton`) declares four push
buttons, a single read-only 8-bit `pushButtonState` control and **no
parameters**. The illuminated rings of the Winsol buttons are powered
straight from the 24 V supply in the post (Winsol's mounting manual notes
that a miswired harness leaves the light working but the button dead), so
neither Home Assistant nor the Casambi app can switch them off.
`pushButtonState` might serve as a second source for presses and releases
that get lost in the mesh; untested.

### Winsol Lamel Intelligent (Star) — decoded, not implemented

The Star pack's louvre motor is a different, richer fixture: 7 bytes of
state instead of 5, exposing natively the intelligence this integration
reimplements in Home Assistant for the Cosy pack. Decoded by
[superkikim](https://github.com/superkikim/casambi-bt-hass) in
`lamel_controls.py`:

| Bits | Control | Meaning |
|---|---|---|
| 0–3 | sensorgroup header | read-only, selects which sensor is in the blob |
| 4–27 | sensorgroupvalue | read-only, 24-bit rotating sensor blob |
| 28–35 | DIMMER `$shadow` | shade/sun bias (0 = sun, 255 = shade) |
| 36–43 | SLIDER `$pos` | louvre angle, 0–142° |
| 44–51 | SLIDER `$temp` | Cool/Warm setpoint, 15–30 °C |
| 52 | ONOFF `$auto` | automatic mode |
| 53 | ONOFF `$intel` | intelligent mode |
| 54 | ONOFF `$startstop` | open/close toggle |

Nothing here is implemented: the Cosy pack ships the Lamel Standard, whose
5-byte state has `$pos` at bits 28–35 and `$startstop` at bit 36 and no
native automation at all — hence `suntrack.py` and the automation entities
in `switch.py`. The map is recorded because a unit that turns out to have
a 7-byte state is a Star, and then these controls should be exposed
directly instead of driving it from Home Assistant. Its sensor-header
mapping (which header carries the internal temperature) is an informed
guess in that fork, not confirmed.

Until then a Star must not be driven as a Lamel Standard: the library
keeps the last slider it parses as `state.slider`, which here is the
Cool/Warm setpoint, and the first writable ONOFF is `$auto`, not
`$startstop`. `classify.py` therefore only treats an EXT/ slider motor as
a louvre when it writes exactly one slider, at most one ONOFF and no
dimmer. Any richer motor is `UNSUPPORTED_MOTOR`: it gets no entities, a
warning in the log and a *Repairs* issue naming the unit and its model
(removed when the entry unloads).

## Pergola automation

**Sun tracking**: louvre units get a **Sun tracking** switch and a **Sun offset** number
(config entities on the louvre device). While the switch is on, the
integration recomputes the louvre angle every 5 minutes from the current
solar position (via Home Assistant's astral location — the same engine as
`sun.sun`): the slats are kept perpendicular to the sun's rays
(`angle = 90° − profile angle + offset`, where
`tan(profile) = tan(elevation) / cos(sun azimuth − pergola azimuth)`).
The pergola's compass orientation is set in the integration options
(default 180° = south). A deadband of ~3° avoids constant motor
adjustments; nothing moves when the sun is down or behind the pergola.
The math lives in `suntrack.py`, the scheduler in `switch.py`.

**Temperature control**: the per-louvre *Temperature control* switch and
*Temperature setpoint* number bias the sun tracking by
`(setpoint − temperature) × 10°` (clamped with the user offset to ±45°) —
warmer than the setpoint tilts toward shade, colder toward sun. The
temperature source entity is chosen in the integration options.

**Weather protection**: a network-level switch (created when a sensor
platform exists or a rain or wind entity is set in the options, and there
is a louvre or screen to protect). While on: rain closes all louvres and
pauses sun tracking until dry. Rain is the OR of the sensor platform
(packet 0 ≥ 2) and the rain entity (`entity_reports_rain`: binary sensor
on, weather condition rainy/pouring/lightning-rainy/snowy-rainy/hail, any
other numeric state above 0; unavailable/unknown/non-numeric keep the last
reading); wind at or above the configured threshold
(default 35 km/h) retracts all screens, closes all louvres and pauses sun
tracking, latched with 80% hysteresis; screens are never re-extended
automatically. Wind is the higher of the sensor platform (packet 1 / 4)
and the wind entity (`entity_wind_kmh`, converted from its unit with
`SpeedConverter`; no unit means km/h). It acts on the transitions, once
when rain or wind starts: reopening the roof during the rain is not undone.
A protective command that fails (`HomeAssistantError` from the proxy) is
kept in `_pending` with the reading it failed on, and sent again on a
later reading or when the connection comes back, as long as the rain or
wind it answers to is still active.

**Frost lock**: a per-louvre config switch (on by default, restored).
`CasambiApi.frost_lock_temperature(unit)` returns the reading while the
lock is on and the temperature entity reads below
`frost_lock_temperature` (default 2 °C), keeping the last numeric reading
when the entity can't tell. The louvre cover then raises
`ServiceValidationError` (`frost_lock`) on open and on any position above
the current one, and sun tracking skips its update; closing, stopping and
weather protection are not affected.

## Protocol capture workflow (decoding the pergola)

To fully decode what a unit (e.g. a Winsol pergola) speaks over BLE, capture two artifacts and correlate them:

1. **The schema — diagnostics download.** With the integration connected, go to Settings → Devices & services → Casambi Bluetooth → three-dot menu → *Download diagnostics*. The `units` section lists every unit's full control table: control type (including raw ids for types the library calls `UNKNOWN`), bit offset, bit length, min/max, and the current parsed state. This shows *what channels exist*. The `automation` section shows what the pergola automation currently holds (rain and wind latches, last temperature, the per-louvre switch and dial values), which is what to look at when the louvres don't move as expected.

2. **The traffic — debug log.** Enable debug logging in `configuration.yaml`:

   ```yaml
   logger:
     default: info
     logs:
       CasambiBt: debug
       custom_components.casambi_bt: debug
   ```

   Then drive the hardware through every state slowly, one action at a time, noting the time of each action: louvres fully open → 50% → closed (from the Casambi/Winsol app AND from HA), every wall switch / remote button (short press, long hold), lights on/off, and any accessories (screens, heating, sensors). The log contains the raw state bytes for every change (`Parsed <hex> to UnitState(...)`) and full switch packets (`[CASAMBI_SWITCH_PACKET] ...`).

3. Sanitize the log before sharing (it can contain the network password and account email) or share only the `CasambiBt` lines; the diagnostics download is redacted automatically.

Correlating "action performed" with "bytes that changed" identifies each control; unknown controls and unparsed message types are then implemented against the captured samples.

## Workflow

Changes go to a feature branch and reach `main` through a pull request on
the fork; `main` is what HACS installs. Before pushing, run:

```bash
pytest tests
ruff check custom_components tests
ruff format --check custom_components tests
mypy custom_components/casambi_bt
```

Changes meant for upstream ([lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass))
are prepared separately on a branch based on its `dev` branch, in its
style, and opened from the fork. Upstream has no test suite, so they are
checked against a temporary test that is not part of the branch.

### Upstream status

| Finding | Where | State |
|---|---|---|
| Status sensor never follows the connection; reconnect stops on the library's early return | integration | branch `fix-status-after-disconnect`, ready to open |
| Disconnect callbacks run before the state becomes `NONE` | library | to report as an issue |
| Wired push buttons (0x12 input stream only) | library | to propose, replaces `switch_decoder.py` |
| `turnOn` sends `SetLevel ff05`, ignored by On-Off relays | library | to report as an issue |
| Winsol covers, relay switch, louvre sensors | integration | to discuss in an issue first |

### Supporting several Home Assistant versions

The minimum supported core is 2026.6, the first to ship bleak 3, which
`casambi-bt` 0.4.0b4 and later require (this fork pins 0.4.0b5) (`hacs.json` enforces it). CI runs the
suite twice: `requirements_test.txt` pins the 2026.2.3 harness on Python 3.13
(below the minimum, but the last harness that installs on 3.13; pip pulls
bleak 3 into it for the library) and `requirements_test_latest.txt` the
2026.10.1 harness on Python 3.14. Two helpers changed in newer cores and the
integration adapts at import time rather than raising the minimum version
further:

- `homeassistant.helpers.sun.get_astral_observer` replaces
  `get_astral_location` (removed in 2027.7) — `suntrack.py` uses whichever
  exists.
- Devices link to their parent with `via_device_id` instead of the
  `via_device` identifier tuple (removed in 2027.8) — `entities.py` passes the
  network device's registry id, recorded on `CasambiApi.network_device_id`
  when the device is created in `async_setup_entry`, on cores whose
  `DeviceInfo` accepts it.

Locally, one venv per requirements file does the same
(`python3.14 -m venv venv314 && venv314/bin/pip install -r requirements_test_latest.txt`).

## Development setup

The development setup has the following goals:

1. Using arbitrary versions of homeassistant, even those that haven't been released.
1. Testing changes to `casambi-bt` locally without requiring an upload to pip.
1. Running homeassistant on the same device used for development, so no hassle with remote debugging.

This of course also has a caveat: The devices used for development must have proper bluetooth support and must be running Linux. If this isn't possible natively it can be achieved using a [bluetooth dongle](https://www.home-assistant.io/integrations/bluetooth/#known-working-high-performance-adapters). I haven't tested the setup under WSL (for those developing under Windows).

The development setup uses venvs. This is a feature that creates an isolated environment for python dependencies so multiple versions of the same dependency can be installed at the same time and changes don't affect your whole system. Understanding venvs isn't necessary to follow the setup instructions. If a venv breaks, it can easily be recreated by removing its folder (normally `venv` or `.venv`), creating a new one and reinstalling all dependencies according to the setup instructions.

These instructions assume the following folder layout (after all steps). This can of course be changed but then some commands have to be modified.

```text
casambi/
├── core/
├── casambi-bt/
└── casambi-ha/
```

### Getting homeassistant

Follow the official instructions for installing homeassistant in a manual environment: [https://developers.home-assistant.io/docs/development_environment](https://developers.home-assistant.io/docs/development_environment).

As a very quick summary this means:

```bash
git clone https://github.com/home-assistant/core.git
cd core
script/setup
source venv/bin/activate
```

After this the venv used by homeassistant is activated in the current shell, so all changes to dependencies now will changes those in use by homeassistant. If you open a new shell, make sure to execute `source venv/bin/activate` again in the correct directory, otherwise homeassistant won't start.

To make sure that homeassistant is running properly you can test it using

```bash
hass -c config
```

### Getting casambi-bt

Next you need to obtain `casambi-bt` by source. To do so use

```bash
git clone https://github.com/lkempf/casambi-bt.git
git checkout dev
```

If you want to use your own fork, the remote needs to be changed of course. Just remember to base your working branch on the `dev` branch.

You now need to install this folder as a dependency for homeassistant so that it is preferred over the newest published version. Make sure that the shell you are using has the correct venv activated (see [Getting homeassistant](#getting-homeassistant)) and that you have navigated to the project root.

```
pip install -e casambi-bt/
```

Some (optional) explanation of what this does: `pip` is used for managing dependencies for python libraries. Since you made sure that you are in a venv pip is aware of that and will install the library into the venv. Normally dependencies are downloaded from PyPi but by using `-e` you tell pip to use a folder as a source instead. This also means that any changes done to that folder are automatically picked up by homeassistant (after a restart) so that there is no need to reinstall anything after a change.

### Getting casambi-bt-hass

For the integration you also probably want homeassistant to always pick up your latest changes. Thankfully, this is easier as with dependencies. You can simply link the folder containing the integration into your development installation of homeassistant with the following commands:

```bash
git clone https://github.com/lkempf/casambi-bt-hass.git
cd core/config/
mkdir custom_components
cd custom_components
ln -s ../../../casambi-bt-hass/custom_components/casambi_bt
```

### Starting homeassistant

Homeassistant is now ready to be started using your development version. To avoid overwriting the development version of `casambi-bt` homeassisant should always be started using

```bash
hass -c config --skip-pip-packages casambi-bt
```
