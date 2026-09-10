"""An in-memory Casambi network used by the demo mode.

Demo mode lets the integration be set up without any hardware: it builds
the units of a typical Winsol pergola (louvre motor, screen, sensor
platform, lights) from their real fixture definitions and answers
commands locally, so covers move, lights dim and the automations can be
tried out before the hardware exists.

The simulated sensor readings are driven by the demo control entities
(simulated wind, rain and presence) and by the current solar position.
"""

from __future__ import annotations

from datetime import timedelta
from math import radians, sin
from typing import Any, Final

from CasambiBt import (
    Group,
    Scene,
    Unit,
    UnitControl,
    UnitControlType,
    UnitState,
    UnitType,
)
from CasambiBt._switch import SwitchEvent

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .suntrack import get_sun_position

DEMO_NETWORK_ID: Final = "casambi-demo-network"
DEMO_NETWORK_NAME: Final = "Casambi Demo"
DEMO_ADDRESS: Final = "00:00:00:00:00:00"

# How often the sun-derived readings are refreshed.
UPDATE_INTERVAL: Final = timedelta(minutes=5)

# The library clamps lux to 12 bit.
LUX_MAX: Final = 4095

PACKET_RAIN: Final = 0
PACKET_WIND: Final = 1
PACKET_SOLAR: Final = 2
PACKET_PIR: Final = 3


def _unit(
    type_id: int,
    device_id: int,
    name: str,
    model: str,
    mode: str,
    controls: list[UnitControl],
    state: UnitState,
    state_length: int = 5,
) -> Unit:
    """Build a unit from a fixture definition."""
    unit = Unit(
        _typeId=type_id,
        deviceId=device_id,
        uuid=f"demo-unit-{device_id}",
        address=DEMO_ADDRESS,
        name=name,
        firmwareVersion="demo",
        unitType=UnitType(
            id=type_id,
            model=model,
            manufacturer="LEDsGO",
            mode=mode,
            stateLength=state_length,
            controls=controls,
        ),
    )
    unit._state = state  # noqa: SLF001
    unit._on = True  # noqa: SLF001
    unit._online = True  # noqa: SLF001
    return unit


def _make_louvre() -> Unit:
    """Build a Winsol Lamel Standard louvre motor."""
    state = UnitState()
    state.slider = 128
    unit = _unit(
        38915,
        1,
        "Pergola louvres",
        "Winsol Lamel Standard V4.1 TA16 180-3500N",
        "EXT/Elements",
        [
            UnitControl(UnitControlType.UNKNOWN, 0, 4, 0, True),
            UnitControl(UnitControlType.SENSOR, 4, 0, 0, True),
            UnitControl(UnitControlType.UNKNOWN, 4, 24, 0, True),
            UnitControl(UnitControlType.SLIDER, 28, 8, 255, False, 0, 142),
            UnitControl(UnitControlType.ONOFF, 36, 1, 0, False),
        ],
        state,
    )
    state._raw_state = bytes(5)  # noqa: SLF001
    return unit


def _make_screen() -> Unit:
    """Build a Winsol SO! screen motor."""
    state = UnitState()
    state.dimmer = 255
    unit = _unit(
        27814,
        2,
        "Terrace screen",
        "SO! V4.1",
        "EXT/1ch/Dim",
        [
            UnitControl(UnitControlType.UNKNOWN, 0, 4, 0, True),
            UnitControl(UnitControlType.SENSOR, 4, 0, 0, True),
            UnitControl(UnitControlType.UNKNOWN, 4, 24, 0, True),
            UnitControl(UnitControlType.DIMMER, 28, 8, 255, False),
            UnitControl(UnitControlType.ONOFF, 36, 1, 0, False),
        ],
        state,
    )
    state._raw_state = bytes(5)  # noqa: SLF001
    return unit


