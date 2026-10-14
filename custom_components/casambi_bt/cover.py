"""Support for Casambi covers (pergola louvres, screens, vertical controls)."""

from __future__ import annotations

from abc import ABCMeta, abstractmethod
from collections.abc import Callable, Coroutine
from datetime import timedelta
import logging
import math
from time import monotonic
from typing import Any, Final

from CasambiBt import Group, Unit, UnitControlType

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .const import CONF_IMPORT_GROUPS, CONF_VERTICAL_AS_COVER, DOMAIN, entry_option
from .entities import (
    CasambiEntity,
    CasambiNetworkGroup,
    CasambiUnitEntity,
    TypedEntityDescription,
)
from .sensor import decode_louvre_channel

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

CASA_RAW_MAX: Final = 255

# Winsol SO! screens report their full-range travel time in milliseconds on
# sensor channels 1 and 2 (35000 on the hardware this was captured on, the
# same both ways, so which channel is which direction is an assumption).
TRAVEL_CHANNEL_EXTEND: Final = 1
TRAVEL_CHANNEL_RETRACT: Final = 2
DEFAULT_TRAVEL_MS: Final = 35000

# A moving screen ignores a target behind it, so Stop aims this far ahead
# of the estimated position (covers the estimate's error and the command
# latency; 1.5 s is about 4 % of a 35 s travel).
STOP_AHEAD_SECONDS: Final = 1.5

