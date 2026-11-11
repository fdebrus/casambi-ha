"""Runtime data of a Casambi config entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .hub import CasambiHub
    from .motor import Motor
    from .pergola import PergolaController


@dataclass(slots=True)
class CasambiData:
    """What a loaded entry runs on.

    The hub owns the Bluetooth link, each motor follows one louvre or
    screen, and the pergola controller holds the automations that act on
    them. Entities only show and command these.
    """

    hub: CasambiHub
    pergola: PergolaController
    # Louvre and screen motors by unit uuid.
    motors: dict[str, Motor]


type CasambiConfigEntry = ConfigEntry[CasambiData]
