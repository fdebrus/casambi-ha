"""Diagnostics support for the Casambi Bluetooth integration."""

from __future__ import annotations

from typing import Any

from CasambiBt import UnitControl, UnitState

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import CasambiConfigEntry

TO_REDACT = {CONF_PASSWORD}


def _control_details(control: UnitControl) -> dict[str, Any]:
    """Describe a unit control including its raw wire layout.

    The bit offset and length describe where the control lives in the
    packed state blob, which is what protocol decoding needs.
    """
    return {
        "type": control.type.name,
        "type_raw": control.type.value,
        "offset": control.offset,
        "length": control.length,
        "default": control.default,
        "readonly": control.readonly,
        "min": control.min,
        "max": control.max,
    }


def _state_details(state: UnitState | None) -> dict[str, Any] | None:
    """Return every parsed state field of a unit."""
    if state is None:
        return None
    return {
        "dimmer": state.dimmer,
        "vertical": state.vertical,
        "slider": state.slider,
        "rgb": state.rgb,
        "hs": state.hs,
        "white": state.white,
        "temperature": state.temperature,
        "colorsource": state.colorsource.name if state.colorsource else None,
        "xy": state.xy,
        "onoff": state.onoff,
        "presence": state.presence,
        "lux": state.lux,
        "raw_state": state.raw_state.hex() if state.raw_state else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CasambiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    casa = entry.runtime_data.casa

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "network": {
            "id": casa.networkId,
            "name": casa.networkName,
            "connected": casa.connected,
        },
        "units": [
            {
                "device_id": unit.deviceId,
                "uuid": unit.uuid,
                "name": unit.name,
                "online": unit.online,
                "on": unit.is_on,
                "firmware": unit.firmwareVersion,
                "manufacturer": unit.unitType.manufacturer,
                "model": unit.unitType.model,
                "unit_type": {
                    "id": unit.unitType.id,
                    "mode": unit.unitType.mode,
                    "state_length": unit.unitType.stateLength,
                },
                "controls": [
                    _control_details(control) for control in unit.unitType.controls
                ],
                "state": _state_details(unit.state),
                "sensor_cache": dict(unit.sensor_cache),
            }
            for unit in casa.units
        ],
        "groups": [
            {
                "id": group.groudId,
                "name": group.name,
                "units": [unit.deviceId for unit in group.units],
            }
            for group in casa.groups
        ],
        "scenes": [{"id": scene.sceneId, "name": scene.name} for scene in casa.scenes],
    }
