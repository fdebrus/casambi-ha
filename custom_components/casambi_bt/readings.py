"""Readings taken from Home Assistant entities chosen in the options.

The pergola automation can take its rain, wind and temperature from any
entity. Each helper turns an entity state into the unit the automation
works in, or returns None when the entity can't tell.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

_LOGGER = logging.getLogger(__name__)

# Weather entity conditions that count as rain.
RAIN_CONDITIONS: Final = frozenset(
    {"rainy", "pouring", "lightning-rainy", "snowy-rainy", "hail"}
)


def entity_reports_rain(state: State | None) -> bool | None:
    """Return whether a rain entity reports rain, or None if it can't tell.

    A binary sensor reports rain when on (e.g. a moisture sensor), a
    weather entity when its condition is a rainy one, and any other sensor
    when its value is above zero (e.g. a precipitation rate in mm/h).
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    if state.domain == "binary_sensor":
        return state.state == STATE_ON
    if state.domain == "weather":
        return state.state in RAIN_CONDITIONS
    try:
        return float(state.state) > 0
    except ValueError:
        return None


def entity_wind_kmh(state: State | None) -> float | None:
    """Return a wind speed entity's reading in km/h, or None if it can't tell.

    The reading is converted from the entity's unit of measurement; one
    without a unit is taken as km/h.
    """
    if state is None:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if not unit or unit == UnitOfSpeed.KILOMETERS_PER_HOUR:
        return value
    try:
        return SpeedConverter.convert(value, unit, UnitOfSpeed.KILOMETERS_PER_HOUR)
    except HomeAssistantError:
        _LOGGER.warning("Unsupported wind speed unit %s on %s", unit, state.entity_id)
        return None


def entity_temperature_c(state: State | None) -> float | None:
    """Return a temperature entity's reading in °C, or None if it can't tell.

    Home Assistant shows temperature sensors in the system's unit, so on
    an imperial install the state is in °F; one without a unit is taken
    as °C.
    """
    if state is None:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if not unit or unit == UnitOfTemperature.CELSIUS:
        return value
    try:
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
    except HomeAssistantError:
        _LOGGER.warning("Unsupported temperature unit %s on %s", unit, state.entity_id)
        return None
