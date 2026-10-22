<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/casambi_bt/brand/dark_logo@2x.png">
    <img src="custom_components/casambi_bt/brand/logo@2x.png" alt="Casambi logo" width="300"/>
  </picture>
</p>

# Casambi Bluetooth for Home Assistant — Winsol pergola fork

> [!WARNING]
> **Personal, experimental fork — please don't use it.**
> It is based on [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass) and tuned for one Winsol SO! pergola. It can break or change at any time and comes with no support.
>
> 👉 **Use the original project instead: [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass)**

The rest of this page documents the fork for my own reference.

## Tested hardware

> [!CAUTION]
> **This fork does not work with every Winsol pergola.** It was built on a single Winsol SO! Climate (Cosy pack). Other packs use other Casambi fixtures, and a motor whose controls sit elsewhere in its state would be driven wrong.

| Unit | Status |
|---|---|
| Winsol Lamel Standard V4.1 TA16 (louvres) | ✅ Tested |
| Winsol SO! V4.1 (screens) | ✅ Tested |
| Winsol On-Off (relay, pergola heating) | ✅ Tested |
| Winsol Mono (dimmable spots) | ✅ Tested |
| 24VDC_4CH_PB_Winsol (wall buttons) | ✅ Tested |
| Sensor Platform V4 (weather option) | ⚠️ Written from the fixture definition, never run on hardware |
| Winsol Lamel Intelligent (Star pack) | ❌ Not supported — skipped, with a warning under *Settings → System → Repairs*. [superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass) supports it |
| Anything else | ❓ Unknown — compare with the diagnostics download, which lists every unit's control layout |

## Requirements

- Home Assistant **2026.6 or newer** (the `casambi-bt` library needs bleak ≥ 3.0).
- A Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) **with active connections** in range of the pergola.
- A Casambi network on *Evolution* firmware (set up with the Casambi or Winsol app).

## Installation

1. Add this repository as a custom repository in HACS, download it and restart HA.
2. Accept the discovered **Casambi Bluetooth** entry (or add it under *Settings → Devices & services*) and type the network password. The form also asks whether to import groups and whether to show vertical controls as covers; both can be changed later in the options.

To remove it, delete the entry under *Settings → Devices & services* (its devices and entities go with it) and uninstall the repository in HACS. The network cache under `.storage/casambi_bt` can be deleted by hand.

## Options

Everything else lives behind the integration's **Configure** button:

| Option | Used for |
|---|---|
| Import groups | Casambi groups as group lights |
| Vertical controls as covers | Light fixtures with a vertical channel (not needed for Winsol motors) |
| Pergola orientation | Sun tracking — compass direction the pergola faces (180 = south) |
| Temperature sensor | Temperature control and frost lock |
| Frost lock temperature | Frost lock — below this the louvres can't be opened (default 2 °C) |
| Rain sensor | Weather protection — binary sensor, precipitation sensor or weather entity |
| Wind sensor | Weather protection — any wind speed sensor, converted to km/h |
| Wind threshold | Weather protection — speed that retracts the screens and closes the louvres (default 35 km/h) |

## Entities

Units are classified by their control layout (`classify.py`): motors become covers, bare on/off relays become switches.

| Hardware | Entities |
|---|---|
| Lights (dimmer, RGB(W), tunable white, XY) | `light`, plus group lights |
| Casambi scenes | `scene` |
| Winsol louvres | `cover` (0–100 % = 0–142°) with stop; the automation switches and dials below; a **temperature** sensor and, as diagnostics, overcurrent events, travel distance and module state |
| Winsol SO! screens | `cover` with position and stop |
| Winsol On-Off relay | `switch` |
| Sensor Platform V4 | wind, solar radiation and illuminance sensors; rain, motion and presence binary sensors; one enable switch per element — **leave rain enabled**, it also drives the pergola's own rain protection |
| Wall buttons and remotes | one `event` entity per button, created on its first press |
| Network | **Status** connectivity sensor, **Weather protection** switch |

### How the motors behave

