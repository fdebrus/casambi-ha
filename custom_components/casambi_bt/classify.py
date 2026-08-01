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
    SENSOR_PLATFORM = auto()
    OTHER = auto()


def classify_unit(unit: Unit) -> UnitKind:
    """Classify a unit by its control layout and mode.

    - Any color-capable unit is a light.
    - A writable SLIDER on an EXT/ unit is a motor position in degrees
      (e.g. Winsol Lamel louvres: slider $pos 0-142° + onoff $startstop).
    - EXT/1ch/Dim with only a dimmer is a motor-driven screen/blind whose
      position is reported as the dimmer value (e.g. Winsol SO!).
    - EXT/Elements units with sensors and no actuator are sensor platforms
      (e.g. Sensor Platform V4: presence, lux, wind, sun, rain).
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
        return UnitKind.LOUVRE

    if mode.startswith("EXT/1ch/Dim") and UnitControlType.DIMMER in ctypes:
        return UnitKind.SCREEN

    if (
        mode.startswith("EXT/Elements")
        and ctypes & _SENSOR_TYPES
        and UnitControlType.DIMMER not in ctypes
    ):
        return UnitKind.SENSOR_PLATFORM

    if UnitControlType.DIMMER in ctypes or UnitControlType.ONOFF in ctypes:
        return UnitKind.LIGHT

    return UnitKind.OTHER
