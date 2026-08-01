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

## Requirements

- Home Assistant **2026.2 or newer** (the underlying library needs bleak ≥ 2.1)
- A Bluetooth adapter in range of the Casambi network — or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) with **active connections enabled** near the pergola
- A Casambi network on *Evolution* firmware (set up via the Casambi or Winsol app)

## Installation & setup

1. Add this repository as a custom repository in HACS, download, restart HA.
2. The network is **discovered automatically** when it advertises — accept the discovered entry, or add manually via *Settings → Devices & services → Add integration → Casambi Bluetooth*.
3. Only the **network password** needs to be typed; the Bluetooth address is pre-filled by discovery.

All later settings live behind the integration's **Configure** button:

| Option | Meaning |
|---|---|
| Import groups | Create entities for Casambi groups |
| Vertical controls as covers | For *light* fixtures with a vertical channel (not needed for Winsol motors) |
| Pergola orientation (azimuth) | Compass direction the pergola faces, for sun tracking (180 = south) |
| Wind threshold | Wind speed (km/h) at which weather protection retracts the screens |
| Temperature sensor | Any HA temperature sensor, used by the temperature control |

## What gets created

Units are classified automatically by their control layout (`classify.py`) — motors become covers, never lights:

| Hardware | Entities |
|---|---|
| Lights / LED (dimmer, RGB(W), tunable white, XY) | `light` entities, plus group lights |
| Casambi scenes | `scene` entities |
| **Winsol louvre motor** (Lamel) | `cover` (blind) with position 0–100% (= 0–142°) and **Stop**, plus the automation entities below |
| **Winsol SO! screen** | `cover` (shade) with position and Stop |
| **Sensor Platform V4** (weather option) | `sensor`: wind speed (km/h), solar radiation, illuminance (lx); `binary_sensor`: rain, motion, presence |
| Wall switches / remotes (e.g. Xpress) | `event` entities per button (created on first press) + bus events |
| Network | `binary_sensor` connectivity status |

## Louvre intelligence

Each louvre device carries three automation layers, from lowest to highest priority — **sun < temperature < weather**:

### ☀️ Sun tracking

Turn on the **Sun tracking** switch and the louvre angle follows the sun (recomputed every 5 minutes from HA's built-in solar position): the slats stay perpendicular to the sun's rays, blocking direct sunlight while letting in maximum indirect light. Set the pergola's **orientation** in the options once. The **Sun offset** dial (−45°…+45°) shifts the result: positive = more sun on the terrace, negative = deeper shade. A ~3° deadband keeps the motor from twitching; nothing moves at night or when the sun is behind the pergola.

### 🌡️ Temperature control (Cool/Warm)

Pick a **temperature sensor** in the options, set the **Temperature setpoint** (15–30 °C), and turn on the **Temperature control** switch. While sun tracking runs, every °C above the setpoint tilts the slats 10° toward shade, every °C below tilts toward sun. Hot afternoon → louvres close down; cool morning → they open up for warmth.

### 🌧️💨 Weather protection

Appears as a network-level **Weather protection** switch when a Sensor Platform is in the network. While on:

- **Rain** → all louvres close into a sealed roof, and sun tracking pauses until the sensor reads dry again.
- **Wind ≥ threshold** (default 35 km/h) → all screens retract immediately. Latched with hysteresis; screens are **never re-extended automatically** — re-extend them yourself when the storm has passed.

All switch states and dials survive HA restarts.

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
- [superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass) — the enhanced `casambi-bt-skk` library this fork uses, and the reverse engineering of the Winsol fixtures and sensor packet semantics.
