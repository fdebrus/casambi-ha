"""Scenes of the Casambi network."""

from __future__ import annotations

import logging
from typing import Any

from CasambiBt import Scene

from homeassistant.components.scene import Scene as SceneEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import CasambiNetworkEntity
from .hub import CasambiHub
from .models import CasambiConfigEntry

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the scene entities."""
    hub = entry.runtime_data.hub
    async_add_entities(CasambiScene(hub, scene) for scene in hub.get_scenes())


class CasambiScene(CasambiNetworkEntity, SceneEntity):
    """A scene of the network."""

    def __init__(self, hub: CasambiHub, scene: Scene) -> None:
        """Initialize a scene entity."""
        super().__init__(hub, str(scene.sceneId), scope="scene")
        self._scene = scene
        self._attr_name = scene.name

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the scene."""
        _LOGGER.debug("Switching to scene %s", self._scene.name)
        # scene.turn_on carries no brightness; the scene plays at full level.
        await self.hub.async_command(self.hub.casa.switchToScene, self._scene, 0xFF)
