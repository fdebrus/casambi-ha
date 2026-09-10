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
WINSOL_LOUVRE_UUID = "unit-winsol-louvre-uuid"
WINSOL_SCREEN_UUID = "unit-winsol-screen-uuid"
SENSOR_PLATFORM_UUID = "unit-sensor-platform-uuid"


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
    mode: str = "",
    model: str = "Test model",
    state_length: int = 2,
) -> Unit:
    """Create a CasambiBt unit for testing."""
    unit_type = UnitType(
        id=type_id,
        model=model,
        manufacturer="Casambi",
        mode=mode,
        stateLength=state_length,
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


def make_winsol_louvre_unit() -> Unit:
    """Create a unit modeled on the Winsol Lamel Standard V4.1 fixture.

    Control layout from the real fixture definition: multiplexed sensors
    (bits 0-27), slider $pos (bits 28-35, 0-142 degrees), and onoff
    $startstop (bit 36).
    """
    state = UnitState()
    state.slider = 128
    unit = _make_unit(
        38915,
        10,
        WINSOL_LOUVRE_UUID,
        "Pergola Louvres",
        [
            UnitControl(UnitControlType.UNKNOWN, 0, 4, 0, True),
            UnitControl(UnitControlType.SENSOR, 4, 0, 0, True),
            UnitControl(UnitControlType.UNKNOWN, 4, 24, 0, True),
            UnitControl(UnitControlType.SLIDER, 28, 8, 255, False, 0, 142),
            UnitControl(UnitControlType.ONOFF, 36, 1, 0, False),
        ],
        state,
        mode="EXT/Elements",
        model="Winsol Lamel Standard V4.1 TA16 180-3500N",
        state_length=5,
    )
    state._raw_state = bytes(5)  # noqa: SLF001
    return unit


def make_winsol_screen_unit() -> Unit:
    """Create a unit modeled on the Winsol SO! V4.1 screen fixture.

    Control layout from the real fixture definition: travel-time sensors
    (bits 0-27), dimmer "Screen Position" (bits 28-35), onoff $toggle
    (bit 36).
    """
    state = UnitState()
    state.dimmer = 64
    unit = _make_unit(
        27814,
        11,
        WINSOL_SCREEN_UUID,
        "Terrace Screen",
        [
            UnitControl(UnitControlType.UNKNOWN, 0, 4, 0, True),
            UnitControl(UnitControlType.SENSOR, 4, 0, 0, True),
            UnitControl(UnitControlType.UNKNOWN, 4, 24, 0, True),
            UnitControl(UnitControlType.DIMMER, 28, 8, 255, False),
            UnitControl(UnitControlType.ONOFF, 36, 1, 0, False),
        ],
        state,
        mode="EXT/1ch/Dim",
        model="SO! V4.1",
        state_length=5,
    )
    state._raw_state = bytes(5)  # noqa: SLF001
    return unit


def make_sensor_platform_unit() -> Unit:
    """Create a unit modeled on the LEDsGO Sensor Platform V4 fixture.

    Control layout from the real fixture definition: presence (bits 0-1),
    lux (bits 2-13), multiplexed wind/sun/PIR/rain (bits 14-33), and four
    writable sensor-enable switches (bits 34-37).
    """
    state = UnitState()
    state.presence = 1
    state.lux = 1234
    unit = _make_unit(
        19772,
        12,
        SENSOR_PLATFORM_UUID,
        "Weather Station",
        [
            UnitControl(UnitControlType.PRESENCE, 0, 2, 0, True),
            UnitControl(UnitControlType.LUX, 2, 12, 0, True, 0, 10000),
            UnitControl(UnitControlType.UNKNOWN, 14, 4, 0, True),
            UnitControl(UnitControlType.SENSOR, 18, 0, 0, True),
            UnitControl(UnitControlType.UNKNOWN, 18, 16, 0, True),
            UnitControl(UnitControlType.ONOFF, 34, 1, 1, False),
            UnitControl(UnitControlType.ONOFF, 35, 1, 1, False),
            UnitControl(UnitControlType.ONOFF, 36, 1, 1, False),
            UnitControl(UnitControlType.ONOFF, 37, 1, 1, False),
        ],
        state,
        mode="EXT/Elements{Presence,Daylight}",
        model="Sensor Platform V4",
        state_length=5,
    )
    # Enable bits 34-37 set (byte 4 = 0b00111100), like the real defaults.
    state._raw_state = bytes([0, 0, 0, 0, 0x3C])  # noqa: SLF001
    unit._sensor_cache.update({0: 1, 1: 140, 2: 40, 3: 0})  # noqa: SLF001
    return unit


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

    casa.setControlValue = AsyncMock()
    casa.setSlider = AsyncMock()
    casa.turnOff = AsyncMock()

    light = make_light_unit()
    louvre = make_louvre_unit()
    casa.units = [
        light,
        louvre,
        make_winsol_louvre_unit(),
        make_winsol_screen_unit(),
        make_sensor_platform_unit(),
    ]
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
