"""How the Winsol louvre and screen motors move, as plain Python.

Nothing here knows Home Assistant or Bluetooth. A model reads the unit
through a ``MotorSource``, is told the time by its caller, and answers two
questions: where is the motor now, and what must be sent to make it do
something. ``motor.Motor`` feeds it the pushes and sends its plans.

What the motors do, from captures on a Winsol SO! Climate pergola:

- **The start/stop bit works like the wall button.** Setting it (0 -> 1)
  starts the motor opposite its last run, clearing it (1 -> 0) stops it,
  after which the motor reports where it stopped; at an end the motor
  clears it itself. The motor also starts opposite its last run after a
  position command.
- **The reported position is a target.** A position sent is reported at
  once, and a few seconds into a bit run the report jumps to the end the
  run heads for. Where the motor is in between is estimated from its
  travel time.
- **Setting the bit during a position move reverses the motor**, so such a
  move is stopped with a position instead: just ahead of the motor.

All positions are raw, 0-255, as the units exchange them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from typing import Final, Literal, Protocol

RAW_MAX: Final = 255

# After Home Assistant writes the start/stop bit, the motor echoes each
# write as a state push within this many seconds (0.5 s in the captures).
# Those echoes carry nothing new; a push after them, or after this, is the
# motor's own news (the wall button).
BIT_ECHO_SECONDS: Final = 3.0

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

# Winsol SO! screens report their full-range travel times in milliseconds,
# measured by the motor's calibration run. The fixture (27814) names sensor
# tag 0 "Time Up" and tag 1 "Time Down"; the channel header is tag + 1.
# Up retracts the screen into its box, down extends it.
SCREEN_CHANNEL_RETRACT: Final = 1
SCREEN_CHANNEL_EXTEND: Final = 2
SCREEN_DEFAULT_TRAVEL_MS: Final = 35000
# Stop aims this far ahead of the estimated position (the estimate's error
# plus the command latency; 1.5 s is about 4 % of a 35 s travel).
SCREEN_STOP_AHEAD_SECONDS: Final = 1.5
# A target right behind a moving screen is ignored by it. Margin (raw) on
# the estimate for what counts as behind: 52 reported against an estimated
# 51.7 was ignored, 65 against 64.1 was honoured (SO! V4.1 captures).
SCREEN_BEHIND_MARGIN: Final = 0.5

# What a state push meant to the model.
type Step = Literal["echo", "run", "stop", "report", "move"] | None


class MotorSource(Protocol):
    """The unit as it is right now.

    The library updates a unit in place, also with the bit Home Assistant
    has just written, so the model always reads the live values.
    """

    @property
    def has_state(self) -> bool:
        """Return True once the unit reported a state."""

    @property
    def has_bit(self) -> bool:
        """Return True if the unit has a writable start/stop bit."""

    @property
    def running(self) -> bool:
        """Return True while the start/stop bit is set."""

    @property
    def position(self) -> int | None:
        """Return the raw position the unit reports: its target."""

    @property
    def channel(self) -> tuple[int, int] | None:
        """Return the (channel, value) multiplexed into the state."""


@dataclass(frozen=True, slots=True)
class SetBit:
    """Write the start/stop bit."""

    on: bool


@dataclass(frozen=True, slots=True)
class SetPosition:
    """Send a raw position."""

    raw: int


type Command = SetBit | SetPosition


@dataclass(frozen=True, slots=True)
class BitRun:
    """A run started by setting the bit.

    :param raising: Whether the raw position rises.
    :param end: The raw end the run heads for.
    :param echoes: The bit values the motor will echo for the writes, in
        order: a reversal is a clear, then a set.
    """

    raising: bool
    end: int
    echoes: tuple[bool, ...] = (True,)


@dataclass(frozen=True, slots=True)
class Plan:
    """What to send for a command, in order.

    :param run: The bit run the writes start, to hand to ``started`` once
        they were all sent.
    """

    commands: tuple[Command, ...] = ()
    run: BitRun | None = None


class MotorModel(ABC):
    """Follows the start/stop bit of a motor.

    It knows which way a run goes and tells Home Assistant's own writes
    coming back from the wall button. The subclasses add where the motor
    is while it moves.
    """

    # The raw position of the fully open and the fully closed motor.
    raw_open: int = RAW_MAX
    raw_closed: int = 0

    def __init__(self, source: MotorSource) -> None:
        """Initialize the model; ``update`` takes the first state."""
        self._source = source
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

    # --- What the owner asks -------------------------------------------

    @property
    def has_bit(self) -> bool:
        """Return True if the motor can be run and stopped like the button."""
        return self._source.has_bit

    @property
    def reported(self) -> int | None:
        """Return the raw position the unit reports: its target."""
        return self._source.position

    @abstractmethod
    def update(self, now: float) -> Step:
        """Take in a state push; return what it was."""

    @abstractmethod
    def position(self, now: float) -> float | None:
        """Return the raw position the motor is at, estimated while moving."""

    @abstractmethod
    def travel(self, now: float) -> bool | None:
        """Return True while the raw position rises, False while it falls."""

    @abstractmethod
    def active(self, now: float) -> bool:
        """Return True while the position is estimated and needs refreshing."""

    def rest_position(self, now: float) -> int | None:
        """Return the raw position the motor rests at, or None while it runs.

        The weather protection sends a motor back from here.
        """
        if self._source.running:
            return None
        return self._source.position

    def open(self, now: float) -> Plan:
        """Plan a full opening, like the wall button where possible."""
        return self._drive_to_end(self.raw_open, now)

    def close(self, now: float) -> Plan:
        """Plan a full closing, like the wall button where possible."""
        return self._drive_to_end(self.raw_closed, now)

    def set_position(self, raw: int, now: float) -> Plan:
        """Plan a move to a raw position."""
        return Plan((SetPosition(raw),))

    def secure(self, now: float) -> Plan:
        """Plan the move to raw 0, the safe position of both motors."""
        return Plan((SetPosition(0),))

    def stop(self, now: float) -> Plan:
        """Plan a stop of a run started with the bit: clear it.

        Setting the bit would reverse the motor, so a motion started with a
        position is left to the subclass.
        """
        if not (self._source.has_bit and self._source.running):
            return Plan()
        # A stop asked for: its echo is a real rest, not a reversal's.
        self._echoes.clear()
        return Plan((SetBit(False),))

    def sending(self, command: Command) -> None:
        """Note a command that is about to be sent."""

    def started(self, run: BitRun, now: float) -> None:
        """Record a run Home Assistant just started by setting the bit.

        The library applies the bit to the unit at once, so the run is
        known before the motor echoes it; the echoes (a transient clear when
        reversing, then the set bit) must not re-derive the direction.
        """
        self._last_raising = run.raising
        self._was_running = True
        self._ran_since_rest = True
        self._echoes = list(run.echoes)
        self._echo_until = now + BIT_ECHO_SECONDS

    def link_lost(self) -> None:
        """Forget the direction: the link dropped.

        The wall button may be used any number of times while Home
        Assistant can't see the motor, so neither the last direction nor
        the rest position says anything once the link is back; the first
        state pushed then is taken as rest.
        """
        self._last_raising = None
        self._rest_raw = None
        self._was_running = False
        self._ran_since_rest = False
        self._echoes.clear()

    # --- The bit -------------------------------------------------------

    def _echo(self, running: bool, now: float) -> bool:
        """Return True if a push is the motor echoing Home Assistant's bit.

        The motor echoes each write in order (a reversal is a clear, then
        a set). A push with another bit than the next echo is a state it
        sent before it got the write. Once every write was echoed, a clear
        is a real stop: the wall button pressed during the run. An echo that
        never comes holds the stop back for BIT_ECHO_SECONDS at most.
        """
        if not self._echoes:
            return False
        if now >= self._echo_until:
            self._echoes.clear()
            return False
        if self._echoes[0] == running:
            del self._echoes[0]
        return True

    def _follow_bit(self, now: float) -> Step:
        """Follow the direction of runs and the rest position.

        A bit run is known by its direction when it starts. A position
        change at rest without a run in between is a position-driven move
        (the app, or a position command), and its direction is the way the
        position went. After a bit run the motor first echoes its stop with
        the stale rest position, then reports where it actually stopped;
        that step is only read as a move when the run's direction is
        unknown, since after a reversal it would point the wrong way.
        """
        running = self._source.running
        raw = self._source.position
        if self._echo(running, now):
            # Home Assistant's own write coming back: known already, and
            # the clear before a reversal is not a rest.
            return "echo"
        step: Step = None
        if running and not self._was_running:
            predicted = self._bit_direction()
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

    def _bit_direction(self) -> bool | None:
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
        if position >= RAW_MAX:
            return False
        if self._last_raising is None:
            return None
        return not self._last_raising

    def _drive_to_end(self, raw_end: int, now: float) -> Plan:
        """Plan a run to an end, with the bit when it goes that way."""
        raising = raw_end == RAW_MAX
        if self._source.has_bit:
            if self._source.running:
                if self._last_raising == raising:
                    return Plan()
                if self._last_raising is None:
                    # A run already under way when the link came back:
                    # which way the bit restarts it is anyone's guess, so
                    # stop it and send the end as a position.
                    self._echoes.clear()
                    return Plan((SetBit(False), SetPosition(raw_end)))
                # Like pressing the button twice: stop, then run back.
                return Plan(
                    (SetBit(False), SetBit(True)),
                    BitRun(raising, raw_end, (False, True)),
                )
            if self._rest_raw == raw_end:
                return Plan()
            if self._bit_direction() == raising:
                return Plan((SetBit(True),), BitRun(raising, raw_end))
        return Plan((SetPosition(raw_end),))


def _ahead(estimate: float, raising: bool) -> int:
    """Return the raw position just past an estimate, the way it travels."""
    position = math.ceil(estimate) if raising else math.floor(estimate)
    return min(max(position, 0), RAW_MAX)


class LouvreModel(MotorModel):
    """Louvres whose position is a Casambi slider (Winsol Lamel).

    The slider is the louvre angle, 0-142 degrees on the raw scale. A run
    stopped on the way first repeats the end it headed for, then reports
    where the louvres stopped (2 s later in the captures). The position is
    therefore estimated from the travel time while they move, and held
    after a stop until the motor reports.
    """

    def __init__(
        self, source: MotorSource, travel_seconds: float = LOUVRE_TRAVEL_SECONDS
    ) -> None:
        """Initialize the model of a louvre motor."""
        super().__init__(source)
        self._travel_seconds = travel_seconds
        # The motion shown: (raw start, raw end, when it set off).
        self._motion: tuple[float, int, float] | None = None
        # The estimate held after a stop, until the motor reports.
        self._held: float | None = None
        self._held_until = 0.0
        # The slider of the last push, the position before the current one.
        self._slider: int | None = None

    def update(self, now: float) -> Step:
        """Follow the run, and the motion shown for it."""
        before = self.position(now)
        step = self._follow_bit(now)
        if step == "run":
            # Started like the wall button, which way the model predicts.
            end = None
            if self._last_raising is not None:
                end = RAW_MAX if self._last_raising else 0
            self._start_motion(before, end, now)
        elif step == "stop":
            self._stopped(before, now)
        elif step == "report":
            # Where the louvres actually stopped.
            self._motion = None
            self._held = None
        elif step == "move":
            self._start_motion(before, self._source.position, now)
        if step != "echo":
            self._slider = self._source.position
        return step

    def position(self, now: float) -> float | None:
        """Return the raw position shown: estimated, held or reported."""
        if self._motion is not None:
            estimate = self._estimate(now)
            if estimate is not None:
                return estimate
            if self._source.running:
                # On the estimate, the end: the bit clears when it gets there.
                return float(self._motion[1])
            self._motion = None
        if self._held is not None:
            if now < self._held_until:
                return self._held
            self._held = None
        return None if self._slider is None else float(self._slider)

    def travel(self, now: float) -> bool | None:
        """Return whether the louvres open (True), close (False) or rest."""
        if self._source.running:
            return self._last_raising
        self.position(now)
        if self._motion is not None:
            return self._motion[1] > self._motion[0]
        return None

    def active(self, now: float) -> bool:
        """Return True while a motion is estimated or a stop is held."""
        self.position(now)
        return self._motion is not None or self._held is not None

    def stop(self, now: float) -> Plan:
        """Plan a stop of the louvres.

        A run started like the wall button is stopped by clearing the bit,
        holding the estimate where it was stopped. A move to a position (the
        app, sun tracking, a position from Home Assistant) is stopped with a
        position just ahead of the louvres. Nothing is sent at rest or when
        they would get there within that margin anyway.
        """
        if self._source.has_bit and self._source.running:
            self._stopped(self.position(now), now)
            return super().stop(now)
        self.position(now)
        if self._motion is None:
            return Plan()
        ahead = self._estimate(now + LOUVRE_STOP_AHEAD_SECONDS)
        if ahead is None:
            return Plan()
        raising = self._motion[1] > self._motion[0]
        return Plan((SetPosition(_ahead(ahead, raising)),))

    def started(self, run: BitRun, now: float) -> None:
        """Show the run Home Assistant set off from where the louvres are."""
        self._start_motion(self.position(now), run.end, now)
        super().started(run, now)

    def link_lost(self) -> None:
        """Forget the motion with the direction."""
        super().link_lost()
        self._motion = None
        self._held = None

    def _estimate(self, now: float) -> float | None:
        """Return where the motion shown has the louvres, if still moving."""
        if self._motion is None:
            return None
        start, end, since = self._motion
        travelled = (now - since) * RAW_MAX / self._travel_seconds
        if travelled < abs(end - start):
            return start + (travelled if end > start else -travelled)
        return None

    def _start_motion(self, start: float | None, end: int | None, now: float) -> None:
        """Show a motion from ``start`` to ``end``, if both are known."""
        self._held = None
        if start is None or end is None or start == end:
            self._motion = None
            return
        self._motion = (start, end, now)

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


class ScreenModel(MotorModel):
    """Screens whose position is a Casambi dimmer (Winsol SO!).

    The dimmer counts how far the screen is extended: 0 is fully retracted
    (open), 255 fully extended (closed). The position is estimated from
    when the target changed and the travel time the motor reports on its
    sensor channels.

    A position sent while the screen moves is only known to be honoured
    when it lies ahead of the screen: position 52 sent 7.1 s into a close
    (just behind it) was ignored and the screen kept extending, position 65
    sent 8.8 s in (ahead of it) stopped it there. Stop therefore sends the
    estimate plus a margin in the direction of travel, and a run to the
    other end during a position move uses the bit, which reverses the motor.
    """

    raw_open = 0
    raw_closed = RAW_MAX

    def __init__(self, source: MotorSource) -> None:
        """Initialize the model of a screen motor."""
        super().__init__(source)
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

    def update(self, now: float) -> Step:
        """Follow the motion; keep the rest position from its estimate."""
        self._track(now)
        if not (self._source.running or self._moving(now)):
            self._rest_raw = self._target
        return None

    def position(self, now: float) -> float | None:
        """Return the estimated raw position, or the reported target."""
        self._moving(now)
        estimate = self._estimate(now)
        if estimate is not None:
            return estimate
        reported = self._source.position
        return None if reported is None else float(reported)

    def travel(self, now: float) -> bool | None:
        """Return True while the screen extends, False while it retracts."""
        if not self._moving(now) or self._target is None:
            return None
        if self._target == self._move_from:
            return None
        return self._target > self._move_from

    def active(self, now: float) -> bool:
        """Return True while the screen is estimated to be travelling."""
        return self._moving(now)

    def rest_position(self, now: float) -> int | None:
        """Return where the screen rests per its model, or None while moving.

        The model, not the reported state, which is a target that can be
        stale after a button run.
        """
        if self._source.running or self._moving(now):
            return None
        if self._target is not None:
            return self._target
        return self._source.position

    def secure(self, now: float) -> Plan:
        """Plan the retraction of the screen for the weather protection.

        A screen at rest gets position 0, which needs no guess about which
        way the bit goes. One already moving out may ignore a position
        behind it, so a gust during a close would leave it extending: it is
        reversed with the bit instead, like open does.
        """
        if self._source.running or self._moving(now):
            return self._drive_to_end(self.raw_open, now)
        return Plan((SetPosition(self.raw_open),))

    def stop(self, now: float) -> Plan:
        """Plan a stop of the screen.

        A run started by the wall button or the app is stopped by clearing
        the bit. A run started with a position is stopped by a position
        just ahead of the screen. Nothing is sent when the screen is at
        rest or would reach its target within the margin anyway.
        """
        if self._source.running:
            return super().stop(now)
        if not self._moving(now) or self._target is None:
            return Plan()
        ahead = self._estimate(now + SCREEN_STOP_AHEAD_SECONDS)
        if ahead is None or ahead == self._target:
            return Plan()
        return Plan((SetPosition(_ahead(ahead, self._target > self._move_from)),))

    def sending(self, command: Command) -> None:
        """Note a position about to be sent.

        It is what the screen reports next, not where a run that just ended
        stopped, nor the target it ignored before.
        """
        if isinstance(command, SetPosition):
            self._settling = False
            self._ignored_target = None

    def started(self, run: BitRun, now: float) -> None:
        """Start the motion estimate for a run Home Assistant set off."""
        self._button_run = True
        self._start_motion(run.end, now)
        super().started(run, now)

    def link_lost(self) -> None:
        """Forget the motion with the direction.

        The screen may be driven while Home Assistant can't see it, so the
        last target is no reference for where it is when the link returns.
        """
        super().link_lost()
        self._target = None
        self._move_start = None
        self._button_run = False
        self._settling = False
        self._ignored_target = None

    def _drive_to_end(self, raw_end: int, now: float) -> Plan:
        """Plan a run to an end, also from the middle of a position move.

        The same direction gets the end as a position, which lies ahead;
        the opposite one the bit, which reverses the motor.
        """
        if (
            self._source.has_bit
            and not self._source.running
            and self._moving(now)
            and self._target is not None
        ):
            raising = raw_end == RAW_MAX
            if (self._target > self._move_from) == raising:
                if self._target != raw_end:
                    return Plan((SetPosition(raw_end),))
                return Plan()
            return Plan((SetBit(True),), BitRun(raising, raw_end))
        return super()._drive_to_end(raw_end, now)

    def _travel_time(self, extending: bool) -> float:
        """Return the full-range travel time (s) in the given direction."""
        channel = SCREEN_CHANNEL_EXTEND if extending else SCREEN_CHANNEL_RETRACT
        travel_ms = self._travel_ms.get(channel) or next(
            iter(self._travel_ms.values()), SCREEN_DEFAULT_TRAVEL_MS
        )
        return max(travel_ms, 1) / 1000

    def _estimate(self, now: float) -> float | None:
        """Return the estimated raw position at ``now``."""
        if self._target is None:
            return None
        if self._move_start is None:
            return float(self._target)
        distance = self._target - self._move_from
        speed = RAW_MAX / self._travel_time(distance > 0)
        travelled = (now - self._move_start) * speed
        if travelled >= abs(distance):
            return float(self._target)
        return self._move_from + (travelled if distance > 0 else -travelled)

    def _moving(self, now: float) -> bool:
        """Return True while the screen is estimated to be travelling."""
        if self._move_start is None:
            return False
        if self._estimate(now) == self._target:
            self._move_start = None
            if not self._source.running:
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

    def _track(self, now: float) -> bool:
        """Follow a state push; return True if a new motion started."""
        source = self._source
        if not source.has_state:
            return False
        channel = source.channel
        if channel is not None and channel[0] in (
            SCREEN_CHANNEL_EXTEND,
            SCREEN_CHANNEL_RETRACT,
        ):
            self._travel_ms[channel[0]] = channel[1]

        button_run = source.running
        if self._echo(button_run, now):
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
                    extending = estimate < RAW_MAX / 2
                return self._start_motion(RAW_MAX if extending else 0, now)
            # The run ended: freeze where the screen is. The target in this
            # same push is still the stale one; the next reports where it
            # actually stopped.
            estimate = self._estimate(now)
            if estimate is not None:
                self._target = round(estimate)
            self._move_start = None
            self._settling = True
            self._settle_until = now + BIT_ECHO_SECONDS
            self._stale_target = source.position
            return False
        if button_run:
            return False

        target = source.position
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
            return target <= estimate + SCREEN_BEHIND_MARGIN
        return target >= estimate - SCREEN_BEHIND_MARGIN