- **Like the wall button.** Winsol motors have the start/stop bit the wall button toggles: setting it runs the motor opposite its last run, clearing it stops it at once. *Open* and *close* use that bit whenever it sends the motor the requested way, so *stop* is instant. Otherwise they send a position; a screen moving that way is stopped with a target about 1.5 s ahead of it, louvres are left to finish. *Set position* always sends a position.
- **Screens are inverted.** Casambi counts how far a screen is *extended*; Home Assistant counts how far it is *open*. Open retracts the screen, close extends it.
- **Screen position is estimated.** A screen reports its target, not where it is. While it moves (from HA, the app or the button) HA shows *opening*/*closing* with a position estimated from its 35 s travel time, refreshed every second.

## Pergola automation

Each louvre has a set of switches and dials; together they work in this order — **frost lock > weather protection > temperature control > sun tracking**. Every state survives a restart.

**☀️ Sun tracking** — keeps the slats perpendicular to the sun, recomputed every 5 minutes from HA's solar position, so they block direct sun but let in indirect light. The **Sun offset** dial (−45°…+45°) shifts the angle toward more sun (+) or more shade (−). It ignores changes under ~3° and does nothing at night or when the sun is behind the pergola. A manual command is overridden at the next update, so turn tracking off to keep a position.

**🌡️ Temperature control** — while tracking runs, each °C above the **Temperature setpoint** (15–30 °C) tilts the slats 10° toward shade, each °C below 10° toward sun.

**🌧️💨 Weather protection** — a network switch, present when a Sensor Platform or a rain/wind sensor is configured.
- **Rain** closes the louvres and pauses tracking until it is dry.
- **Wind** at the threshold retracts the screens, closes the louvres (the position the roof is rated for: Winsol SO! wind class 3, 6 Beaufort) and pauses tracking until it drops below 80 % of the threshold. Screens are never lowered again automatically.
- It acts once when rain or wind starts. A command that fails (the link was down during a gust) is sent again on the next reading and as soon as the pergola is reachable. Unavailable sensors keep their last reading.

**🧊 Frost lock** — on by default. Below the frost lock temperature, Home Assistant refuses to open the louvres (from the dashboard, Siri/HomeKit or an automation) with an error saying why, and tracking pauses. Closing and stopping still work. The wall button and the Casambi app are not affected. Watch the **Overcurrent events** sensor: it counts the motor forcing on stuck slats.

## Wall buttons

Each press creates or updates the button's `event` entity (`press`, `release`, `hold`, `release_after_hold`; anything else the library reports becomes `unknown`) and fires a `casambi_bt_button_event` bus event:

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id: event.24vdc_4ch_pb_winsol_button_2
    options:
      event_type:
        - hold
actions:
  - action: cover.close_cover
    target:
      entity_id: cover.winsol_louvres
```

Wired button modules only report on the input stream; some release frames get lost in the mesh.

## Connection

- The integration reconnects on its own: retries start after 2 s and back off to 5 minutes (1 minute while no adapter sees the network), and it asks for the password again if it changed.
- The **Status** sensor goes off the moment the link drops, and all unit entities go unavailable with it.
- After 5 minutes unreachable a warning in the log names the reason (e.g. *no Bluetooth adapter or proxy in range sees it*), and another one says when it is back. A Bluetooth proxy that went offline is the usual cause.
- Reconnect detection listens to the network's advertisements with **passive** scanning. A local Linux adapter only supports that with BlueZ's experimental interfaces (`Experimental = true` under `[General]` in `/etc/bluetooth/main.conf`); otherwise HA falls back to active scanning.

## Debugging

- **Diagnostics** (integration page → ⋮ → *Download diagnostics*): every unit with its control table, parsed and raw state, and sensor cache. The password is redacted.
- **Debug logging:**

  ```yaml
  logger:
    logs:
      CasambiBt: debug
      custom_components.casambi_bt: debug
  ```

  Debug logs can contain the network password and account email — clean them before sharing.

[DEVELOPMENT.md](DEVELOPMENT.md) covers the architecture, the decoded Winsol wire formats and the protocol capture workflow.

## Credits

- [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass) — the original integration and the `casambi-bt` library.
- [superkikim/casambi-bt-hass](https://github.com/superkikim/casambi-bt-hass) — sensor platforms, switch events and raw control access (now in `casambi-bt`), and the first decoding of the Winsol fixtures.
