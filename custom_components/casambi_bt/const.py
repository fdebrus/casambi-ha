"""Constants for the Casambi Bluetooth integration."""

from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

DOMAIN: Final = "casambi_bt"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SCENE,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
]

CONF_IMPORT_GROUPS: Final = "import_groups"
CONF_VERTICAL_AS_COVER: Final = "vertical_as_cover"
CONF_LOUVRE_AZIMUTH: Final = "louvre_azimuth"
CONF_TEMPERATURE_ENTITY: Final = "temperature_entity"
CONF_WIND_THRESHOLD: Final = "wind_threshold"
CONF_RAIN_ENTITY: Final = "rain_entity"
CONF_WIND_ENTITY: Final = "wind_entity"
CONF_FROST_LOCK_TEMPERATURE: Final = "frost_lock_temperature"

DEFAULT_LOUVRE_AZIMUTH: Final = 180
DEFAULT_WIND_THRESHOLD: Final = 35
# Below this temperature (°C) the louvres may be frozen to their seals.
DEFAULT_FROST_LOCK_TEMPERATURE: Final = 2.0
# Setpoint (°C) of the temperature control until the dial is moved.
DEFAULT_TEMP_SETPOINT: Final = 22.0

# Weather protection: a louvre or screen moved out of its safe position
# (by the wall button or the app) while the lock holds is sent back, but
# not twice within this many seconds, so a motor that just stopped and has
# not reported its position yet is not fought.
WEATHER_RESECURE_INTERVAL: Final = 30.0

# Full scale of the 8-bit raw values the library exchanges with the units.
CASA_RAW_MAX: Final = 255

EVENT_BUTTON: Final = f"{DOMAIN}_button_event"

# Reconnect backoff, in seconds. The delay starts at START and is
# multiplied by STEP after every failed attempt until it reaches MAX.
# When Home Assistant reports the device as out of range the delay is
# raised to NO_DEVICE at once, since retrying quickly cannot help.
RECONNECT_BACKOFF_START: Final = 2
RECONNECT_BACKOFF_STEP: Final = 2
RECONNECT_BACKOFF_MAX: Final = 300
RECONNECT_BACKOFF_NO_DEVICE: Final = 60
# Warn in the log once a reconnect has been failing for this long (s).
RECONNECT_WARN_AFTER: Final = 300


def entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a setting from entry options with fallback to entry data."""
    return entry.options.get(key, entry.data.get(key, default))
