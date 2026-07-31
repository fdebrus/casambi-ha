"""Diagnostics support for the Casambi Bluetooth integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import CasambiConfigEntry

TO_REDACT = {CONF_PASSWORD}


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
                "firmware": unit.firmwareVersion,
                "manufacturer": unit.unitType.manufacturer,
                "model": unit.unitType.model,
                "controls": [control.type.name for control in unit.unitType.controls],
                "state": repr(unit.state),
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
