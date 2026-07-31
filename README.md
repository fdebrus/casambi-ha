<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/casambi_bt/brand/dark_logo@2x.png">
    <img src="custom_components/casambi_bt/brand/logo@2x.png" alt="Casambi logo" width="300"/>
  </picture>
</p>

# Home Assistant integration for Casambi using Bluetooth

[![Discord](https://img.shields.io/discord/1186445089317326888)](https://discord.gg/jgZVugfx)

This is a Home Assistant integration for Casambi networks using Bluetooth. Since this is an unofficial implementation of the rather complex undocumented protocol used by the Casambi app there may be issues in networks configured differently to the one used to test this integration.
Please see the information below on how to report such issues.

A more mature HA integration for Casambi networks can be found under [https://github.com/hellqvio86/home_assistant_casambi](https://github.com/hellqvio86/home_assistant_casambi). This integration requires a network gateway to always connect the network to the Casambi cloud.

## Network configuration

See [https://github.com/lkempf/casambi-bt#casambi-network-setup](https://github.com/lkempf/casambi-bt#casambi-network-setup) for the proper network configuration. If you get "Unexcpected error" or "Failed to connect" different network configurations are the most common cause. Due to the high complexity of the protocol I won't be able to support all configurations allthough I might try if the suggested config doesn't work and the fix isn't to complex.

## Installation

### Manual

Place the `casambi_bt` folder in the `custom_components` folder.

### HACS

Add this repository as custom repository in the HACS store (HACS -> integrations -> custom repositories):

1. Setup HACS https://hacs.xyz/
2. Select HACS from the left sidebar
3. Search for `Casambi **Bluetooth**` in the searchbar at the top and select it. If you can't find it you might have to add this repository as a custom repository.
4. Click the Download button at the bottom right
5. Restart Home Assistant

## Features

Functionality exposed to HA:
- Lights
- Light groups
- Scenes
- Covers (units with a vertical control, e.g. pergola louvres — see below)
- Wall switch buttons (see below)

Supported control types:
- Dimmer
- White
- Rgb
- OnOff
- Temperature (Only for units since there are some open problems for groups.)
- Vertical (as a number entity, or optionally as a cover entity)

### Wall switches

Button presses on Casambi wall switches (e.g. Xpress) are exposed in two ways:

- **Event entities**: an `event` entity is created automatically the first time a button is used, named after the button number. Event types: `press`, `release`, `hold`, `release_after_hold`.
- **Bus events**: every button action also fires a `casambi_bt_button_event` on the Home Assistant event bus with `network_id`, `unit_id`, `button`, and `event_type` — usable directly in automation event triggers.

### Pergola louvres (e.g. Winsol So!)

Casambi-based pergolas such as the Winsol So! expose their louvre angle through the Casambi *vertical* control. By default this shows up in HA as a number entity. Enable **"Expose vertical controls as covers"** during setup (or later via the integration's *Configure* button) to get proper `cover` entities instead, with open/close buttons and position control (0 = closed, 100 = fully open). Cover entities work with HA dashboards, voice assistants, and automations much better than a raw number slider.

Not supported yet:
- Sensors
- Additional control types (e.g. slider, ...)
- Networks with classic firmware

## How data updates work

The integration connects to the Casambi network directly over Bluetooth and receives state changes as push updates — there is no polling and no cloud dependency during operation (the cloud is contacted once during setup to fetch the network configuration, which is then cached). If the Bluetooth connection drops, the integration reconnects automatically as soon as the network is in range again; a `Status` diagnostic sensor shows the connection state. If units were added or removed via the Casambi app in the meantime, the integration reloads itself so the entities match the network.

## Supported devices

Any unit that works with the Casambi app on a network with *Evolution* firmware should work, including:

- Casambi-ready luminaires and drivers (dimmer, RGB(W), tunable white, XY color)
- Casambi-based pergolas with louvres (e.g. Winsol So!) via the vertical control
- Casambi wall switches (e.g. Xpress) as event entities

## Example automations

Close the louvres when a wall switch button is held:

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
      entity_id: cover.pergola
```

Open the louvres to 40% every morning:

```yaml
triggers:
  - trigger: time
    at: "08:00:00"
actions:
  - action: cover.set_cover_position
    target:
      entity_id: cover.pergola
    data:
      position: 40
```

## Removing the integration

1. Go to **Settings → Devices & services → Casambi Bluetooth**.
2. Select the network entry, open the three-dot menu, and choose **Delete**.
3. If installed via HACS, remove the repository from HACS afterwards.

The cached network configuration in `.storage/casambi_bt` is kept so that re-adding the network is fast; it can be deleted manually if desired. Devices for units that were removed from the Casambi network can be deleted from the device page in HA.

## Reporting issues

Before reporting issues make sure that you have the debug log enabled for all relevant components. This can be done by placing the following in `configuration.yaml` of your HA installation:

```yaml
logger:
  default: info
  logs:
    CasambiBt: debug
    custom_components.casambi_bt: debug
```

The log might contain sensitive information about the network (including your network password and the email address used for the network) so sanitize it first or mail it to the address on my github profile referencing your issue.

## Development

When developing you might also want to change [https://github.com/lkempf/casambi-bt](casambi-bt). To make this more convenient run
```
pip install -e PATH_TO_CASAMBI_BT_REPO
```
in the homeassistant venv and then start HA with
```
hass -c config --skip-pip-packages casambi-bt
```

If you are unsure what these terms mean you might want to have a look at [https://developers.home-assistant.io/docs/development_environment](https://developers.home-assistant.io/docs/development_environment) first.
