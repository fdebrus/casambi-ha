<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/casambi_bt/brand/dark_logo@2x.png">
    <img src="custom_components/casambi_bt/brand/logo@2x.png" alt="Casambi logo" width="300"/>
  </picture>
</p>

# Casambi Bluetooth for Home Assistant — experimental fork

> [!WARNING]
> **This is an experimental fork — do not use it.**
>
> This repository is a personal playground based on [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass), used to experiment with Casambi-based pergola control (Winsol SO! louvres), wall switch events, and other changes. It can break or change at any time and comes with no support.
>
> 👉 **Please use the original project instead: [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass)**

---

The documentation below describes what this fork does, for my own reference.

## Tested hardware

> [!CAUTION]
> **This fork does not work with every Winsol pergola.** It was built and
> tested on a single installation, a Winsol SO! Climate pergola (Cosy pack),
> and only the units below are confirmed. Winsol ships other packs with
> different Casambi fixtures: the same pergola model can carry a louvre
> motor or screen whose controls sit at other places in its state. A louvre
> motor with more controls than the tested one is skipped and reported
> under *Repairs*; other differences are not detected.

| Unit | Status |
|---|---|
| Winsol Lamel Standard V4.1 TA16 (louvres, 5-byte state) | ✅ Tested |
| Winsol SO! V4.1 (screens) | ✅ Tested |
| Winsol On-Off (relay, pergola heating) | ✅ Tested |
| Winsol Mono (dimmable spots) | ✅ Tested |
| 24VDC_4CH_PB_Winsol (wall buttons) | ✅ Tested |
| Sensor Platform V4 (weather option) | ⚠️ Implemented from fixture definitions only, never run on real hardware |
| **Winsol Lamel Intelligent (Star pack, 7-byte state)** | ❌ **Not supported**: its louvre has native automatic/intelligent modes, shade/sun and cool/warm controls this fork does not handle. It is left alone: no entities are created for it and a warning appears under *Settings → System → Repairs* |
| Any other Winsol or Casambi motor | ❓ Unknown |

If your pergola has a different unit, do not use this fork to drive it;
[superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass)
supports the Lamel Intelligent. The integration's diagnostics download
lists every unit's name and control layout, so you can compare.

## Requirements

- Home Assistant **2026.6 or newer** (the underlying `casambi-bt` library needs bleak ≥ 3.0, which Home Assistant ships from 2026.6)
- A Bluetooth adapter in range of the Casambi network — or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) with **active connections enabled** near the pergola
- A Casambi network on *Evolution* firmware (set up via the Casambi or Winsol app)

## Installation & setup

1. Add this repository as a custom repository in HACS, download, restart HA.
2. The network is **discovered automatically** when it advertises — accept the discovered entry, or add manually via *Settings → Devices & services → Add integration → Casambi Bluetooth*.
3. Adding it manually offers two choices:
   - **Connect to a Casambi network** — the real thing; only the **network password** needs to be typed, the Bluetooth address is pre-filled by discovery.
   - **Demo (no hardware needed)** — see below.

All later settings live behind the integration's **Configure** button:

| Option | Meaning |
|---|---|
| Import groups | Create entities for Casambi groups |
| Vertical controls as covers | For *light* fixtures with a vertical channel (not needed for Winsol motors) |
| Pergola orientation (azimuth) | Compass direction the pergola faces, for sun tracking (180 = south) |
| Wind threshold | Wind speed (km/h) at which weather protection retracts the screens and closes the louvres |
| Temperature sensor | Any HA temperature sensor, used by the temperature control |
| Rain sensor | Any HA rain source (binary sensor, precipitation sensor or weather entity), used by weather protection — needed when the pergola has no Sensor Platform |
| Wind sensor | Any HA wind speed sensor (converted to km/h), used by weather protection — needed when the pergola has no Sensor Platform |

## What gets created

Units are classified automatically by their control layout (`classify.py`) — motors become covers and bare on/off relays become switches, never lights:

| Hardware | Entities |
|---|---|
| Lights / LED (dimmer, RGB(W), tunable white, XY) | `light` entities, plus group lights |
| Casambi scenes | `scene` entities |
| **Winsol louvre motor** (Lamel) | `cover` (blind) with position 0–100% (= 0–142°) and **Stop**, plus the automation entities below, plus its four multiplexed sensors: **temperature** (°C) and, as diagnostics, overcurrent events, travel distance and module state |
| **Winsol SO! screen** | `cover` (shade) with position and Stop |
| **Winsol On-Off relay** (e.g. the pergola heating) | `switch` |
| **Sensor Platform V4** (weather option) | `sensor`: wind speed (km/h), solar radiation, illuminance (lx); `binary_sensor`: rain, motion, presence; `switch`: per-element enables (wind/rain/light/motion) |
| Wall switches / remotes (e.g. Xpress, EnOcean) and wired push-button interfaces (e.g. Winsol `24VDC_4CH_PB`) | `event` entities per button (created on first press) + bus events |
| Network | `binary_sensor` connectivity status |

