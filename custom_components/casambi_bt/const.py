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

DEFAULT_LOUVRE_AZIMUTH: Final = 180
DEFAULT_WIND_THRESHOLD: Final = 35

EVENT_BUTTON: Final = f"{DOMAIN}_button_event"


def entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a setting from entry options with fallback to entry data."""
    return entry.options.get(key, entry.data.get(key, default))
