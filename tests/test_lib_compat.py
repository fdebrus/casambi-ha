"""Contract tests against the real casambi-bt-skk library.

Every other test in this suite mocks the library, so a renamed attribute or
a method that stops being a coroutine would pass the whole suite and only
break against hardware. These tests import the pinned library for real and
assert that the API the integration relies on still exists and still has
the shape the integration assumes.

Keep this in sync with the code: when you start using a new library
attribute, add it here.
"""

from dataclasses import fields, is_dataclass
from importlib.metadata import version
import inspect
import json
from pathlib import Path
import re

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
from CasambiBt._switch import SwitchEvent
import CasambiBt.errors as casambi_errors
import pytest

# Methods the integration awaits. They must stay coroutines: CasambiProxy
# only wraps coroutine functions, so a method that became synchronous would
# silently lose its reconnect-on-bluetooth-error handling.
COROUTINE_METHODS = (
    "connect",
    "disconnect",
    "reconnect",
    "invalidateCache",
    "setLevel",
    "setVertical",
    "setSlider",
    "setColor",
    "setWhite",
    "setTemperature",
    "setUnitState",
    "setControlValue",
    "turnOn",
    "turnOff",
    "switchToScene",
)

# Synchronous members of Casambi used by the integration.
CASAMBI_MEMBERS = (
    "connected",
    "networkId",
    "networkName",
    "units",
    "groups",
    "scenes",
    "registerDisconnectCallback",
    "unregisterDisconnectCallback",
    "registerUnitChangedHandler",
    "unregisterUnitChangedHandler",
    "registerSwitchEventHandler",
    "unregisterSwitchEventHandler",
)

# Control types the classification and the platforms branch on.
CONTROL_TYPES = (
    "DIMMER",
    "SLIDER",
    "ONOFF",
    "VERTICAL",
    "RGB",
    "WHITE",
    "TEMPERATURE",
    "XY",
    "PRESENCE",
    "LUX",
    "SENSOR",
    "WHITECOLORBALANCE",
    "UNKNOWN",
)

# Errors the connection handling branches on. Losing one would change which
# failures are retried, which start a reauth, and which are fatal.
ERRORS = (
    "AuthenticationError",
    "BluetoothError",
    "BluetoothDeviceNotFoundError",
    "NetworkNotFoundError",
    "ProtocolError",
)


def _has_member(obj: type, name: str) -> bool:
    """Return True if a class exposes a member, field or annotation."""
    if hasattr(obj, name):
        return True
    if is_dataclass(obj) and name in {f.name for f in fields(obj)}:
        return True
    return name in getattr(obj, "__annotations__", {})


@pytest.mark.parametrize("name", COROUTINE_METHODS)
def test_casambi_coroutine_methods(name: str) -> None:
    """Test that awaited library methods exist and are still coroutines."""
    method = getattr(Casambi, name, None)
    assert method is not None, f"Casambi.{name} is gone from the library"
    assert inspect.iscoroutinefunction(method), (
        f"Casambi.{name} is no longer a coroutine; CasambiProxy would stop "
        "wrapping it and writes would no longer trigger a reconnect"
    )


@pytest.mark.parametrize("name", CASAMBI_MEMBERS)
def test_casambi_members(name: str) -> None:
    """Test that the synchronous parts of the library API still exist."""
    assert _has_member(Casambi, name), f"Casambi.{name} is gone from the library"


def test_reconnect_takes_a_device() -> None:
    """Test that reconnect still accepts the BLE device as its only argument."""
    params = list(inspect.signature(Casambi.reconnect).parameters)
    assert params[0] == "self"
    assert len(params) == 2, f"Casambi.reconnect signature changed: {params}"


@pytest.mark.parametrize(
    "name",
    ["uuid", "deviceId", "name", "address", "firmwareVersion", "unitType"],
)
def test_unit_fields(name: str) -> None:
    """Test the Unit fields used for entity and device identity."""
    assert _has_member(Unit, name), f"Unit.{name} is gone from the library"


@pytest.mark.parametrize("name", ["state", "online", "is_on", "sensor_cache"])
def test_unit_properties(name: str) -> None:
    """Test the Unit properties used for entity state and availability."""
    assert _has_member(Unit, name), f"Unit.{name} is gone from the library"


@pytest.mark.parametrize(
    "name",
    [
        "dimmer",
        "vertical",
        "slider",
        "rgb",
        "white",
        "temperature",
        "xy",
        "hs",
        "onoff",
        "presence",
        "lux",
        "colorsource",
        "raw_state",
    ],
)
def test_unit_state_attributes(name: str) -> None:
    """Test the UnitState attributes read by the platforms and diagnostics."""
    assert _has_member(UnitState, name), f"UnitState.{name} is gone"


@pytest.mark.parametrize(
    "name", ["id", "model", "manufacturer", "mode", "stateLength", "controls"]
)
def test_unit_type_fields(name: str) -> None:
    """Test the UnitType fields used for classification and device info."""
    assert _has_member(UnitType, name), f"UnitType.{name} is gone"


def test_unit_type_get_control() -> None:
    """Test that a control can still be looked up by type."""
    assert callable(UnitType.get_control)


@pytest.mark.parametrize("name", ["type", "offset", "length", "readonly"])
def test_unit_control_fields(name: str) -> None:
    """Test the UnitControl fields used to decode the Winsol wire format."""
    assert _has_member(UnitControl, name), f"UnitControl.{name} is gone"


@pytest.mark.parametrize("name", CONTROL_TYPES)
def test_control_types(name: str) -> None:
    """Test that the control types the classification branches on exist."""
    assert hasattr(UnitControlType, name), f"UnitControlType.{name} is gone"


@pytest.mark.parametrize("name", ERRORS)
def test_errors(name: str) -> None:
    """Test that the errors the connection handling catches still exist."""
    assert hasattr(casambi_errors, name), f"CasambiBt.errors.{name} is gone"


@pytest.mark.parametrize("name", ["unit_id", "button", "event"])
def test_switch_event_fields(name: str) -> None:
    """Test the SwitchEvent fields the button events are built from.

    These were renamed once in the library already, which broke the button
    entities without any test noticing.
    """
    assert _has_member(SwitchEvent, name), f"SwitchEvent.{name} is gone"


@pytest.mark.parametrize(
    ("cls", "name"),
    [(Group, "groudId"), (Group, "name"), (Group, "units"), (Scene, "name")],
)
def test_group_and_scene_fields(cls: type, name: str) -> None:
    """Test the Group and Scene fields used by the group and scene entities."""
    assert _has_member(cls, name), f"{cls.__name__}.{name} is gone"


def test_manifest_pins_the_tested_library() -> None:
    """Test that the version under test is the one the manifest requires.

    Without this the contract above could be checked against a different
    build than the one Home Assistant installs.
    """
    manifest = json.loads(
        Path("custom_components/casambi_bt/manifest.json").read_text(encoding="utf-8")
    )
    requirement = next(
        r for r in manifest["requirements"] if r.startswith("casambi-bt")
    )
    match = re.fullmatch(r"(?P<name>[\w-]+)==(?P<version>.+)", requirement)
    assert match is not None, f"unexpected requirement format: {requirement}"
    assert version(match["name"]) == match["version"], (
        f"tests run against {match['name']} {version(match['name'])} but the "
        f"manifest pins {match['version']}"
    )
