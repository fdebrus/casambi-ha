"""Tests for the Casambi Bluetooth event platform (wall switch buttons)."""

from collections.abc import Callable
from unittest.mock import MagicMock

from CasambiBt._switch import ButtonEventType, SwitchEvent
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.casambi_bt.const import DOMAIN, EVENT_BUTTON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import LIGHT_UUID, NETWORK_ID, get_device


def _make_switch_event(
    button: int, unit_id: int, event: ButtonEventType
) -> SwitchEvent:
    return SwitchEvent(
        button_event_index=button - 1,
        button=button,
        unit_id=unit_id,
        target_type=0x06,
        event=event,
        flags=0,
        extra_data=b"",
    )


def _get_switch_handler(mock_casambi: MagicMock) -> Callable[[SwitchEvent], None]:
    mock_casambi.registerSwitchEventHandler.assert_called_once()
    return mock_casambi.registerSwitchEventHandler.call_args.args[0]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_button_event_fires_bus_event(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a switch event fires a bus event with the right data."""
    await _setup(hass, mock_config_entry)

    events = async_capture_events(hass, EVENT_BUTTON)
    handler = _get_switch_handler(mock_casambi)
    handler(_make_switch_event(1, 42, ButtonEventType.PRESS))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "network_id": NETWORK_ID,
        "unit_id": 42,
        "button": 1,
        "event_type": "press",
    }


async def test_button_event_creates_entity(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that an event entity is created on first use and updates after."""
    await _setup(hass, mock_config_entry)

    handler = _get_switch_handler(mock_casambi)
    handler(_make_switch_event(2, 42, ButtonEventType.PRESS))
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "event", DOMAIN, f"{NETWORK_ID}-switch-42-2"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "press"

    # A second event on the same button reuses the entity.
    handler(_make_switch_event(2, 42, ButtonEventType.HOLD))
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "hold"


async def test_button_entity_device_links_to_network(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a button of a known unit lands on that unit's device."""
    await _setup(hass, mock_config_entry)

    # Unit 1 is the test light; unit 42 is unknown to the network.
    handler = _get_switch_handler(mock_casambi)
    handler(_make_switch_event(1, 1, ButtonEventType.PRESS))
    handler(_make_switch_event(1, 42, ButtonEventType.PRESS))
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    entry_id = mock_config_entry.entry_id
    network_device = get_device(devices, (DOMAIN, NETWORK_ID), entry_id)
    assert network_device is not None

    known = registry.async_get(
        registry.async_get_entity_id("event", DOMAIN, f"{NETWORK_ID}-switch-1-1") or ""
    )
    assert known is not None and known.device_id is not None
    unit_device = devices.async_get(known.device_id)
    assert unit_device is not None
    assert (DOMAIN, LIGHT_UUID) in unit_device.identifiers
    assert unit_device.via_device_id == network_device.id

    unknown = registry.async_get(
        registry.async_get_entity_id("event", DOMAIN, f"{NETWORK_ID}-switch-42-1") or ""
    )
    assert unknown is not None
    assert unknown.device_id == network_device.id

    assert "deprecated `via_device`" not in caplog.text


async def test_button_entity_restored_from_registry(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that previously discovered buttons are recreated on startup."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "event",
        DOMAIN,
        f"{NETWORK_ID}-switch-7-3",
        config_entry=mock_config_entry,
    )

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = registry.async_get_entity_id(
        "event", DOMAIN, f"{NETWORK_ID}-switch-7-3"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None


async def test_button_entity_follows_the_connection(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a button entity goes unavailable when the link drops."""
    await _setup(hass, mock_config_entry)
    api = mock_config_entry.runtime_data
    handler = _get_switch_handler(mock_casambi)
    handler(_make_switch_event(1, 42, ButtonEventType.PRESS))
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{NETWORK_ID}-switch-42-1"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state != "unavailable"

    mock_casambi.connected = False
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"

    mock_casambi.connected = True
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unavailable"
