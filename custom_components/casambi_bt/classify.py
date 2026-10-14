"""Classification of Casambi units into kinds of hardware.

Casambi units only describe their controls, not what physical device they
are. A Winsol louvre motor is a "slider + onoff" unit, a Winsol SO! screen
is a "dimmer" unit in EXT/1ch/Dim mode, and the weather station is an
EXT/Elements unit with multiplexed sensors. The heuristics used here are
based on real fixture definitions and on the work of the
https://github.com/superkikim/casambi-bt-hass fork.
"""

from __future__ import annotations

from enum import Enum, auto

from CasambiBt import Unit, UnitControlType

_LIGHT_COLOR_TYPES = {
    UnitControlType.RGB,
    UnitControlType.WHITE,
    UnitControlType.TEMPERATURE,
    UnitControlType.XY,
    UnitControlType.VERTICAL,
    UnitControlType.WHITECOLORBALANCE,
}

_SENSOR_TYPES = {
    UnitControlType.SENSOR,
    UnitControlType.PRESENCE,
    UnitControlType.LUX,
}


class UnitKind(Enum):
    """The kind of physical device behind a Casambi unit."""

    LIGHT = auto()
    LOUVRE = auto()
    SCREEN = auto()
    RELAY = auto()
    SENSOR_PLATFORM = auto()
    UNSUPPORTED_MOTOR = auto()
    OTHER = auto()


def _is_known_louvre(unit: Unit) -> bool:
    """Return True if the motor has the tested Lamel Standard layout.

    That layout writes exactly one slider (the louvre angle) and at most one
    on/off bit (start/stop). Richer motors such as the Winsol Lamel
    Intelligent (Star pack) also write a shade/sun dimmer, a cool/warm
    slider and automatic/intelligent bits; driving them as a plain louvre
    would read the cool/warm setpoint as the position and toggle the
    automatic mode instead of the motor.
    """
    writable = [c.type for c in unit.unitType.controls if not c.readonly]
    return (
        writable.count(UnitControlType.SLIDER) == 1
        and writable.count(UnitControlType.ONOFF) <= 1
        and UnitControlType.DIMMER not in writable
    )


def classify_unit(unit: Unit) -> UnitKind:
    """Classify a unit by its control layout and mode.

    - Any color-capable unit is a light.
    - A writable SLIDER on an EXT/ unit is a motor position in degrees
      (e.g. Winsol Lamel louvres: slider $pos 0-142° + onoff $startstop).
      A motor with more writable controls than that (e.g. the Winsol Lamel
      Intelligent) is an unsupported motor and gets no entities.
    - EXT/1ch/Dim with only a dimmer is a motor-driven screen/blind whose
      position is reported as the dimmer value (e.g. Winsol SO!).
    - An EXT/ unit whose only control is a writable ONOFF bit is a relay
      switching some external load (e.g. the Winsol On-Off driving the
      pergola heating). Ordinary on/off luminaires are not in EXT/ mode
      and stay lights.
    - Units that report sensor readings and drive nothing are sensor
      platforms (e.g. Sensor Platform V4: presence, lux, wind, sun, rain).
      This is decided on the control types alone because the mode string
      varies between installations.
    - Anything else with a dimmer or onoff control is treated as a light,
      matching the previous behavior.
    """
    ctypes = {c.type for c in unit.unitType.controls}
    mode = unit.unitType.mode

    if ctypes & _LIGHT_COLOR_TYPES:
        return UnitKind.LIGHT

    if mode.startswith("EXT/") and any(
        c.type == UnitControlType.SLIDER and not c.readonly
        for c in unit.unitType.controls
    ):
        if _is_known_louvre(unit):
            return UnitKind.LOUVRE
        return UnitKind.UNSUPPORTED_MOTOR

    if mode.startswith("EXT/1ch/Dim") and UnitControlType.DIMMER in ctypes:
        return UnitKind.SCREEN

    if (
        mode.startswith("EXT/")
        and ctypes == {UnitControlType.ONOFF}
        and not all(c.readonly for c in unit.unitType.controls)
    ):
        return UnitKind.RELAY

    # Detected by control types rather than by the mode string: the mode
    # naming varies between installations, but a unit that reports sensor
    # readings and drives nothing is a sensor platform either way.
    if ctypes & _SENSOR_TYPES and UnitControlType.DIMMER not in ctypes:
        return UnitKind.SENSOR_PLATFORM

    if UnitControlType.DIMMER in ctypes or UnitControlType.ONOFF in ctypes:
        return UnitKind.LIGHT

    return UnitKind.OTHER
