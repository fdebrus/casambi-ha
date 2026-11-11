"""A louvre or screen motor of the network.

A ``Motor`` ties one unit to its motion model: it feeds the model the
pushes, refreshes the estimate once a second while the motor moves, and
sends what the model plans. It exists whether or not the unit's cover
entity is enabled, so that the weather protection always has the model's
view of where the motor rests and a way to send it to safety.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging
from time import monotonic
from typing import Any, Final

from CasambiBt import Unit, UnitControl, UnitControlType

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .classify import UnitKind
from .hub import CasambiHub
from .louvre_channels import decode_channel, read_bit
from .motor_model import LouvreModel, MotorModel, Plan, ScreenModel, SetBit

_LOGGER = logging.getLogger(__name__)

# How often the estimated position of a moving motor is refreshed.
MOTION_UPDATE_INTERVAL: Final = timedelta(seconds=1)

MOTOR_KINDS: Final = (UnitKind.LOUVRE, UnitKind.SCREEN)


class UnitSource:
    """The live state of a motor unit, as its model reads it."""

    def __init__(self, unit: Unit, kind: UnitKind) -> None:
        """Initialize the source of a louvre or screen unit."""
        self._unit = unit
        # The louvres' position is their slider, the screens' their dimmer.
        self._attribute = "slider" if kind is UnitKind.LOUVRE else "dimmer"
        self.bit: UnitControl | None = next(
            (
                control
                for control in unit.unitType.controls
                if control.type == UnitControlType.ONOFF and not control.readonly
            ),
            None,
        )

    @property
    def has_state(self) -> bool:
        """Return True once the unit reported a state."""
        return self._unit.state is not None

    @property
    def has_bit(self) -> bool:
        """Return True if the unit has a writable start/stop bit."""
        return self.bit is not None

    @property
    def running(self) -> bool:
        """Return True while the start/stop bit is set."""
        state = self._unit.state
        if self.bit is None or state is None:
            return False
        return bool(read_bit(state.raw_state, self.bit.offset))

    @property
    def position(self) -> int | None:
        """Return the raw position the unit reports."""
        state = self._unit.state
        if state is None:
            return None
        position: int | None = getattr(state, self._attribute)
        return position

    @property
    def channel(self) -> tuple[int, int] | None:
        """Return the sensor channel multiplexed into the state."""
        state = self._unit.state
        return decode_channel(state.raw_state if state else None)


class Motor:
    """One louvre or screen motor: its unit, its model and its commands."""

    def __init__(
        self, hass: HomeAssistant, hub: CasambiHub, unit: Unit, kind: UnitKind
    ) -> None:
        """Initialize the motor of a louvre or screen unit."""
        self.hass = hass
        self.hub = hub
        self.unit = unit
        self.kind = kind
        self._source = UnitSource(unit, kind)
        self.model: MotorModel = (
            LouvreModel(self._source)
            if kind is UnitKind.LOUVRE
            else ScreenModel(self._source)
        )
        self.model.update(monotonic())

        self._listeners: list[Callable[[], None]] = []
        self._watchers: list[Callable[[Motor], None]] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._cancel_tick: CALLBACK_TYPE | None = None

    # --- Lifecycle -----------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start following the unit and the link."""
        self._unsubscribe = [
            self.hub.subscribe_unit(self.unit, self._handle_push),
            self.hub.subscribe_connection(self._handle_connection),
        ]

    @callback
    def async_stop(self) -> None:
        """Stop following the unit."""
        self._stop_ticking()
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    @callback
    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``listener`` whenever what the motor shows may have changed."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def add_watcher(self, watcher: Callable[[Motor], None]) -> CALLBACK_TYPE:
        """Call ``watcher`` once the model has taken in a push or come to rest.

        Watchers run after the listeners, on what the model made of the
        push: the weather protection waits for the motor to rest.
        """
        self._watchers.append(watcher)
        return lambda: self._watchers.remove(watcher)

    # --- What the motor shows ------------------------------------------

    @property
    def position(self) -> float | None:
        """Return the raw position, estimated while the motor moves."""
        return self.model.position(monotonic())

    @property
    def travel(self) -> bool | None:
        """Return True while the raw position rises, False while it falls."""
        return self.model.travel(monotonic())

    @property
    def rest_position(self) -> int | None:
        """Return the raw position the motor rests at, or None while it moves."""
        return self.model.rest_position(monotonic())

    @property
    def reported(self) -> int | None:
        """Return the raw position the unit reports: its target."""
        return self.model.reported

    # --- Commands ------------------------------------------------------

    async def async_open(self) -> None:
        """Open fully, like the wall button where possible."""
        await self._execute(self.model.open(monotonic()))

    async def async_close(self) -> None:
        """Close fully, like the wall button where possible."""
        await self._execute(self.model.close(monotonic()))

    async def async_stop_motion(self) -> None:
        """Stop where the motor is."""
        await self._execute(self.model.stop(monotonic()))

    async def async_set_position(self, raw: int) -> None:
        """Move to a raw position."""
        await self._execute(self.model.set_position(raw, monotonic()))

    async def async_secure(self) -> None:
        """Move to raw 0: louvres closed, screen retracted."""
        await self._execute(self.model.secure(monotonic()))

    async def _execute(self, plan: Plan) -> None:
        """Send a plan; a command that fails raises and ends it there."""
        if not plan.commands:
            return
        casa = self.hub.casa
        send_position: Callable[[Unit, int], Awaitable[Any]] = (
            casa.setSlider if self.kind is UnitKind.LOUVRE else casa.setLevel
        )
        for command in plan.commands:
            self.model.sending(command)
            if isinstance(command, SetBit):
                assert self._source.bit is not None
                await self.hub.async_command(
                    casa.setControlValue, self.unit, self._source.bit, int(command.on)
                )
            else:
                await self.hub.async_command(send_position, self.unit, command.raw)
        if plan.run is not None:
            self.model.started(plan.run, monotonic())
        # Show the command now rather than at the motor's answer.
        self._changed()

    # --- What happens to the motor -------------------------------------

    @callback
    def _handle_push(self, _unit: Unit) -> None:
        self.model.update(monotonic())
        self._changed()
        self._watch()

    @callback
    def _handle_connection(self) -> None:
        if not self.hub.available:
            self.model.link_lost()
            self._stop_ticking()
        self._notify()

    @callback
    def _tick(self, _now: datetime) -> None:
        """Refresh the estimate; stop once nothing is estimated any more."""
        if self.model.active(monotonic()):
            self._notify()
            return
        self._stop_ticking()
        self._notify()
        # The motor says nothing when it arrives; the weather protection
        # waits for it to rest before sending it back.
        self._watch()

    @callback
    def _changed(self) -> None:
        """Keep the estimate refreshed while there is one, and tell."""
        if self._cancel_tick is None and self.model.active(monotonic()):
            self._cancel_tick = async_track_time_interval(
                self.hass, self._tick, MOTION_UPDATE_INTERVAL
            )
        self._notify()

    def _stop_ticking(self) -> None:
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None

    def _notify(self) -> None:
        # One listener failing must not keep the others, and the watchers,
        # from the news.
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Error updating an entity of %s", self.unit.name)

    def _watch(self) -> None:
        for watcher in list(self._watchers):
            try:
                watcher(self)
            except Exception:
                _LOGGER.exception("Error watching %s", self.unit.name)