def _make_sensor_platform() -> Unit:
    """Build a LEDsGO Sensor Platform V4 weather station."""
    state = UnitState()
    state.presence = 0
    state.lux = 0
    unit = _unit(
        19772,
        3,
        "Pergola sensors",
        "Sensor Platform V4",
        "EXT/Elements{Presence,Daylight}",
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
    )
    # All four sensor elements enabled (bits 34-37).
    state._raw_state = bytes([0, 0, 0, 0, 0x3C])  # noqa: SLF001
    return unit


def _make_light(device_id: int, name: str) -> Unit:
    """Build a dimmable, tunable white light."""
    state = UnitState()
    state.dimmer = 180
    state.temperature = 3000
    return _unit(
        1000 + device_id,
        device_id,
        name,
        "Demo LED driver",
        "EXT/1ch/Dim/TW",
        [
            UnitControl(UnitControlType.DIMMER, 0, 8, 255, False),
            UnitControl(UnitControlType.TEMPERATURE, 8, 8, 3000, False, 2200, 5000),
        ],
        state,
        state_length=2,
    )


class DemoCasambi:
    """A local stand-in for :class:`CasambiBt.Casambi`.

    Only the parts of the API that the integration uses are implemented.
    Commands change the simulated state and notify the registered
    handlers, exactly like the real network does.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Build the demo network."""
        self._hass = hass
        self.connected = False

        self._louvre = _make_louvre()
        self._screen = _make_screen()
        self._sensors = _make_sensor_platform()
        lights = [_make_light(4, "Pergola spots"), _make_light(5, "Terrace strip")]

        self.units: list[Unit] = [self._louvre, self._screen, self._sensors, *lights]
        self.groups: list[Group] = [Group(1, "Pergola lights", lights)]
        self.scenes: list[Scene] = [Scene(1, "Evening"), Scene(2, "Dinner")]

        self._unit_changed_callbacks: list[Any] = []
        self._switch_event_callbacks: list[Any] = []
        self._disconnect_callbacks: list[Any] = []
        self._cancel_interval: Any = None

        # Values driven by the demo control entities.
        self.wind_kmh: float = 12.0
        self.raining: bool = False
        self.presence: bool = False

    # Network properties -------------------------------------------------

    @property
    def networkId(self) -> str:
        """Return the demo network id."""
        return DEMO_NETWORK_ID

    @property
    def networkName(self) -> str:
        """Return the demo network name."""
        return DEMO_NETWORK_NAME

    # Lifecycle ----------------------------------------------------------

    async def connect(self, *_args: Any, **_kwargs: Any) -> None:
        """Connect to the demo network."""
        self.connected = True
        self.refresh_sensors()
        if self._cancel_interval is None:
            self._cancel_interval = async_track_time_interval(
                self._hass, self._interval_update, UPDATE_INTERVAL
            )

    async def disconnect(self) -> None:
        """Disconnect from the demo network."""
        self.connected = False
        if self._cancel_interval is not None:
            self._cancel_interval()
            self._cancel_interval = None

    async def invalidateCache(self, *_args: Any, **_kwargs: Any) -> None:
        """Ignore cache invalidation; the demo network has no cache."""

    # Handlers -----------------------------------------------------------

    def registerUnitChangedHandler(self, handler: Any) -> None:
        """Register a unit changed handler."""
        self._unit_changed_callbacks.append(handler)

    def unregisterUnitChangedHandler(self, handler: Any) -> None:
        """Unregister a unit changed handler."""
        self._unit_changed_callbacks.remove(handler)

    def registerSwitchEventHandler(self, handler: Any) -> None:
        """Register a switch event handler."""
        self._switch_event_callbacks.append(handler)

    def unregisterSwitchEventHandler(self, handler: Any) -> None:
        """Unregister a switch event handler."""
        self._switch_event_callbacks.remove(handler)

    def registerDisconnectCallback(self, handler: Any) -> None:
        """Register a disconnect callback."""
        self._disconnect_callbacks.append(handler)

    def unregisterDisconnectCallback(self, handler: Any) -> None:
        """Unregister a disconnect callback."""
        self._disconnect_callbacks.remove(handler)

    def _notify(self, unit: Unit) -> None:
        for handler in list(self._unit_changed_callbacks):
            handler(unit)

    def fire_switch_event(self, event: SwitchEvent) -> None:
        """Deliver a simulated switch event."""
        for handler in list(self._switch_event_callbacks):
            handler(event)

    # Simulated sensors --------------------------------------------------

    @callback
    def _interval_update(self, _now: Any) -> None:
        self.refresh_sensors()

    def refresh_sensors(self) -> None:
        """Recompute the sensor readings and notify listeners."""
        elevation, _azimuth = get_sun_position(self._hass)
        daylight = max(0.0, sin(radians(elevation)))

        state = self._sensors.state
        if state is not None:
            state.lux = round(daylight * LUX_MAX)
            state.presence = 1 if self.presence else 0

        cache = self._sensors._sensor_cache  # noqa: SLF001
        cache[PACKET_RAIN] = 5 if self.raining else 1
        cache[PACKET_WIND] = round(self.wind_kmh * 4)
        cache[PACKET_SOLAR] = round(daylight * 250 * 4)
        cache[PACKET_PIR] = 1 if self.presence else 0

        self._notify(self._sensors)

    # Commands -----------------------------------------------------------

    def _target_units(self, target: Unit | Group | None) -> list[Unit]:
        if target is None:
            return list(self.units)
        if isinstance(target, Group):
            return list(target.units)
        return [target]

    def _apply(self, target: Unit | Group | None, **values: Any) -> None:
        for unit in self._target_units(target):
            state = unit.state
            if state is None:
                continue
            for name, value in values.items():
                if value is None:
                    continue
                setattr(state, name, value)
            if "dimmer" in values and values["dimmer"] is not None:
                unit._on = values["dimmer"] > 0  # noqa: SLF001
            self._notify(unit)

    async def setLevel(self, target: Unit | Group | None, level: int) -> None:
        """Set the brightness of the target."""
        self._apply(target, dimmer=level)

    async def setVertical(self, target: Unit | Group | None, vertical: int) -> None:
        """Set the vertical value of the target."""
        self._apply(target, vertical=vertical)

    async def setSlider(self, target: Unit | Group | None, value: int) -> None:
        """Set the slider value of the target."""
        self._apply(target, slider=value)

    async def setWhite(self, target: Unit | Group | None, level: int) -> None:
        """Set the white level of the target."""
        self._apply(target, white=level)

    async def setColor(
        self, target: Unit | Group | None, color: tuple[int, int, int]
    ) -> None:
        """Set the color of the target."""
        self._apply(target, rgb=color)

    async def setTemperature(
        self, target: Unit | Group | None, temperature: int
    ) -> None:
        """Set the color temperature of the target."""
        self._apply(target, temperature=temperature)

    async def setUnitState(self, target: Unit, state: UnitState) -> None:
        """Set the complete state of a unit."""
        target._state = state  # noqa: SLF001
        target._on = bool(state.dimmer is None or state.dimmer > 0)  # noqa: SLF001
        self._notify(target)

    async def setControlValue(
        self, unit: Unit, control: UnitControl, value: int
    ) -> None:
        """Write a single control into the raw state of a unit."""
        state = unit.state
        if state is None or state.raw_state is None:
            return
        raw = bytearray(state.raw_state)
        byte_index, bit_index = divmod(control.offset, 8)
        if byte_index < len(raw):
            mask = ((1 << control.length) - 1) << bit_index
            raw[byte_index] = (raw[byte_index] & ~mask & 0xFF) | (
                (value << bit_index) & mask
            )
            state._raw_state = bytes(raw)  # noqa: SLF001
        self._notify(unit)

    async def turnOn(self, target: Unit | Group | None) -> None:
        """Turn the target on."""
        self._apply(target, dimmer=255)

    async def turnOff(self, target: Unit | Group | None) -> None:
        """Turn the target off."""
        self._apply(target, dimmer=0)

    async def switchToScene(self, target: Scene, level: int = 0xFF) -> None:
        """Activate a scene."""
        for unit in self.units:
            if unit.unitType.get_control(UnitControlType.DIMMER) is not None:
                self._apply(unit, dimmer=level)