# How often the estimated position of a moving screen is refreshed.
MOTION_UPDATE_INTERVAL: Final = timedelta(seconds=1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi cover entities."""
    casa_api = config_entry.runtime_data

    entities: list[CoverEntity] = []

    # Motor-driven units (e.g. Winsol pergola louvres and SO! screens) are
    # always covers - they are never lights.
    for unit in casa_api.get_units():
        kind = classify_unit(unit)
        if kind is UnitKind.LOUVRE:
            entities.append(CasambiLouvreCover(casa_api, unit))
        elif kind is UnitKind.SCREEN:
            entities.append(CasambiScreenCover(casa_api, unit))

    # Light units with a vertical control can optionally be shown as covers.
    if entry_option(config_entry, CONF_VERTICAL_AS_COVER, False):
        entities.extend(
            CasambiCoverUnit(casa_api, u)
            for u in casa_api.get_units([UnitControlType.VERTICAL])
        )
        if entry_option(config_entry, CONF_IMPORT_GROUPS, True):
            entities.extend(
                CasambiCoverGroup(casa_api, g)
                for g in casa_api.get_groups()
                if any(
                    u.unitType.get_control(UnitControlType.VERTICAL) is not None
                    for u in g.units
                )
            )

    async_add_entities(entities)


class CasambiCover(CasambiEntity, CoverEntity, metaclass=ABCMeta):
    """Defines a Casambi cover entity base class."""

    def __init__(
        self,
        api: CasambiApi,
        description: TypedEntityDescription,
        obj: Group | Unit,
    ) -> None:
        """Initialize a Casambi cover entity base class."""

        self._attr_device_class = CoverDeviceClass.BLIND
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )

        self._obj: Group | Unit
        super().__init__(api, description, obj)

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed."""
        position = self.current_cover_position
        if position is None:
            return None
        return position == 0


class CasambiVerticalCover(CasambiCover, metaclass=ABCMeta):
    """Base class for covers driven by the Casambi vertical control."""

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_casa_command(
            self._api.casa.setVertical(self._obj, CASA_RAW_MAX)
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_casa_command(self._api.casa.setVertical(self._obj, 0))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position (0-100)."""
        position = kwargs[ATTR_POSITION]
        await self._async_casa_command(
            self._api.casa.setVertical(self._obj, round(position * CASA_RAW_MAX / 100))
        )


class CasambiCoverUnit(CasambiVerticalCover, CasambiUnitEntity):
    """Defines a Casambi cover entity for a single vertical-control unit."""

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a Casambi cover entity."""

        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")

        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover (0-100)."""
        unit = self._obj
        if unit.state is not None and unit.state.vertical is not None:
            return round(unit.state.vertical * 100 / CASA_RAW_MAX)
        return None


class CasambiCoverGroup(CasambiVerticalCover, CasambiNetworkGroup):
    """Defines a Casambi cover entity for a group of vertical-control units."""

    def __init__(self, api: CasambiApi, group: Group) -> None:
        """Initialize a Casambi cover group entity."""

        desc = TypedEntityDescription(
            key=str(group.groudId), name=group.name, entity_type="cover"
        )

        self._obj: Group
        super().__init__(api, desc, group)

    @property
    def current_cover_position(self) -> int | None:
        """Return the average position of the covers in the group (0-100)."""
        values = [
            unit.state.vertical
            for unit in self._unit_map.values()
            if unit.state is not None and unit.state.vertical is not None
        ]
        if values:
            return round(sum(values) / len(values) * 100 / CASA_RAW_MAX)
        return None


class CasambiMotorCover(CasambiCover, CasambiUnitEntity, metaclass=ABCMeta):
    """Base class for motor-driven covers with a start/stop bit.

    Winsol louvres (onoff $startstop) and SO! screens (onoff $toggle) carry
    a writable ONOFF bit that works like their wall button: setting it
    (0 -> 1) starts the motor in the direction opposite to its last run,
    clearing it (1 -> 0) stops it, after which the motor reports where it
    stopped. Captured on a SO! V4.1 driven by its wall button: press, the
    bit reads 1 and the screen extends; press, the bit reads 0 and the
    screen reports target 42 (5.8 s of a 35 s travel); press, the bit reads
    1 and it retracts.

    The motor also starts opposite its last run after a position command
    (a SetLevel close followed by the bit retracted the screen; a SetSlider
    close followed by the bit opened the louvres). Open and close therefore
    work like the wall button whenever that direction is the one asked for:
    they set the bit, and stop clears it again at once. When the bit would
    start the wrong way, they fall back to a position command, which
    leaves the bit at 0; setting it during such a run would reverse the
    motor, so stop leaves that motion to the subclass.
    """

    # Raw position (0-255) of the fully open and fully closed ends.
    _raw_open: int = CASA_RAW_MAX
    _raw_closed: int = 0

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a motor cover entity."""
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")

        self._obj: Unit
        super().__init__(api, desc, unit)

        self._toggle_control = next(
            (
                c
                for c in unit.unitType.controls
                if c.type == UnitControlType.ONOFF and not c.readonly
            ),
            None,
        )
        if self._toggle_control is not None:
            self._attr_supported_features = (
                self._attr_supported_features or CoverEntityFeature(0)
            ) | CoverEntityFeature.STOP

        # Direction of the last run (raw position rising) and the position
        # the motor last rested at, to predict where the bit sends it.
        self._last_raising: bool | None = None
        self._rest_raw: int | None = None
        self._was_running = False
        self._note_direction(unit)

    @abstractmethod
    def _raw_position(self, unit: Unit) -> int | None:
        """Return the raw position (0-255) the unit reports."""

    @abstractmethod
    def _send_position(self, raw: int) -> Coroutine[Any, Any, None]:
        """Return the command moving the unit to a raw position."""

    def _note_direction(self, unit: Unit) -> None:
        """Follow the direction of runs and the rest position."""
        running = self._toggle_running(unit)
        raw = self._raw_position(unit)
        if running and not self._was_running:
            predicted = self._toggle_direction()
            if predicted is not None:
                self._last_raising = predicted
        elif (
            not running
            and not self._was_running
            and raw is not None
            and self._rest_raw is not None
            and raw != self._rest_raw
        ):
            self._last_raising = raw > self._rest_raw
        self._was_running = running
        if not running and raw is not None:
            self._rest_raw = raw

    def _toggle_direction(self) -> bool | None:
        """Return whether setting the bit now raises the raw position."""
        position = self._rest_raw
        if position is None:
            return None
        if position <= 0:
            return True
        if position >= CASA_RAW_MAX:
            return False
        if self._last_raising is None:
            return None
        return not self._last_raising

    @callback
    def _change_callback(self, unit: Unit) -> None:
        self._note_direction(unit)
        super()._change_callback(unit)

    async def _drive_to_end(self, raw_end: int) -> None:
        """Run to an end like the wall button when the bit goes that way."""
        control = self._toggle_control
        raising = raw_end > (self._rest_raw if self._rest_raw is not None else -1)
        if control is not None and not self._api.demo:
            if self._toggle_running():
                if self._last_raising == (raw_end == CASA_RAW_MAX):
                    return
                # Like pressing the button twice: stop, then run back.
                await self._async_casa_command(
                    self._api.casa.setControlValue(self._obj, control, 0)
                )
                await self._async_casa_command(
                    self._api.casa.setControlValue(self._obj, control, 1)
                )
                return
            if self._rest_raw == raw_end:
                return
            if self._toggle_direction() == raising:
                await self._async_casa_command(
                    self._api.casa.setControlValue(self._obj, control, 1)
                )
                return
        await self._async_casa_command(self._send_position(raw_end))

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover, like the wall button where possible."""
        await self._drive_to_end(self._raw_open)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover, like the wall button where possible."""
        await self._drive_to_end(self._raw_closed)

    def _toggle_running(self, unit: Unit | None = None) -> bool:
        """Return True if the start/stop bit is set, i.e. a button-style run."""
        control = self._toggle_control
        state = (unit or self._obj).state
        if control is None or state is None or state.raw_state is None:
            return False
        byte_index, bit_index = divmod(control.offset, 8)
        raw = state.raw_state
        return byte_index < len(raw) and bool(raw[byte_index] >> bit_index & 1)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop a run started with the start/stop bit by clearing the bit.

        Setting the bit would reverse the motor, so a motion started with a
        position command is not touched here.
        """
        if self._toggle_control is None or not self._toggle_running():
            return
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._toggle_control, 0)
        )


class CasambiLouvreCover(CasambiMotorCover):
    """Cover for louvre motors whose position is a Casambi slider.

    E.g. the Winsol Lamel: the slider is the louvre angle (0-142 degrees on
    the wire, normalized to 0-255 by the library).
    """

    def _raw_position(self, unit: Unit) -> int | None:
        return unit.state.slider if unit.state is not None else None

    def _send_position(self, raw: int) -> Coroutine[Any, Any, None]:
        return self._api.casa.setSlider(self._obj, raw)

    @property
    def current_cover_position(self) -> int | None:
        """Return the louvre position (0-100)."""
        unit = self._obj
        if unit.state is not None and unit.state.slider is not None:
            return round(unit.state.slider * 100 / CASA_RAW_MAX)
        return None

    @property
    def is_opening(self) -> bool:
        """Return True while the louvres open on a button-style run."""
        return self._toggle_running() and self._last_raising is True

    @property
    def is_closing(self) -> bool:
        """Return True while the louvres close on a button-style run."""
        return self._toggle_running() and self._last_raising is False

    def _check_frost_lock(self) -> None:
        """Refuse to open louvres that may be frozen to their seals.

        Closing and stopping stay possible: they don't pull frozen slats
        open. Commands from the wall button or the Casambi app bypass Home
        Assistant and are not affected.
        """
        temperature = self._api.frost_lock_temperature(self._obj)
        if temperature is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="frost_lock",
                translation_placeholders={"temperature": f"{temperature:g}"},
            )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the louvres unless the frost lock holds them."""
        self._check_frost_lock()
        await super().async_open_cover(**kwargs)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the louvres to a specific position (0-100)."""
        target = round(kwargs[ATTR_POSITION] * CASA_RAW_MAX / 100)
        current = self._raw_position(self._obj)
        if current is None or target > current:
            self._check_frost_lock()
        await self._async_casa_command(self._api.casa.setSlider(self._obj, target))


class CasambiScreenCover(CasambiMotorCover):
    """Cover for screen motors whose position is a Casambi dimmer.

    E.g. the Winsol SO! screen: the fixture's dimmer control is labeled
    "Screen Position".

    The dimmer runs the opposite way round to Home Assistant: Casambi
    reports 0 for a fully retracted (open) screen and 255 for a fully
    extended (closed) one, where Home Assistant uses 100 for open and 0
    for closed. Confirmed on Winsol SO! V4.1 hardware, where open and
    close were swapped before this inversion.

    The screen reports the position it is heading for, not where it is,
    so a close showed "closed" the moment it was sent. While it moves the
    position is therefore estimated from when the target changed and the
    full travel time the motor reports on its sensor channels (35 s), and
    the cover reports opening or closing until the estimate arrives.

    A run started by the wall button or the app sets the start/stop bit,
    and stop clears it like the button does. A run started with a position
    command can only be stopped with another position, and a position sent
    while it moves is only honoured when it lies ahead of the screen:
    position 52 sent 7.1 s into a close (just behind it) was ignored and
    the screen kept extending, position 65 sent 8.8 s in (ahead of it)
    stopped it there (SO! V4.1 captures). Stop then sends the estimated
    position plus a margin in the direction of travel, so the screen halts
    slightly past the point where Stop was pressed.
    """

    _attr_device_class = CoverDeviceClass.SHADE

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a screen cover entity."""
        super().__init__(api, unit)
        self._attr_device_class = CoverDeviceClass.SHADE
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
            | CoverEntityFeature.STOP
        )
        self._travel_ms: dict[int, int] = {}
        self._target: int | None = None
        self._move_from = 0.0
        self._move_start: float | None = None
        self._cancel_tick: Callable[[], None] | None = None
        self._button_run = False
        self._track_motion(unit)
        self._note_direction(unit)

    def _travel_seconds(self, extending: bool) -> float:
        """Return the full-range travel time in the given direction."""
        channel = TRAVEL_CHANNEL_EXTEND if extending else TRAVEL_CHANNEL_RETRACT
        travel_ms = self._travel_ms.get(channel) or next(
            iter(self._travel_ms.values()), DEFAULT_TRAVEL_MS
        )
        return max(travel_ms, 1) / 1000

    def _estimate(self, now: float) -> float | None:
        """Return the estimated raw position (0-255) at ``now``."""
        if self._target is None:
            return None
        if self._move_start is None:
            return float(self._target)
        distance = self._target - self._move_from
        speed = CASA_RAW_MAX / self._travel_seconds(distance > 0)
        travelled = (now - self._move_start) * speed
        if travelled >= abs(distance):
            return float(self._target)
        return self._move_from + (travelled if distance > 0 else -travelled)

    def _moving(self) -> bool:
        """Return True while the screen is estimated to be travelling."""
        if self._move_start is None:
            return False
        if self._estimate(monotonic()) == self._target:
            self._move_start = None
            return False
        return True

    def _start_motion(self, target: int, now: float) -> bool:
        """Set off from the current estimate towards ``target``."""
        estimate = self._estimate(now)
        previous = self._target
        self._target = target
        if previous is None or estimate is None:
            return False
        self._move_from = estimate
        self._move_start = now
        self._last_raising = target > estimate
        return True

    def _track_motion(self, unit: Unit) -> bool:
        """Follow a state update; return True if a new motion started."""
        state = unit.state
        if state is None:
            return False
        decoded = decode_louvre_channel(state.raw_state)
        if decoded is not None and decoded[0] in (
            TRAVEL_CHANNEL_EXTEND,
            TRAVEL_CHANNEL_RETRACT,
        ):
            self._travel_ms[decoded[0]] = decoded[1]
        now = monotonic()

        button_run = self._toggle_running(unit)
        if button_run != self._button_run:
            self._button_run = button_run
            if button_run:
                # Started like the wall button: the reported target is stale
                # and the screen heads for the end opposite its last run.
                estimate = self._estimate(now)
                if self._last_raising is not None:
                    extending = not self._last_raising
                else:
                    extending = (estimate or 0) < CASA_RAW_MAX / 2
                return self._start_motion(CASA_RAW_MAX if extending else 0, now)
            # The run ended: freeze where the screen is. The target in this
            # same update is still the stale one; the next reports where it
            # actually stopped.
            estimate = self._estimate(now)
            if estimate is not None:
                self._target = round(estimate)
            self._move_start = None
            return False
        if button_run:
            return False

        target = state.dimmer
        if target is None or target == self._target:
            return False
        return self._start_motion(target, now)

    _raw_open = 0
    _raw_closed = CASA_RAW_MAX

    def _raw_position(self, unit: Unit) -> int | None:
        return unit.state.dimmer if unit.state is not None else None

    def _send_position(self, raw: int) -> Coroutine[Any, Any, None]:
        return self._api.casa.setLevel(self._obj, raw)

    def _note_direction(self, unit: Unit) -> None:
        """Keep the rest position from the motion estimate.

        The screen tracks its runs itself (``_track_motion``), which also
        sets the direction of the last one.
        """
        if not hasattr(self, "_target"):
            return
        self._was_running = self._toggle_running(unit)
        if self._was_running or self._moving():
            return
        self._rest_raw = self._target

    @callback
    def _change_callback(self, unit: Unit) -> None:
        if self._track_motion(unit) and self._cancel_tick is None:
            self._cancel_tick = async_track_time_interval(
                self.hass, self._tick, MOTION_UPDATE_INTERVAL
            )
        super()._change_callback(unit)

    @callback
    def _tick(self, _now: Any) -> None:
        """Refresh the estimated position while the screen moves."""
        if not self._moving():
            self._stop_ticking()
        self.async_write_ha_state()

    def _stop_ticking(self) -> None:
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the screen.

        A run started by the wall button or the app is stopped by clearing
        the start/stop bit. A run started with a position command is
        stopped by a position just ahead of the screen, since it ignores
        one behind it: the estimated position plus a margin in the direction
        of travel. Nothing is sent when the screen is at rest or would reach
        its target within the margin anyway.
        """
        if self._toggle_running():
            await super().async_stop_cover(**kwargs)
            return
        if not self._moving() or self._target is None:
            return
        extending = self._target > self._move_from
        ahead = self._estimate(monotonic() + STOP_AHEAD_SECONDS)
        if ahead is None or ahead == self._target:
            return
        position = math.ceil(ahead) if extending else math.floor(ahead)
        position = min(max(position, 0), CASA_RAW_MAX)
        await self._async_casa_command(self._api.casa.setLevel(self._obj, position))

    async def async_will_remove_from_hass(self) -> None:
        """Stop refreshing when the entity is removed."""
        self._stop_ticking()
        await super().async_will_remove_from_hass()

    @property
    def current_cover_position(self) -> int | None:
        """Return the screen position (0-100, inverted from the dimmer).

        While the screen travels this is the estimated position rather
        than the reported target.
        """
        self._moving()
        estimate = self._estimate(monotonic())
        if estimate is not None:
            return 100 - round(estimate * 100 / CASA_RAW_MAX)
        unit = self._obj
        if unit.state is not None and unit.state.dimmer is not None:
            return 100 - round(unit.state.dimmer * 100 / CASA_RAW_MAX)
        return None

    @property
    def is_opening(self) -> bool:
        """Return True while the screen retracts."""
        return (
            self._moving()
            and self._target is not None
            and self._target < self._move_from
        )

    @property
    def is_closing(self) -> bool:
        """Return True while the screen extends."""
        return (
            self._moving()
            and self._target is not None
            and self._target > self._move_from
        )

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the screen to a specific position (0-100)."""
        position = kwargs[ATTR_POSITION]
        await self._async_casa_command(
            self._api.casa.setLevel(
                self._obj, round((100 - position) * CASA_RAW_MAX / 100)
            )
        )