> [!NOTE]
> **Screen direction.** Casambi counts how far a Winsol SO! screen is
> *extended* (raw 0 = fully retracted, 255 = fully extended), the opposite
> of Home Assistant's cover position. The integration inverts this, so
> **open** retracts the screen and **close** extends it. Confirmed on SO!
> V4.1 hardware.
>
> **Open, close and stop work like the wall button.** Winsol screens and
> louvres have a start/stop bit, the one their wall button toggles: setting
> it starts the motor in the direction opposite its last run, clearing it
> stops it at once. **Open** and **close** set that bit whenever it would
> send the motor the way you asked — always from either end, and part way
> when the last run went the other way — so **Stop** is instant, exactly
> like the button. Otherwise they fall back to a position command; a
> screen moving that way is stopped with a position about 1.5 s of travel
> ahead of it (it ignores one behind it), and louvres are left running.
> Set position always uses a position command.
>
> **Screen position while moving.** A screen reports where it is heading,
> not where it is. While it moves — from Home Assistant, the app or the
> wall button — Home Assistant shows it as *opening* or *closing* with a
> position estimated from the 35 s travel time it reports, refreshed every
> second. A wall-button stop after 5.8 s was estimated at raw 42; the
> screen reported 42.

## Demo mode

Demo mode simulates a complete pergola so the integration can be tried **without any hardware, Bluetooth adapter or Casambi network** — useful for building dashboards and testing automations before the pergola is installed.

Add the integration and pick **Demo (no hardware needed)**. It creates a network with a louvre motor, a screen, a weather sensor platform, two lights, a heating relay, a light group and two scenes, built from the real Winsol fixture definitions. Covers move, lights dim and the sun tracking, temperature control and weather protection all work exactly as they do on real hardware.

Three extra config entities drive the simulation, so the automations can be tested deterministically:

| Entity | Effect |
|---|---|
| **Simulate rain** | Feeds the rain sensor — turn it on to watch weather protection close the louvres |
| **Simulated wind** | Feeds the wind speed sensor — raise it past the threshold to watch the screen retract |
| **Simulate presence** | Feeds the presence and motion sensors |

Illuminance and solar radiation follow the real solar position for your HA location, refreshed every five minutes. Only one demo network can be added, and it can be deleted like any other entry.

## Louvre intelligence

Each louvre device carries three automation layers, from lowest to highest priority — **sun < temperature < weather**:

### ☀️ Sun tracking

