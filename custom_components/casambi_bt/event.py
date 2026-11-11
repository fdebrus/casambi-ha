"""Events: the buttons of Casambi wall switches."""

from __future__ import annotations

import logging

from CasambiBt._switch import SwitchEvent

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import CasambiEntity, network_device_info, unit_device_info
from .hub import CasambiHub
from .models import CasambiConfigEntry

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

EVENT_TYPES = ["press", "release", "hold", "release_after_hold", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the button event entities.

    Which buttons exist can't be read from the network, so entities are
    created when a button is used for the first time and recreated from
    the entity registry on later starts.
    """
    hub = entry.runtime_data.hub
    entities: dict[tuple[int, int], CasambiButtonEventEntity] = {}

    def _add_button(
        unit_id: int, button: int, pending: SwitchEvent | None = None
    ) -> None:
        # The pending event is set before adding: the add may run eagerly
        # and process async_added_to_hass at once.
        entity = CasambiButtonEventEntity(hub, unit_id, button, pending)
        entities[(unit_id, button)] = entity
        async_add_entities([entity])

    # Recreate the entities discovered in earlier sessions.
    registry = er.async_get(hass)
    prefix = f"{hub.network_id}-switch-"
    for known in er.async_entries_for_config_entry(registry, entry.entry_id):
        if known.domain != "event" or not known.unique_id.startswith(prefix):
            continue
        try:
            unit_id, button = (
                int(part) for part in known.unique_id.removeprefix(prefix).split("-")
            )
        except ValueError:
            continue
        if (unit_id, button) not in entities:
            _add_button(unit_id, button)

    @callback
    def _handle_button_event(event: SwitchEvent) -> None:
        key = (event.unit_id, event.button)
        if key not in entities:
            _LOGGER.debug(
                "Discovered new switch button: unit %i button %i",
                event.unit_id,
                event.button,
            )
            _add_button(*key, pending=event)
            return
        entities[key].handle_event(event)

    entry.async_on_unload(hub.subscribe_buttons(_handle_button_event))


class CasambiButtonEventEntity(CasambiEntity, EventEntity):
    """One button of a Casambi wall switch."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"

    def __init__(
        self,
        hub: CasambiHub,
        unit_id: int,
        button: int,
        pending: SwitchEvent | None = None,
    ) -> None:
        """Initialize the event entity of a button.

        :param pending: The event that made the button known, to show once
            the entity is added.
        """
        super().__init__(hub)
        self._pending = pending
        # False while Home Assistant has not added the entity: it refuses
        # one disabled in the registry, which then must not write state.
        self._added = False
        self._attr_unique_id = f"{hub.network_id}-switch-{unit_id}-{button}"
        self._attr_translation_placeholders = {"button": str(button)}
        # The device of the switch unit if it is known, else the network.
        unit = next((u for u in hub.get_units() if u.deviceId == unit_id), None)
        self._attr_device_info = (
            unit_device_info(hub, unit) if unit else network_device_info(hub)
        )

    async def async_added_to_hass(self) -> None:
        """Show the event that made the button known."""
        await super().async_added_to_hass()
        self._added = True
        if self._pending is not None:
            self.handle_event(self._pending)
            self._pending = None

    async def async_will_remove_from_hass(self) -> None:
        """Stop showing events."""
        self._added = False
        await super().async_will_remove_from_hass()

    @callback
    def handle_event(self, event: SwitchEvent) -> None:
        """Show an event of this button."""
        if not self._added:
            return
        self._trigger_event(event.event.name.lower())
        self.async_write_ha_state()
