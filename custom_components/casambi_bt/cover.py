"""Support for Casambi covers (pergola louvres, screens, vertical controls)."""

from __future__ import annotations

from abc import ABCMeta, abstractmethod
from collections.abc import Callable, Coroutine
from datetime import timedelta
import logging
import math
from time import monotonic
from typing import Any, Final, Literal

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
from .const import (
    CASA_RAW_MAX,
    CONF_IMPORT_GROUPS,
    CONF_VERTICAL_AS_COVER,
    DOMAIN,
    entry_option,
)
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

# Winsol SO! screens report their full-range travel times in milliseconds,
# measured by the motor's calibration run. The fixture (27814) names sensor
# tag 0 "Time Up" and tag 1 "Time Down"; the channel header is tag + 1.
# Up retracts the screen into its box, down extends it.
TRAVEL_CHANNEL_RETRACT: Final = 1
TRAVEL_CHANNEL_EXTEND: Final = 2
DEFAULT_TRAVEL_MS: Final = 35000

# A moving screen ignores a target behind it, so Stop aims this far ahead
# of the estimated position (covers the estimate's error and the command
# latency; 1.5 s is about 4 % of a 35 s travel).
STOP_AHEAD_SECONDS: Final = 1.5

# Winsol Lamel louvres: full travel (0-142 degrees) in either direction,
# timed on the wall button (21.9 s closing; 10.3 s of opening stopped at
# raw 118, 10.2 s predicted). The motor reports no travel time itself.
LOUVRE_TRAVEL_SECONDS: Final = 22.0
# After a stop in the middle of a run the louvres report where they stopped
# within this long (2 s in the captures); until then the estimate shows.
LOUVRE_STOP_REPORT_SECONDS: Final = 5.0
# A run stopped this close (raw) to its end is taken as arrived: the motor
# clears the bit itself at the end, with nothing more to report.
LOUVRE_END_MARGIN: Final = 26
# Moving louvres honour a new position, ahead of them (70 degrees sent
# during a close from 142 stopped them there) or behind (0 sent half way
# through an opening took them back), so stop sends the estimate this far
# ahead in the direction of travel: the command latency plus the
# estimate's error, 1 s is about 6 degrees.
LOUVRE_STOP_AHEAD_SECONDS: Final = 1.0

# A moving screen ignores a target behind it. Margin (raw) on the estimate
# for what counts as behind: 52 reported against an estimated 51.7 was
# ignored, 65 against 64.1 was honoured (SO! V4.1 captures).
BEHIND_MARGIN: Final = 0.5

# How often the estimated position of a moving screen is refreshed.
MOTION_UPDATE_INTERVAL: Final = timedelta(seconds=1)

# After Home Assistant writes the start/stop bit, the motor echoes each
# write as a state push within this many seconds (0.5 s in the captures).
# Those echoes carry nothing the integration doesn't already know; a push
# after them, or after this, is the motor's own news (the wall button).
BIT_ECHO_SECONDS: Final = 3.0