Turn on the **Sun tracking** switch and the louvre angle follows the sun (recomputed every 5 minutes from HA's built-in solar position): the slats stay perpendicular to the sun's rays, blocking direct sunlight while letting in maximum indirect light. Set the pergola's **orientation** in the options once. The **Sun offset** dial (−45°…+45°) shifts the result: positive = more sun on the terrace, negative = deeper shade. A ~3° deadband keeps the motor from twitching; nothing moves at night or when the sun is behind the pergola.

### 🌡️ Temperature control (Cool/Warm)

Pick a **temperature sensor** in the options, set the **Temperature setpoint** (15–30 °C), and turn on the **Temperature control** switch. While sun tracking runs, every °C above the setpoint tilts the slats 10° toward shade, every °C below tilts toward sun. Hot afternoon → louvres close down; cool morning → they open up for warmth.

### 🌧️💨 Weather protection

Appears as a network-level **Weather protection** switch when a Sensor Platform is in the network, or when a **rain sensor** or **wind sensor** is chosen in the options. The rain sensor can be any Home Assistant entity: a binary sensor (on = rain, e.g. a moisture sensor), a precipitation sensor (rain above 0, e.g. [Buienradar](https://www.home-assistant.io/integrations/buienradar/)'s precipitation in mm/h) or a weather entity (rainy, pouring, lightning-rainy, snowy-rainy or hail). The wind sensor is any wind speed sensor, e.g. a Netatmo anemometer; its unit is converted to km/h. Rain counts while either source reports it, the stronger wind reading is used, and an unavailable sensor keeps its last reading. While on:

- **Rain** → all louvres close into a sealed roof, and sun tracking pauses until the sensor reads dry again.
- **Wind ≥ threshold** (default 35 km/h) → all screens retract and all louvres close immediately — closed is the position in which the roof is rated for wind (Winsol SO!: wind class 3, 6 Beaufort, about 39–49 km/h) — and sun tracking pauses. Latched with hysteresis: tracking resumes once the wind drops below 80% of the threshold, but screens are **never re-extended automatically** — re-extend them yourself when the storm has passed.

All switch states and dials survive HA restarts.

### Sensor element enables

The sensor platform exposes writable enable bits for its elements; these appear as four config switches on the sensor device (**Wind/Rain/Light/Motion sensor enabled**). They gate the element's reporting *and* the network's own use of it — **do not disable the rain sensor**, as that also disables the pergola's built-in rain protection. Note that enabling an element only produces data if the hardware element is physically fitted.

## Wall switch buttons

Pressing any wall switch/remote button creates an `event` entity for that button ("Button 1", …) recording `press`, `release`, `hold`, `release_after_hold` — and simultaneously fires a bus event usable in automations:

```yaml
triggers:
  - trigger: event
    event_type: casambi_bt_button_event
    event_data:
      button: 2
      event_type: hold
actions:
  - action: cover.close_cover
    target:
      entity_id: cover.pergola_louvres
```

Watch events live under *Developer Tools → Events → `casambi_bt_button_event`*.

## Blueprints

Three automation blueprints ship with the integration, in
`blueprints/automation/casambi_bt/`. They need **Home Assistant 2026.7 or
newer** (they use the `event.received` trigger) and are imported via
*Settings → Automations & scenes → Blueprints → Import blueprint*, pasting
the file's GitHub URL.

| Blueprint | What it does |
|---|---|
| **Casambi button — custom actions** | Runs your own actions on press / short press / hold / release |
| **Casambi button — pergola cover** | Short press toggles a louvre or screen open/closed, holding stops it mid-travel |
| **Casambi button — toggle and dim** | Short press toggles a light, holding dims it until you let go |

All three pick the button from a filtered entity picker, so no unit IDs or
device IDs are involved. Note that a long press sends `press` first and
`hold` only afterwards — put toggles on **short press** (which fires on
release without a hold) if you want short and long to be exclusive.

## Example automations

Open the louvres to 40% every morning:

```yaml
triggers:
  - trigger: time
    at: "08:00:00"
actions:
  - action: cover.set_cover_position
    target:
      entity_id: cover.pergola_louvres
    data:
      position: 40
```

Notify when the wind retracts the screens:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.weather_station_wind_speed
conditions:
  - condition: numeric_state
    entity_id: sensor.weather_station_wind_speed
    above: 35
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Wind {{ states('sensor.weather_station_wind_speed') }} km/h — screens retracted."
```

## Connection handling

To notice when the network is back in range, the integration listens to its Bluetooth advertisements with **passive** scanning, which ESPHome proxies and most adapters support. On a local Linux adapter, BlueZ only offers passive scanning with its experimental D-Bus interfaces enabled (`Experimental = true` under `[General]` in `/etc/bluetooth/main.conf`); without it Home Assistant falls back to active scanning on its own.

Losing the BLE link is normal — the adapter gets busy, a proxy reboots, the pergola is at the far end of the garden. The integration reconnects on its own: a single background attempt retries with a delay that starts at 2 s and doubles up to 5 minutes, backs off to a minute while HA reports the network out of range, and stops to ask you for the password if it was changed. A command that fails on Bluetooth both reports the failure and triggers a reconnect, and all entities go unavailable (and come back) the moment the link changes, so the **Status** connectivity sensor always reflects reality.

## Diagnostics & debugging

- **Diagnostics download** (integration page → ⋮ → *Download diagnostics*): the full network schema — every unit with its control table (types, bit offsets, ranges), parsed state, raw state bytes, and live sensor cache. Password is redacted automatically.
- **Debug logging**:

  ```yaml
  logger:
    default: info
    logs:
      CasambiBt: debug
      custom_components.casambi_bt: debug
  ```

  Logs may contain the network password and account email — sanitize before sharing.

See `DEVELOPMENT.md` for the architecture, the decoded Winsol wire formats, and the protocol-capture workflow.

## Credits

- [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass) — the original integration and `casambi-bt` library this fork is based on.
- [superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass) — the library work (sensor platforms, switch events, raw control access) now merged into `casambi-bt`, and the reverse engineering of the Winsol fixtures and sensor packet semantics.
