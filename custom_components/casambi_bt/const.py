"""Constants for the Casambi Bluetooth integration."""

from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

DOMAIN: Final = "casambi_bt"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.LIGHT,
    Platform.SCENE,
    Platform.NUMBER,
]

CONF_IMPORT_GROUPS: Final = "import_groups"
CONF_VERTICAL_AS_COVER: Final = "vertical_as_cover"


def entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a setting from entry options with fallback to entry data."""
    return entry.options.get(key, entry.data.get(key, default))
