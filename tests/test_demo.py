"""Tests for the hardware-free demo mode."""

from unittest.mock import MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import CONF_DEMO, DOMAIN
from custom_components.casambi_bt.demo import DEMO_NETWORK_ID
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_SET_COVER_POSITION,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er


def _demo_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEMO_NETWORK_ID,
        title="Casambi Demo",
        data={CONF_DEMO: True},
    )


async def _setup_demo(hass: HomeAssistant) -> MockConfigEntry:
    entry = _demo_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"missing entity for {unique_id}"
    return entity_id


async def test_demo_config_flow(hass: HomeAssistant) -> None:
    """Test that the demo can be set up from the menu without hardware."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"network", "demo"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "demo"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEMO] is True

    # Only one demo network can be set up.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "demo"}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_demo_creates_pergola_entities(hass: HomeAssistant) -> None:
    """Test that the demo network exposes a full pergola."""
    await _setup_demo(hass)

    louvre = _entity_id(hass, COVER_DOMAIN, f"{DEMO_NETWORK_ID}-unit-demo-unit-1-cover")
    screen = _entity_id(hass, COVER_DOMAIN, f"{DEMO_NETWORK_ID}-unit-demo-unit-2-cover")
    assert hass.states.get(louvre) is not None
    assert hass.states.get(screen) is not None

    sensors = f"{DEMO_NETWORK_ID}-unit-demo-unit-3"
    for domain, key in (
        ("sensor", "wind"),
        ("sensor", "solar"),
        ("sensor", "illuminance"),
        ("binary_sensor", "rain"),
        ("binary_sensor", "motion"),
        ("binary_sensor", "presence"),
        ("switch", "demo-rain"),
        ("switch", "demo-presence"),
        ("number", "demo-wind"),
    ):
        state = hass.states.get(_entity_id(hass, domain, f"{sensors}-{key}"))
        assert state is not None
        assert state.state != "unavailable"

    # Lights, a group, scenes and the louvre automations exist too.
    assert hass.states.get(
        _entity_id(hass, "light", f"{DEMO_NETWORK_ID}-unit-demo-unit-4-light")
    )
    assert hass.states.get(_entity_id(hass, "light", f"{DEMO_NETWORK_ID}-group-1"))
    assert hass.states.get(_entity_id(hass, "scene", f"{DEMO_NETWORK_ID}-scene-1"))
    assert hass.states.get(
        _entity_id(hass, "switch", f"{DEMO_NETWORK_ID}-unit-demo-unit-1-sun-tracking")
    )


async def test_demo_cover_moves(hass: HomeAssistant) -> None:
    """Test that commands change the simulated state."""
    await _setup_demo(hass)
    entity_id = _entity_id(
        hass, COVER_DOMAIN, f"{DEMO_NETWORK_ID}-unit-demo-unit-1-cover"
    )

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: entity_id, ATTR_POSITION: 20},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_CURRENT_POSITION] == 20


async def test_demo_weather_protection_end_to_end(hass: HomeAssistant) -> None:
    """Test that simulated rain closes the louvres via weather protection."""
    await _setup_demo(hass)

    louvre = _entity_id(hass, COVER_DOMAIN, f"{DEMO_NETWORK_ID}-unit-demo-unit-1-cover")
    protection = _entity_id(hass, "switch", f"{DEMO_NETWORK_ID}-weather-protection")
    rain = _entity_id(hass, "switch", f"{DEMO_NETWORK_ID}-unit-demo-unit-3-demo-rain")

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: protection}, blocking=True
    )
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: louvre, ATTR_POSITION: 80},
        blocking=True,
    )
    await hass.async_block_till_done()

    # It starts raining: the louvres close into a sealed roof.
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: rain}, blocking=True
    )
    await hass.async_block_till_done()

    assert (
        hass.states.get(
            _entity_id(
                hass, "binary_sensor", f"{DEMO_NETWORK_ID}-unit-demo-unit-3-rain"
            )
        ).state
        == "on"
    )
    state = hass.states.get(louvre)
    assert state is not None
    assert state.attributes[ATTR_CURRENT_POSITION] == 0


async def test_demo_sun_tracking(hass: HomeAssistant) -> None:
    """Test that sun tracking positions the simulated louvres."""
    await _setup_demo(hass)

    louvre = _entity_id(hass, COVER_DOMAIN, f"{DEMO_NETWORK_ID}-unit-demo-unit-1-cover")
    tracking = _entity_id(
        hass, "switch", f"{DEMO_NETWORK_ID}-unit-demo-unit-1-sun-tracking"
    )

    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: tracking}, blocking=True
        )
        await hass.async_block_till_done()

    state = hass.states.get(louvre)
    assert state is not None
    # 45 degrees of 142 -> 32%
    assert state.attributes[ATTR_CURRENT_POSITION] == 32


async def test_demo_needs_no_bluetooth(hass: HomeAssistant) -> None:
    """Test that setting up the demo never touches the bluetooth stack."""
    with patch(
        "homeassistant.components.bluetooth.async_ble_device_from_address"
    ) as ble_device:
        await _setup_demo(hass)

    ble_device.assert_not_called()


async def test_demo_wind_drives_sensor(hass: HomeAssistant) -> None:
    """Test that the simulated wind control feeds the wind sensor."""
    await _setup_demo(hass)

    wind_number = _entity_id(
        hass, "number", f"{DEMO_NETWORK_ID}-unit-demo-unit-3-demo-wind"
    )
    wind_sensor = _entity_id(hass, "sensor", f"{DEMO_NETWORK_ID}-unit-demo-unit-3-wind")

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: wind_number, "value": 42},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(wind_sensor)
    assert state is not None
    assert float(state.state) == 42.0


async def test_demo_unload(hass: HomeAssistant, mock_casambi: MagicMock) -> None:
    """Test that a demo entry unloads cleanly."""
    entry = await _setup_demo(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
