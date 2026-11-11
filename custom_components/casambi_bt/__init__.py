"""The Casambi Bluetooth integration.

``hub`` holds the Bluetooth link, ``motor`` follows each louvre and screen
and ``pergola`` runs the automations on them; the platforms only show and
command these.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from . import switch_decoder
from .classify import UnitKind
from .const import DOMAIN, PLATFORMS
from .hub import CasambiHub
from .models import CasambiConfigEntry, CasambiData
from .motor import MOTOR_KINDS, Motor
from .pergola import PergolaController

__all__ = ["CasambiConfigEntry", "CasambiData"]

_LOGGER: Final = logging.getLogger(__name__)

# Wired push-button interfaces only report on the input stream, which the
# library's decoder drops for presses and releases.
switch_decoder.install()


async def async_setup_entry(hass: HomeAssistant, entry: CasambiConfigEntry) -> bool:
    """Set up a Casambi network from a config entry."""
    hub = CasambiHub(hass, entry)
    await hub.async_connect()

    motors = {
        unit.uuid: Motor(hass, hub, unit, hub.kind(unit))
        for unit in hub.get_units()
        if hub.kind(unit) in MOTOR_KINDS
    }
    pergola = PergolaController(hass, entry, hub, motors)
    entry.runtime_data = CasambiData(hub=hub, pergola=pergola, motors=motors)

    async def _stop(_event: Event) -> None:
        await hub.async_shut_down()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop))

    # The network device comes before the platforms so that the unit
    # devices can link to it by id.
    network_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub.network_id)},
        connections={(dr.CONNECTION_BLUETOOTH, hub.address)},
        manufacturer="Casambi",
        model="Network",
        name=hub.network_name,
    )
    hub.network_device_id = network_device.id

    _flag_unsupported_motors(hass, entry, hub)

    for motor in motors.values():
        motor.async_start()
    pergola.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Option and reconfigure changes reload the entry from the flows
    # (OptionsFlowWithReload, async_update_reload_and_abort), which Home
    # Assistant refuses alongside an update listener.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CasambiConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Entities are still up: keep the connection they rely on.
        return False

    data = entry.runtime_data
    data.pergola.async_stop()
    for motor in data.motors.values():
        motor.async_stop()
    await data.hub.async_disconnect()
    _clear_unsupported_motors(hass, entry)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: CasambiConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removal of devices for units no longer in the network."""
    hub = entry.runtime_data.hub
    current = {(DOMAIN, unit.uuid) for unit in hub.get_units()}
    current.add((DOMAIN, hub.network_id))
    return not any(identifier in current for identifier in device_entry.identifiers)


def _unsupported_issue_prefix(entry: CasambiConfigEntry) -> str:
    return f"unsupported_motor_{entry.entry_id}_"


@callback
def _flag_unsupported_motors(
    hass: HomeAssistant, entry: CasambiConfigEntry, hub: CasambiHub
) -> None:
    """Raise a repair issue for each motor this integration must not drive."""
    for unit in hub.get_units():
        if hub.kind(unit) is not UnitKind.UNSUPPORTED_MOTOR:
            continue
        _LOGGER.warning(
            "%s (%s) is a motor with an untested control layout; "
            "no entities are created for it",
            unit.name,
            unit.unitType.model,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{_unsupported_issue_prefix(entry)}{unit.uuid}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unsupported_motor",
            translation_placeholders={
                "name": unit.name,
                "model": unit.unitType.model,
            },
        )


@callback
def _clear_unsupported_motors(hass: HomeAssistant, entry: CasambiConfigEntry) -> None:
    """Remove this entry's unsupported-motor issues."""
    prefix = _unsupported_issue_prefix(entry)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