# What a state push meant to the motor model (see _note_direction).
type MotorStep = Literal["echo", "run", "stop", "report", "move"] | None


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

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )
    _obj: Group | Unit

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
            self._attr_supported_features |= CoverEntityFeature.STOP

        # Direction of the last run (raw position rising) and the position
        # the motor last rested at, to predict where the bit sends it.
        self._last_raising: bool | None = None
        self._rest_raw: int | None = None
        self._was_running = False
        # Whether a bit run happened since the motor last rested: its stop
        # position must not be read as a position-driven move.
        self._ran_since_rest = False
        # The bit values the motor is still to echo for the writes Home
        # Assistant just made, and until when, to tell those echoes from a
        # wall button stopping the run.
        self._echoes: list[bool] = []
        self._echo_until = 0.0
        # The timer refreshing an estimated position while the motor moves.
        self._cancel_tick: Callable[[], None] | None = None
        self._note_direction(unit)

    @abstractmethod
    def _raw_position(self, unit: Unit) -> int | None:
        """Return the raw position (0-255) the unit reports."""

    @abstractmethod
    def _send_position(self, raw: int) -> Coroutine[Any, Any, None]:
        """Return the command moving the unit to a raw position."""

    async def async_added_to_hass(self) -> None:
        """Tell the weather protection how to see whether the motor rests."""
        await super().async_added_to_hass()
        self._api.register_rest_check(self._obj, self._rest_position)
        self._api.register_lock_listener(self._write_state)

    async def async_will_remove_from_hass(self) -> None:
        """Withdraw the rest check and stop refreshing."""
        self._stop_ticking()
        self._api.unregister_rest_check(self._obj)
        self._api.unregister_lock_listener(self._write_state)
        await super().async_will_remove_from_hass()

    def _rest_position(self) -> int | None:
        """Return the raw position the motor rests at, or None while it runs.

        The weather protection sends a motor back from here: the cover's
        own model, not the reported state, which for a screen is a target
        that can be stale.
        """
        if self._toggle_running():
            return None
        return self._raw_position(self._obj)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show whether, and why, the weather protection holds the motor."""
        reason = self._api.weather_lock(self._obj)
        return {
            "weather_lock": reason,
            "weather_lock_value": self._api.wind_kmh if reason == "wind" else None,
        }

    def _snapshot(self) -> object:
        """Write only when what the cover shows changed.

        The louvre motor pushes its state every few seconds as it cycles
        through its sensor channels; most pushes change nothing here. A
        moving screen's estimate is refreshed by its own timer.
        """
        return (
            self.current_cover_position,
            self.is_opening,
            self.is_closing,
            self.available,
            tuple(self.extra_state_attributes.items()),
        )

    def _check_weather_lock(self, raw_target: int | None) -> None:
        """Refuse to move the motor out of safety while the weather holds it.

        Raw 0 is the safe position of both motors (louvres closed, screen
        retracted), so a target above the current raw position is refused;
        anything towards safety, and stop, stay possible. Like the frost
        lock this only binds Home Assistant: the wall button and the app
        still work, and the weather protection sends the motor back.
        """
        reason = self._api.weather_lock(self._obj)
        if reason is None:
            return
        current = self._raw_position(self._obj)
        if raw_target == 0 or (
            raw_target is not None and current is not None and raw_target <= current
        ):
            # Towards safety, which needs no known position to be safe.
            return
        wind = self._api.wind_kmh
        _LOGGER.info(
            "Weather protection: refused to move %s (%s%s)",
            self._obj.name,
            reason,
            f", {wind:g} km/h" if wind is not None else "",
        )
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=f"weather_lock_{reason}",
            translation_placeholders={
                "name": self._obj.name,
                "wind": f"{wind:g}" if wind is not None else "?",
            },
        )

    def _echo(self, running: bool) -> bool:
        """Return True if a push is the motor echoing Home Assistant's bit.

        The motor echoes each write in order (a reversal is a clear, then
        a set). A push with another bit than the next echo is a state it
        sent before it got the write. Once every write was echoed, a clear
        is a real stop: the wall button pressed during the run. An echo that
        never comes holds the stop back for BIT_ECHO_SECONDS at most.
        """
        if not self._echoes:
            return False
        if monotonic() >= self._echo_until:
            self._echoes.clear()
            return False
        if self._echoes[0] == running:
            del self._echoes[0]
        return True

    def _bit_run_started(
        self, raising: bool, raw_end: int, echoes: tuple[bool, ...] = (True,)
    ) -> None:
        """Record a run Home Assistant just started by setting the bit.

        The library applies the bit to the unit at once, so the run is
        known before the motor echoes it; the echoes (a transient clear when
        reversing, then the set bit) must not re-derive the direction.
        """
        self._last_raising = raising
        self._was_running = True
        self._ran_since_rest = True
        self._echoes = list(echoes)
        self._echo_until = monotonic() + BIT_ECHO_SECONDS
        # Show the run now rather than at the motor's echo.
        self._write_state()

    def _note_direction(self, unit: Unit) -> MotorStep:
        """Follow the direction of runs and the rest position.

        A bit run is known by its direction when it starts. A position
        change at rest without a run in between is a position-driven move
        (the app, or a position command), and its direction is the way the
        position went. After a bit run the motor first echoes its stop with
        the stale rest position, then reports where it actually stopped;
        that step is only read as a move when the run's direction is
        unknown, since after a reversal it would point the wrong way.

        Returns what the push was: Home Assistant's own write coming back
        ("echo"), a run starting ("run") or stopping ("stop"), where the
        motor stopped after a run ("report"), a position-driven move
        ("move"), or nothing new (None).
        """
        running = self._toggle_running(unit)
        raw = self._raw_position(unit)
        if self._echo(running):
            # Home Assistant's own write coming back: known already, and
            # the clear before a reversal is not a rest.
            return "echo"
        step: MotorStep = None
        if running and not self._was_running:
            predicted = self._toggle_direction()
            if predicted is not None:
                self._last_raising = predicted
            self._ran_since_rest = True
            step = "run"
        elif not running and self._was_running:
            step = "stop"
        elif (
            not running
            and raw is not None
            and self._rest_raw is not None
            and raw != self._rest_raw
        ):
            step = "report" if self._ran_since_rest else "move"
            if not self._ran_since_rest or self._last_raising is None:
                self._last_raising = raw > self._rest_raw
            self._ran_since_rest = False
        self._was_running = running
        if not running and raw is not None:
            self._rest_raw = raw
        return step

    def _ensure_ticking(self) -> None:
        if self._cancel_tick is None and self.hass is not None:
            self._cancel_tick = async_track_time_interval(
                self.hass, self._tick, MOTION_UPDATE_INTERVAL
            )

    def _stop_ticking(self) -> None:
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None

    @callback
    def _tick(self, _now: Any) -> None:
        """Refresh the estimated position while the motor moves."""
        self._stop_ticking()

    def _toggle_direction(self) -> bool | None:
        """Return whether setting the bit now raises the raw position.

        The motor runs opposite its last run. At an end that is the only
        way it can go, which is all that is known before any run was seen.
        After a run the end says nothing: a double tap from the closed end
        stops the run with the stale rest (still the end) and reverses it.
        """
        position = self._rest_raw
        if position is None:
            return None
        if self._ran_since_rest and self._last_raising is not None:
            return not self._last_raising
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

    @callback
    def _connection_callback(self) -> None:
        """Forget the direction when the link drops.

        The wall button may be used any number of times while Home
        Assistant can't see the motor, so neither the last direction nor
        the rest position says anything once the link is back; the first
        state pushed then is taken as rest.
        """
        if not self._api.available:
            self._last_raising = None
            self._rest_raw = None
            self._was_running = False
            self._ran_since_rest = False
            self._echoes.clear()
        super()._connection_callback()

    async def _set_bit(self, value: int) -> None:
        """Write the start/stop bit."""
        assert self._toggle_control is not None
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._toggle_control, value)
        )

    async def _drive_to_end(self, raw_end: int) -> None:
        """Run to an end like the wall button when the bit goes that way."""
        raising = raw_end == CASA_RAW_MAX
        if self._toggle_control is not None:
            if self._toggle_running():
                if self._last_raising == raising:
                    return
                if self._last_raising is None:
                    # A run already under way when the link came back:
                    # which way the bit restarts it is anyone's guess, so
                    # stop it and send the end as a position.
                    self._echoes.clear()
                    await self._set_bit(0)
                    await self._async_casa_command(self._send_position(raw_end))
                    return
                # Like pressing the button twice: stop, then run back.
                await self._set_bit(0)
                await self._set_bit(1)
                self._bit_run_started(raising, raw_end, (False, True))
                return
            if self._rest_raw == raw_end:
                return
            if self._toggle_direction() == raising:
                await self._set_bit(1)
                self._bit_run_started(raising, raw_end)
                return
        await self._async_casa_command(self._send_position(raw_end))

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover, like the wall button where possible."""
        self._check_weather_lock(self._raw_open)
        await self._drive_to_end(self._raw_open)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover, like the wall button where possible."""
        self._check_weather_lock(self._raw_closed)
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
        # A stop asked for: its echo is a real rest, not a reversal's.
        self._echoes.clear()
        await self._set_bit(0)
        self._write_state()


class CasambiLouvreCover(CasambiMotorCover):
    """Cover for louvre motors whose position is a Casambi slider.

    E.g. the Winsol Lamel: the slider is the louvre angle, 0-142 degrees
    reported by the motor on the 8-bit scale 0-255 (fully open reads 255 in
    the captures).

    Like the screen, the slider is where the louvres head, not where they
    are: a position from the app is reported at once, and a few seconds
    into a run started with the bit the slider jumps to the end it runs to.
    A run stopped on the way first repeats that end, then reports where the
    louvres stopped (2 s later in the captures); at an end the motor clears
    the bit itself. The position shown is therefore estimated from the
    travel time (LOUVRE_TRAVEL_SECONDS) while they move, and held after a
    stop until the motor reports. The reported slider stays what the weather
    protection, the frost lock and sun tracking work on.
    """

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a louvre cover entity."""
        # Before the base class, whose __init__ already follows the run.
        # The motion shown: (raw start, raw end, when it set off).
        self._motion: tuple[float, int, float] | None = None
        # The estimate held after a stop, until the motor reports.
        self._held: float | None = None
        self._held_until = 0.0
        # The slider of the last push, the position before the current one.
        self._slider: int | None = None
        super().__init__(api, unit)

    def _raw_position(self, unit: Unit) -> int | None:
        return unit.state.slider if unit.state is not None else None

    def _send_position(self, raw: int) -> Coroutine[Any, Any, None]:
        return self._api.casa.setSlider(self._obj, raw)

    def _estimate(self, now: float) -> float | None:
        """Return where the motion shown has the louvres at ``now``, if moving."""
        if self._motion is None:
            return None
        start, end, since = self._motion
        travelled = (now - since) * CASA_RAW_MAX / LOUVRE_TRAVEL_SECONDS
        if travelled < abs(end - start):
            return start + (travelled if end > start else -travelled)
        return None

    def _shown(self, now: float) -> float | None:
        """Return the raw position shown: estimated, held or reported."""
        if self._motion is not None:
            estimate = self._estimate(now)
            if estimate is not None:
                return estimate
            end = self._motion[1]
            if self._toggle_running():
                # On the estimate, the end: the bit clears when it gets there.
                return float(end)
            self._motion = None
        if self._held is not None:
            if now < self._held_until:
                return self._held
            self._held = None
        return None if self._slider is None else float(self._slider)

    def _start_motion(self, start: float | None, end: int | None, now: float) -> None:
        """Show a motion from ``start`` to ``end``, if both are known."""
        self._held = None
        if start is None or end is None or start == end:
            self._motion = None
            return
        self._motion = (start, end, now)
        self._ensure_ticking()

    def _note_direction(self, unit: Unit) -> MotorStep:
        """Follow the run, and the motion shown for it."""
        now = monotonic()
        before = self._shown(now)
        step = super()._note_direction(unit)
        if step == "run":
            # Started like the wall button, which way the model predicts.
            end = None if self._last_raising is None else self._end(self._last_raising)
            self._start_motion(before, end, now)
        elif step == "stop":
            self._stopped(before, now)
        elif step == "report":
            # Where the louvres actually stopped.
            self._motion = None
            self._held = None
        elif step == "move":
            self._start_motion(before, self._raw_position(unit), now)
        if step != "echo":
            self._slider = self._raw_position(unit)
        return step

    def _end(self, raising: bool) -> int:
        return CASA_RAW_MAX if raising else 0

    def _stopped(self, position: float | None, now: float) -> None:
        """Hold the estimate where a run stopped until the motor reports."""
        if self._motion is None:
            return
        end = self._motion[1]
        self._motion = None
        if position is None or abs(position - end) <= LOUVRE_END_MARGIN:
            # Arrived, or as good as: the motor has nothing more to say.
            self._held = None
            return
        self._held = position
        self._held_until = now + LOUVRE_STOP_REPORT_SECONDS
        self._ensure_ticking()

    def _bit_run_started(
        self, raising: bool, raw_end: int, echoes: tuple[bool, ...] = (True,)
    ) -> None:
        """Show the run Home Assistant set off from where the louvres are."""
        now = monotonic()
        self._start_motion(self._shown(now), raw_end, now)
        super()._bit_run_started(raising, raw_end, echoes)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the louvres.

        A run started like the wall button is stopped by clearing the bit,
        holding the estimate where it was stopped. A move to a position (the
        app, sun tracking, a position from Home Assistant) is stopped with a
        position just ahead of the louvres, since setting the bit then would
        reverse them. Nothing is sent at rest or when they would get there
        within that margin anyway.
        """
        now = monotonic()
        if self._toggle_control is not None and self._toggle_running():
            self._stopped(self._shown(now), now)
            await super().async_stop_cover(**kwargs)
            return
        self._shown(now)
        if self._motion is None:
            return
        opening = self._motion[1] > self._motion[0]
        ahead = self._estimate(now + LOUVRE_STOP_AHEAD_SECONDS)
        if ahead is None:
            return
        position = math.ceil(ahead) if opening else math.floor(ahead)
        position = min(max(position, 0), CASA_RAW_MAX)
        await self._async_casa_command(self._send_position(position))

    @callback
    def _connection_callback(self) -> None:
        """Forget the motion when the link drops."""
        if not self._api.available:
            self._motion = None
            self._held = None
            self._stop_ticking()
        super()._connection_callback()

    @callback
    def _tick(self, _now: Any) -> None:
        """Refresh the estimate; stop once nothing is estimated any more."""
        self._shown(monotonic())
        if self._motion is None and self._held is None:
            self._stop_ticking()
        self._write_state()

    def _way(self) -> bool | None:
        """Return whether the louvres open (True), close (False) or rest."""
        if self._toggle_running():
            return self._last_raising
        self._shown(monotonic())
        if self._motion is not None:
            return self._motion[1] > self._motion[0]
        return None

    @property
    def current_cover_position(self) -> int | None:
        """Return the louvre position (0-100), estimated while they move."""
        shown = self._shown(monotonic())
        if shown is None:
            return None
        return round(shown * 100 / CASA_RAW_MAX)

    @property
    def is_opening(self) -> bool:
        """Return True while the louvres open."""
        return self._way() is True

    @property
    def is_closing(self) -> bool:
        """Return True while the louvres close."""
        return self._way() is False

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
        """Open the louvres unless the frost lock or the weather holds them."""
        self._check_frost_lock()
        await super().async_open_cover(**kwargs)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the louvres to a specific position (0-100)."""
        target = round(kwargs[ATTR_POSITION] * CASA_RAW_MAX / 100)
        current = self._raw_position(self._obj)
        if current is None or target > current:
            self._check_frost_lock()
        self._check_weather_lock(target)
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
    # Stop is always possible: a run started with a position command is
    # stopped with another position, whether or not the unit has the bit.
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.STOP
    )

    # The dimmer counts how far the screen is extended.
    _raw_open = 0
    _raw_closed = CASA_RAW_MAX

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a screen cover entity."""
        # Before the base class, whose __init__ already follows the run.
        self._travel_ms: dict[int, int] = {}
        self._target: int | None = None
        self._move_from = 0.0
        self._move_start: float | None = None
        self._button_run = False
        # After a button-style run ends, the next reported target is where
        # the screen stopped, not a new run; the stale one is ignored.
        self._settling = False
        self._stale_target: int | None = None
        # The stop report follows the end of a run within a fraction of a
        # second (171 ms in the captures); after this, a target is a move.
        self._settle_until = 0.0
        # A target reported behind the moving screen, which it ignored: the
        # unit keeps reporting it once the screen rests elsewhere.
        self._ignored_target: int | None = None
        super().__init__(api, unit)
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
            if not self._toggle_running():
                # Arrived on the estimate, with no push to say so: this is
                # where the bit starts the next run from.
                self._rest_raw = self._target
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
        if self._echo(button_run):
            # Home Assistant's own write coming back (with the clear before
            # a reversal): the motion it started goes on.
            return False
        if button_run != self._button_run:
            self._button_run = button_run
            if button_run:
                # Started like the wall button: the reported target is stale
                # and the screen heads for the end opposite its last run.
                estimate = self._estimate(now)
                if estimate is None:
                    # Nowhere known to start from (first state after a
                    # reconnect): wait for the run to end and take the
                    # position reported then.
                    return False
                if self._last_raising is not None:
                    extending = not self._last_raising
                else:
                    extending = estimate < CASA_RAW_MAX / 2
                return self._start_motion(CASA_RAW_MAX if extending else 0, now)
            # The run ended: freeze where the screen is. The target in this
            # same update is still the stale one; the next reports where it
            # actually stopped.
            estimate = self._estimate(now)
            if estimate is not None:
                self._target = round(estimate)
            self._move_start = None
            self._settling = True
            self._settle_until = now + BIT_ECHO_SECONDS
            self._stale_target = state.dimmer
            return False
        if button_run:
            return False

        target = state.dimmer
        if self._settling and now > self._settle_until:
            # No stop report came: whatever is reported now is a move.
            self._settling = False
        if self._settling:
            # Where the screen actually stopped. Not a run: it differs from
            # the estimate by the estimate's error, which says nothing
            # about the direction of the run that just ended.
            if target is None or target == self._stale_target:
                return False
            self._settling = False
            self._target = target
            return False
        if target is None or target in (self._target, self._ignored_target):
            return False
        if self._behind(target, now):
            # Like the screen, go on to where it was heading.
            self._ignored_target = target
            return False
        self._ignored_target = None
        return self._start_motion(target, now)

    def _behind(self, target: int, now: float) -> bool:
        """Return True if a target lies behind the screen as it moves."""
        if self._move_start is None or self._target is None:
            return False
        estimate = self._estimate(now)
        if estimate is None or estimate == self._target:
            return False
        if self._target > self._move_from:
            return target <= estimate + BEHIND_MARGIN
        return target >= estimate - BEHIND_MARGIN

    def _raw_position(self, unit: Unit) -> int | None:
        return unit.state.dimmer if unit.state is not None else None

    def _send_position(self, raw: int) -> Coroutine[Any, Any, None]:
        # A position sent now is what the screen reports next, not where a
        # run that just ended stopped, nor the target it ignored before.
        self._settling = False
        self._ignored_target = None
        return self._api.casa.setLevel(self._obj, raw)

    def _rest_position(self) -> int | None:
        """Return where the screen rests per its model, or None while moving."""
        if self._toggle_running() or self._moving():
            return None
        if self._target is not None:
            return self._target
        return self._raw_position(self._obj)

    def _note_direction(self, unit: Unit) -> MotorStep:
        """Keep the rest position from the motion estimate.

        The screen tracks its runs itself (``_track_motion``), which also
        sets the direction of the last one.
        """
        if self._toggle_running(unit) or self._moving():
            return None
        self._rest_raw = self._target
        return None

    @callback
    def _change_callback(self, unit: Unit) -> None:
        if self._track_motion(unit):
            self._ensure_ticking()
        super()._change_callback(unit)

    @callback
    def _connection_callback(self) -> None:
        """Forget the motion when the link drops.

        The screen may be driven while Home Assistant can't see it, so the
        last target is no reference for where it is when the link returns:
        the first state pushed after a reconnect is taken as rest.
        """
        if not self._api.available:
            self._target = None
            self._move_start = None
            self._button_run = False
            self._settling = False
            self._ignored_target = None
            self._stop_ticking()
        super()._connection_callback()

    def _bit_run_started(
        self, raising: bool, raw_end: int, echoes: tuple[bool, ...] = (True,)
    ) -> None:
        """Start the motion estimate for a run Home Assistant set off."""
        self._button_run = True
        if self._start_motion(raw_end, monotonic()):
            self._ensure_ticking()
        super()._bit_run_started(raising, raw_end, echoes)

    async def _drive_to_end(self, raw_end: int) -> None:
        """Run to an end, also from the middle of a position-driven move.

        A moving screen honours a position ahead of it and ignores one
        behind it, so the same direction gets the end as a position and the
        opposite one the bit, which reverses the motor.
        """
        if (
            self._toggle_control is not None
            and not self._toggle_running()
            and self._moving()
            and self._target is not None
        ):
            if (self._target > self._move_from) == (raw_end == CASA_RAW_MAX):
                if self._target != raw_end:
                    await self._async_casa_command(self._send_position(raw_end))
                return
            await self._set_bit(1)
            self._bit_run_started(raw_end == CASA_RAW_MAX, raw_end)
            return
        await super()._drive_to_end(raw_end)

    @callback
    def _tick(self, _now: Any) -> None:
        """Refresh the estimated position while the screen moves."""
        if not self._moving():
            self._stop_ticking()
            self._write_state()
            # The screen says nothing when it arrives; the weather
            # protection waits for it to rest before sending it back.
            self._api.run_late_callbacks(self._obj)
            return
        self._write_state()

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
        await self._async_casa_command(self._send_position(position))

    async def async_added_to_hass(self) -> None:
        """Let the weather protection retract the screen through the cover."""
        await super().async_added_to_hass()
        self._api.register_retract_handler(self._obj, self._async_retract)

    async def async_will_remove_from_hass(self) -> None:
        """Withdraw the retract handler."""
        self._api.unregister_retract_handler(self._obj)
        await super().async_will_remove_from_hass()

    async def _async_retract(self) -> None:
        """Retract the screen for the weather protection.

        A screen at rest gets position 0, which needs no guess about which
        way the bit goes. One already moving ignores a position behind it,
        so a gust during a close would leave it extending: it is reversed
        with the bit instead, like open does.
        """
        if self._toggle_running() or self._moving():
            await self._drive_to_end(self._raw_open)
        else:
            await self._async_casa_command(self._send_position(self._raw_open))

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
        raw = round((100 - kwargs[ATTR_POSITION]) * CASA_RAW_MAX / 100)
        self._check_weather_lock(raw)
        await self._async_casa_command(self._send_position(raw))
