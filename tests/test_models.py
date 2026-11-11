"""Tests for the motor models on their own, replaying captures.

No Home Assistant here: the models are plain Python, fed the unit's state
and the time. Positions are raw (0-255).
"""

from dataclasses import dataclass

import pytest

from custom_components.casambi_bt.motor_model import (
    BitRun,
    LouvreModel,
    Plan,
    ScreenModel,
    SetBit,
    SetPosition,
)


@dataclass
class FakeUnit:
    """A motor unit as its model reads it."""

    position: int | None = 0
    running: bool = False
    channel: tuple[int, int] | None = None
    has_bit: bool = True
    has_state: bool = True


def test_louvre_button_run_stopped_half_way() -> None:
    """Test the wall button run captured on the Lamel at 13:28:32.

    Bit set from closed; the slider jumps to the end 1.3 s in; the button
    stops the run after 10.3 s; the louvres report raw 118 two seconds on.
    """
    unit = FakeUnit(position=0)
    model = LouvreModel(unit)
    model.update(0.0)

    unit.running = True
    assert model.update(100.0) == "run"
    unit.position = 255
    assert model.update(101.3) is None
    assert model.travel(101.3) is True
    assert model.position(101.3) == pytest.approx(1.3 * 255 / 22)

    unit.running = False
    assert model.update(110.3) == "stop"
    # Held where it stopped: 120 by the estimate, 118 by the motor.
    assert model.position(110.3) == pytest.approx(119.4, abs=0.1)
    assert model.travel(110.3) is None
    assert model.active(110.3)

    unit.position = 118
    assert model.update(112.3) == "report"
    assert model.position(112.3) == 118
    assert not model.active(112.3)


def test_louvre_run_to_the_end_keeps_the_end_until_the_bit_clears() -> None:
    """Test a full close: 21.9 s on the wall button, 22 s in the model."""
    unit = FakeUnit(position=255)
    model = LouvreModel(unit)
    model.update(0.0)

    unit.running = True
    assert model.update(100.0) == "run"
    assert model.travel(100.0) is False
    # Past the estimate with the bit still set: at the end, still closing.
    assert model.position(123.0) == 0
    assert model.active(123.0)

    unit.running, unit.position = False, 0
    assert model.update(121.9) == "stop"
    assert model.position(121.9) == 0
    assert not model.active(121.9)


def test_louvre_plans_like_the_wall_button() -> None:
    """Test open from closed, a reversal and the stop of a position move."""
    unit = FakeUnit(position=0)
    model = LouvreModel(unit)
    model.update(0.0)

    # From the closed end the bit can only open.
    plan = model.open(10.0)
    assert plan == Plan((SetBit(True),), BitRun(raising=True, end=255))
    assert plan.run is not None
    unit.running = True
    model.started(plan.run, 10.0)
    assert model.open(11.0) == Plan()

    # Close during the opening: like pressing the button twice.
    assert model.close(12.0) == Plan(
        (SetBit(False), SetBit(True)), BitRun(False, 0, echoes=(False, True))
    )
    assert model.stop(12.0) == Plan((SetBit(False),))


def test_louvre_stop_of_a_position_move_aims_ahead() -> None:
    """Test stop while the louvres move to a position from the app."""
    unit = FakeUnit(position=0)
    model = LouvreModel(unit)
    model.update(0.0)
    assert model.stop(50.0) == Plan()

    unit.position = 255
    assert model.update(100.0) == "move"
    # 11 s of a 22 s travel, rounded further along.
    assert model.stop(110.0) == Plan((SetPosition(128),))


def test_button_stop_right_after_a_start_from_home_assistant() -> None:
    """Test that only the motor's echo of the write is passed over."""
    unit = FakeUnit(position=255)
    model = LouvreModel(unit)
    model.update(0.0)
    plan = model.close(1000.0)
    assert plan.run is not None
    unit.running = True
    model.started(plan.run, 1000.0)

    assert model.update(1000.5) == "echo"
    unit.running = False
    assert model.update(1001.0) == "stop"


def test_screen_wall_button_capture() -> None:
    """Test the SO! wall button capture of 19:19.

    Press: the bit is set with a stale target and the screen extends.
    Press 5.8 s later: the bit clears, then the screen reports 42. Press
    again: it retracts.
    """
    unit = FakeUnit(position=0)
    model = ScreenModel(unit)
    model.update(0.0)
    assert model.rest_position(0.0) == 0

    unit.running = True
    model.update(100.0)
    assert model.travel(100.0) is True
    assert model.rest_position(102.0) is None

    unit.running = False
    model.update(105.8)
    assert model.position(105.8) == 42
    unit.position = 42
    model.update(106.0)
    assert model.rest_position(106.0) == 42

    unit.running = True
    model.update(109.0)
    assert model.travel(109.0) is False


def test_screen_travel_time_per_direction() -> None:
    """Test that extending uses Time Down and retracting uses Time Up."""
    unit = FakeUnit(position=0)
    model = ScreenModel(unit)
    # Channel 1 is the fixture's "Time Up", channel 2 its "Time Down".
    unit.channel = (1, 40000)
    model.update(0.0)
    unit.channel = (2, 20000)
    model.update(1.0)

    unit.position = 255
    model.update(100.0)
    assert model.position(110.0) == pytest.approx(127.5)
    unit.position = 0
    model.update(120.0)
    assert model.position(130.0) == pytest.approx(255 - 255 / 4)


def test_screen_is_secured_with_the_bit_while_it_extends() -> None:
    """Test the retraction for the weather protection."""
    unit = FakeUnit(position=0)
    model = ScreenModel(unit)
    model.update(0.0)
    assert model.secure(1.0) == Plan((SetPosition(0),))

    # Closing on a position: 0 would lie behind it, the bit reverses it.
    unit.position = 255
    model.update(100.0)
    assert model.secure(105.0) == Plan((SetBit(True),), BitRun(False, 0))


def test_link_loss_forgets_the_motion() -> None:
    """Test that nothing of the motion survives a dropped link."""
    unit = FakeUnit(position=0)
    model = ScreenModel(unit)
    model.update(0.0)
    unit.position = 255
    model.update(100.0)
    assert model.active(105.0)

    model.link_lost()
    assert not model.active(105.0)
    assert model.travel(105.0) is None
