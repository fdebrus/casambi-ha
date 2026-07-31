"""Fixtures for the Casambi Bluetooth integration tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from CasambiBt import (
    Casambi,
    Group,
    Scene,
    Unit,
    UnitControl,
    UnitControlType,
    UnitState,
    UnitType,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import (
    CONF_IMPORT_GROUPS,
    CONF_VERTICAL_AS_COVER,
    DOMAIN,
)
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD

NETWORK_ADDRESS = "AA:BB:CC:DD:EE:FF"
NETWORK_ID = "abcdef123456"
NETWORK_NAME = "Test Network"

LIGHT_UUID = "unit-light-uuid"
LOUVRE_UUID = "unit-louvre-uuid"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""
    return


def _make_unit(
    type_id: int,
    device_id: int,
    uuid: str,
    name: str,
    controls: list[UnitControl],
    state: UnitState | None,
) -> Unit:
    """Create a CasambiBt unit for testing."""
    unit_type = UnitType(
        id=type_id,
        model="Test model",
        manufacturer="Casambi",
        mode="",
        stateLength=2,
        controls=controls,
    )
    unit = Unit(
        _typeId=type_id,
        deviceId=device_id,
        uuid=uuid,
        address="11:22:33:44:55:66",
        name=name,
        firmwareVersion="1.0",
        unitType=unit_type,
    )
    unit._state = state  # noqa: SLF001
    unit._on = True  # noqa: SLF001
    unit._online = True  # noqa: SLF001
    return unit


def make_light_unit() -> Unit:
    """Create a dimmable light unit."""
    state = UnitState()
    state.dimmer = 128
    return _make_unit(
        1,
        1,
        LIGHT_UUID,
        "Test Light",
        [UnitControl(UnitControlType.DIMMER, 0, 8, 0, False)],
        state,
    )


def make_louvre_unit() -> Unit:
    """Create a louvre unit with a vertical control."""
    state = UnitState()
    state.vertical = 128
    return _make_unit(
        2,
        2,
        LOUVRE_UUID,
        "Test Louvre",
        [UnitControl(UnitControlType.VERTICAL, 0, 8, 0, False)],
        state,
    )


@pytest.fixture
def mock_casambi() -> Generator[MagicMock]:
    """Mock the Casambi library for the integration and the config flow."""
    casa = MagicMock(spec=Casambi)
    casa.connect = AsyncMock()
    casa.disconnect = AsyncMock()
    casa.invalidateCache = AsyncMock()
    casa.setLevel = AsyncMock()
    casa.setVertical = AsyncMock()
    casa.setUnitState = AsyncMock()
    casa.setColor = AsyncMock()
    casa.setWhite = AsyncMock()
    casa.turnOn = AsyncMock()
    casa.switchToScene = AsyncMock()
    casa.connected = True
    casa.networkId = NETWORK_ID
    casa.networkName = NETWORK_NAME

    light = make_light_unit()
    louvre = make_louvre_unit()
    casa.units = [light, louvre]
    casa.groups = [Group(1, "Pergola", [light, louvre])]
    casa.scenes = [Scene(1, "Evening")]

    with (
        patch("custom_components.casambi_bt.Casambi", return_value=casa),
        patch("custom_components.casambi_bt.config_flow.Casambi", return_value=casa),
    ):
        yield casa


@pytest.fixture
def mock_bluetooth() -> Generator[MagicMock]:
    """Mock the bluetooth helpers used by the integration."""
    device = MagicMock()
    device.address = NETWORK_ADDRESS

    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=device,
        ),
        patch(
            "custom_components.casambi_bt.config_flow.async_ble_device_from_address",
            return_value=device,
        ),
        patch(
            "custom_components.casambi_bt.config_flow.async_scanner_count",
            return_value=1,
        ),
        patch(
            "homeassistant.components.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
    ):
        yield device


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a config entry for the integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=NETWORK_ADDRESS,
        title=NETWORK_NAME,
        data={
            CONF_ADDRESS: NETWORK_ADDRESS,
            CONF_PASSWORD: "password",
            CONF_IMPORT_GROUPS: True,
            CONF_VERTICAL_AS_COVER: False,
        },
    )
