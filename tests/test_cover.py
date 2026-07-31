"""Tests for the Casambi Bluetooth cover platform."""

from unittest.mock import MagicMock

from CasambiBt import Group, Unit
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import (
    CONF_IMPORT_GROUPS,
    CONF_VERTICAL_AS_COVER,
    DOMAIN,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import LOUVRE_UUID, NETWORK_ADDRESS, NETWORK_ID


@pytest.fixture
def cover_config_entry() -> MockConfigEntry:
    """Create a config entry with cover mode enabled."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=NETWORK_ADDRESS,
        title="Test Network",
        data={
            CONF_ADDRESS: NETWORK_ADDRESS,
            CONF_PASSWORD: "password",
            CONF_IMPORT_GROUPS: True,
            CONF_VERTICAL_AS_COVER: True,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _unit_cover_entity_id(hass: HomeAssistant) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        COVER_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LOUVRE_UUID}-cover"
    )
    assert entity_id is not None
    return entity_id


async def test_cover_entities_created(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    cover_config_entry: MockConfigEntry,
) -> None:
    """Test that unit and group covers are created and numbers suppressed."""
    await _setup(hass, cover_config_entry)

    registry = er.async_get(hass)

    unit_entity = registry.async_get_entity_id(
        COVER_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LOUVRE_UUID}-cover"
    )
    assert unit_entity is not None

    group_entity = registry.async_get_entity_id(
        COVER_DOMAIN, DOMAIN, f"{NETWORK_ID}-group-1"
    )
    assert group_entity is not None

    # The vertical number entities must not be created in cover mode.
    assert (
        registry.async_get_entity_id(
            "number", DOMAIN, f"{NETWORK_ID}-unit-{LOUVRE_UUID}-vertical"
        )
        is None
    )


async def test_cover_position_state(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    cover_config_entry: MockConfigEntry,
) -> None:
    """Test that the cover reports the position from the vertical state."""
    await _setup(hass, cover_config_entry)

    state = hass.states.get(_unit_cover_entity_id(hass))
    assert state is not None
    assert state.state == "open"
    # vertical 128 of 255 -> 50%
    assert state.attributes[ATTR_CURRENT_POSITION] == 50


@pytest.mark.parametrize(
    ("service", "data", "expected_vertical"),
    [
        (SERVICE_OPEN_COVER, {}, 255),
        (SERVICE_CLOSE_COVER, {}, 0),
        (SERVICE_SET_COVER_POSITION, {ATTR_POSITION: 40}, 102),
    ],
)
async def test_cover_services(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    cover_config_entry: MockConfigEntry,
    service: str,
    data: dict,
    expected_vertical: int,
) -> None:
    """Test that cover services send the right vertical values."""
    await _setup(hass, cover_config_entry)
    entity_id = _unit_cover_entity_id(hass)

    await hass.services.async_call(
        COVER_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id, **data},
        blocking=True,
    )

    mock_casambi.setVertical.assert_awaited_once()
    target, vertical = mock_casambi.setVertical.await_args.args
    assert isinstance(target, Unit)
    assert target.uuid == LOUVRE_UUID
    assert vertical == expected_vertical


async def test_group_cover_targets_group(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    cover_config_entry: MockConfigEntry,
) -> None:
    """Test that the group cover sends commands to the Casambi group."""
    await _setup(hass, cover_config_entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        COVER_DOMAIN, DOMAIN, f"{NETWORK_ID}-group-1"
    )
    assert entity_id is not None

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_OPEN_COVER,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    target, vertical = mock_casambi.setVertical.await_args.args
    assert isinstance(target, Group)
    assert vertical == 255
