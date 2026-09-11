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

CONF_DEMO: Final = "demo"
CONF_IMPORT_GROUPS: Final = "import_groups"
CONF_VERTICAL_AS_COVER: Final = "vertical_as_cover"
CONF_LOUVRE_AZIMUTH: Final = "louvre_azimuth"
CONF_TEMPERATURE_ENTITY: Final = "temperature_entity"
CONF_WIND_THRESHOLD: Final = "wind_threshold"

DEFAULT_LOUVRE_AZIMUTH: Final = 180
DEFAULT_WIND_THRESHOLD: Final = 35

EVENT_BUTTON: Final = f"{DOMAIN}_button_event"

# Reconnect backoff, in seconds. The delay starts at START and is
# multiplied by STEP after every failed attempt until it reaches MAX.
# When Home Assistant reports the device as out of range the delay is
# raised to NO_DEVICE at once, since retrying quickly cannot help.
RECONNECT_BACKOFF_START: Final = 2
RECONNECT_BACKOFF_STEP: Final = 2
RECONNECT_BACKOFF_MAX: Final = 300
RECONNECT_BACKOFF_NO_DEVICE: Final = 60


def entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a setting from entry options with fallback to entry data."""
    return entry.options.get(key, entry.data.get(key, default))
