# Development

This document explain the recommended development workflow as well as a possible setup for development. This workflow includes both the integration (this repository) as well as the interfacing library [casambi-bt](https://github.com/lkempf/casambi-bt).

## Architecture

The integration is `local_push`: the `casambi-bt` library keeps a BLE connection to the network open and pushes unit state changes via callbacks. `CasambiApi` (in `__init__.py`) owns the connection, handles automatic reconnects, and fans out per-unit change callbacks that entities subscribe to in `async_added_to_hass`.

**Why is there no `DataUpdateCoordinator`?** The coordinator pattern exists to centralize *polling* so that many entities share one fetch cycle. This integration never polls — state arrives as push callbacks from the BLE mesh — so a coordinator would add indirection without benefit. `CasambiApi` already fulfils the coordinator's other role (a single shared connection/state owner). All entity platforms therefore declare `PARALLEL_UPDATES = 0` and `should_poll = False`.

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
  at once — retrying faster cannot help, and the bluetooth callback will
  wake us as soon as the network advertises again.
* **Failures that retrying cannot fix.** A `ProtocolError` is retried once
  and then abandoned; an `AuthenticationError` (the network password was
  changed) starts a reauth flow instead of looping.
* **Writes trigger reconnects.** Entities reach the library through
  `CasambiProxy`, which wraps every coroutine call: a `BluetoothError`
  during a write means the link is gone before the library noticed, so a
  reconnect is scheduled. Unlike upstream the error is re-raised, so the
  failed command is still reported to the caller as a `HomeAssistantError`
  rather than silently doing nothing.
* **Entities follow the connection.** `CasambiApi` fans out connection
  state changes to entities (`register_connection_updates`), so the
  connectivity binary sensor and every entity's availability update the
  moment the link drops or comes back, instead of waiting for the next
  pushed unit state.
* **Unloading wins.** `disconnect()` cancels a running reconnect task and
  waits up to 5 s for it; once cancelled, no new reconnect is scheduled.

Most of this is ported from upstream `dev`; see `tests/test_reconnect.py`.

## Decoded Winsol hardware

Based on real fixture definitions and on protocol work by the
[superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass)
fork (whose library work has since been merged into upstream `casambi-bt`,
which this integration uses):

| Unit | Mode | Controls | Exposed as |
|---|---|---|---|
| Winsol Lamel Standard V4.1 (louvres) | `EXT/Elements` | slider `$pos` (0–142°, bits 28–35), onoff `$startstop` (bit 36), 4 multiplexed sensor channels (header bits 0–3, value bits 4–27) | Cover (blind) with position + stop, plus one raw diagnostic sensor per channel |
| Winsol SO! V4.1 (screen) | `EXT/1ch/Dim` | dimmer "Screen Position" (bits 28–35, reports the *target*), onoff `$toggle` (bit 36, **reverses** the motion), travel-time sensors (channels 1 and 2, ms) | Cover (shade) with position (estimated from the travel time while moving, with opening/closing), stop = clear the start/stop bit for a button run, SetLevel just ahead of the estimated position otherwise |
| Winsol On-Off (relay, e.g. heating) | `EXT/Elements` | a single writable onoff (bit 0, 1-byte state) | Switch (written with `turnOn`/`turnOff`) |
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
target change and the travel time on sensor channels 1 and 2 (35000 ms,
the default until seen), reports opening or closing meanwhile, and
refreshes once a second until the estimate arrives. The louvres' onoff
bit (`$startstop`) behaves the same way: setting it while they moved after
a SetSlider reversed them (18:24:42 target 0 -> 255, 18:25:21 target
255 -> 0). Their stop therefore only clears the bit of a button run; a run
started from Home Assistant is left alone until their travel time is
known for an aim-ahead stop.

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
and two `Repeater` units (mode `Sensor`, one UNKNOWN control). **No sensor
platform was present**, so the weather entities and the weather
protection switch do not appear on such a network; whether the pack's rain
sensor is wired directly to the motor rather than onto the mesh is an open
question for the installer.

The Lamel itself declares four SENSOR controls and cycles through four
channels in its state every ~4 s. Observed on that network: channel 1 rose
from 17 to 30 over a sunny day (temperature-like), channels 2, 3 and 4 held
0, 15 and 50. Each channel is exposed as a raw diagnostic sensor on the
louvre device so they can be identified (e.g. by wetting the rain sensor
and watching which channel reacts) before any semantics are attached.

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

## Sun tracking

Louvre units get a **Sun tracking** switch and a **Sun offset** number
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
platform exists). While on: rain (packet 0 ≥ 2) closes all louvres and
pauses sun tracking until dry; wind at or above the configured threshold
(default 35 km/h) retracts all screens, latched with 80% hysteresis, and
never re-extends them automatically.

## Demo mode

`demo.py` contains `DemoCasambi`, a local stand-in that implements the parts
of the `CasambiBt.Casambi` API the integration uses. It builds real `Unit`,
`Group` and `Scene` objects from the Winsol fixture definitions, applies
commands to their state and notifies the registered handlers, so every
platform behaves as it does against hardware. `CasambiApi` selects it when
the config entry carries `demo: true` and then skips Bluetooth entirely.

## Protocol capture workflow (decoding the pergola)

To fully decode what a unit (e.g. a Winsol pergola) speaks over BLE, capture two artifacts and correlate them:

1. **The schema — diagnostics download.** With the integration connected, go to Settings → Devices & services → Casambi Bluetooth → three-dot menu → *Download diagnostics*. The `units` section lists every unit's full control table: control type (including raw ids for types the library calls `UNKOWN`), bit offset, bit length, min/max, and the current parsed state. This shows *what channels exist*.

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

This repository uses two important branches `main` and `dev`. Everything happening in `main` should be tested and planned for the next release. Changes that haven't been tested properly should only be applied to `dev`.

If you want to contribute changes you should always target `dev`. The changes will be merged over to `main` in time for the next release.

All changes on `main` and `dev` must conform to the coding standards used for this repository. This will be verified automatically when creating a pull request but to avoid delays the necessary checks should be performed beforehand. This means running

```bash
ruff check .
black . --check
```

For `casambi-bt` also run `mypy .`. All of these tools should be available when following the recommended setup procedure.

### Supporting several Home Assistant versions

The minimum supported core is 2026.6, the first to ship bleak 3, which
`casambi-bt` 0.4.0b4 and later require (`hacs.json` enforces it). The test
requirements still pin the 2026.2.3 harness, the last one installable on
Python 3.13 as used by CI; pip pulls bleak 3 into it for the library, so the
suite runs fine there. Two helpers changed in newer cores and the integration
adapts at import time rather than raising the minimum version further:

- `homeassistant.helpers.sun.get_astral_observer` replaces
  `get_astral_location` (removed in 2027.7) — `suntrack.py` uses whichever
  exists.
- Devices link to their parent with `via_device_id` instead of the
  `via_device` identifier tuple (removed in 2027.8) — `entities.py` passes the
  network device's registry id, recorded on `CasambiApi.network_device_id`
  when the device is created in `async_setup_entry`, on cores whose
  `DeviceInfo` accepts it.

To run the suite against the newest core as well, create a second venv with
Python 3.14, install the latest `pytest-homeassistant-custom-component`, and
add the requirements listed in the `bluetooth`, `bluetooth_adapters` and `usb`
component manifests of that core (the harness does not install them).

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
└── casambi-bt-hass/
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
