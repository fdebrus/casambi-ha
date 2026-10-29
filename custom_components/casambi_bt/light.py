"""Support for Casambi compatible lights."""

from __future__ import annotations

from abc import ABCMeta
from copy import copy
import logging
from typing import Any, Final

from CasambiBt import ColorSource, Group, Unit, UnitControlType, UnitState

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_XY_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind
from .const import CONF_IMPORT_GROUPS, DOMAIN, entry_option
from .entities import (
    CasambiEntity,
    CasambiNetworkGroup,
    CasambiUnitEntity,
    TypedEntityDescription,
)

CASA_LIGHT_CTRL_TYPES: Final[list[UnitControlType]] = [
    UnitControlType.DIMMER,
    UnitControlType.RGB,
    UnitControlType.WHITE,
    UnitControlType.ONOFF,
    UnitControlType.TEMPERATURE,
]

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi light entities."""
    casa_api = config_entry.runtime_data

    # Units like louvre or screen motors carry dimmer/onoff controls but are
    # covers, not lights, and on/off relays are switches.
    light_entities: list[CasambiLight] = []
    registry = er.async_get(hass)
    for unit in casa_api.get_units(CASA_LIGHT_CTRL_TYPES):
        kind = casa_api.unit_kind(unit)
        if kind is UnitKind.LIGHT:
            if capabilities(unit) == {ColorMode.UNKNOWN}:
                # E.g. a white channel alone: Home Assistant refuses a light
                # without a usable color mode, so there is nothing to show.
                _LOGGER.debug("No light entity for %s: no usable mode", unit.name)
                continue
            light_entities.append(CasambiLightUnit(casa_api, unit))
        elif kind is UnitKind.RELAY:
            # Relays used to be exposed as on/off lights; drop the stale
            # registry entry so only the switch remains.
            stale = registry.async_get_entity_id(
                LIGHT_DOMAIN,
                DOMAIN,
                f"{casa_api.casa.networkId}-unit-{unit.uuid}-light",
            )
            if stale is not None:
                _LOGGER.info("Removing %s, the relay is now a switch", stale)
                registry.async_remove(stale)

    group_entities: list[CasambiLight] = []
    if entry_option(config_entry, CONF_IMPORT_GROUPS, True):
        for group in casa_api.get_groups():
            if _group_is_a_light(casa_api, group):
                group_entities.append(CasambiLightGroup(casa_api, group))
                continue
            stale = registry.async_get_entity_id(
                LIGHT_DOMAIN,
                DOMAIN,
                f"{casa_api.casa.networkId}-group-{group.groudId}",
            )
            if stale is not None:
                _LOGGER.info(
                    "Removing %s: group %s is not a light group", stale, group.name
                )
                registry.async_remove(stale)

    async_add_entities(light_entities + group_entities)


def _group_is_a_light(api: CasambiApi, group: Group) -> bool:
    """Return True if a Casambi group can be shown as a light.

    A group command goes to every member, so a group holding a louvre or a
    screen would move the motor on "turn off all lights": the screen's
    position is its dimmer. Such a group gets no light; the motors have
    their own covers, with the frost and weather locks. A group must also
    hold at least one light.
    """
    kinds = {api.unit_kind(unit) for unit in group.units}
    if kinds & {UnitKind.LOUVRE, UnitKind.SCREEN, UnitKind.UNSUPPORTED_MOTOR}:
        return False
    return any(
        capabilities(unit) != {ColorMode.UNKNOWN}
        for unit in group.units
        if api.unit_kind(unit) is UnitKind.LIGHT
    )


def capabilities(unit: Unit) -> set[ColorMode]:
    """Return the color modes a unit supports.

    ONOFF, BRIGHTNESS and UNKNOWN only stand in when nothing better is
    known; UNKNOWN means the unit has no usable mode at all.
    """
    supported: set[ColorMode] = set()
    unit_modes = [uc.type for uc in unit.unitType.controls]

    if UnitControlType.RGB in unit_modes and UnitControlType.WHITE in unit_modes:
        supported.add(ColorMode.RGBW)
    elif UnitControlType.RGB in unit_modes:
        supported.add(ColorMode.RGB)
    if UnitControlType.TEMPERATURE in unit_modes:
        supported.add(ColorMode.COLOR_TEMP)
    if UnitControlType.XY in unit_modes:
        supported.add(ColorMode.XY)

    if len(supported) == 0:
        if UnitControlType.DIMMER in unit_modes:
            supported.add(ColorMode.BRIGHTNESS)
        elif UnitControlType.ONOFF in unit_modes:
            supported.add(ColorMode.ONOFF)
        else:
            supported.add(ColorMode.UNKNOWN)

    return supported


class CasambiLight(CasambiEntity, LightEntity, metaclass=ABCMeta):
    """Defines a Casambi light entity base class.

    This class contains common functionality for units and groups.
    """

    def __init__(
        self, api: CasambiApi, description: TypedEntityDescription, obj: Group | Unit
    ) -> None:
        """Initialize a Casambi light entity base class."""

        # Effects and transitions aren't supported
        self._attr_supported_features = LightEntityFeature(0)

        self._attr_color_mode = self._mode_helper(self.supported_color_modes)

        self._obj: Group | Unit
        super().__init__(api, description, obj)

    def _mode_helper(self, modes: set[ColorMode] | set[str] | None) -> ColorMode:
        if modes:
            if ColorMode.RGBW in modes:
                return ColorMode.RGBW
            if ColorMode.RGB in modes:
                return ColorMode.RGB
            if ColorMode.XY in modes:
                return ColorMode.XY
            if ColorMode.COLOR_TEMP in modes:
                return ColorMode.COLOR_TEMP
            if ColorMode.BRIGHTNESS in modes:
                return ColorMode.BRIGHTNESS
            if ColorMode.ONOFF in modes:
                return ColorMode.ONOFF
        return ColorMode.UNKNOWN

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity of."""
        await self._async_casa_command(self._api.casa.setLevel(self._obj, 0))


class CasambiLightUnit(CasambiLight, CasambiUnitEntity):
    """Defines a Casambi light entity."""

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a Casambi light entity."""
        self._attr_supported_color_modes = capabilities(unit)

        temp_control = unit.unitType.get_control(UnitControlType.TEMPERATURE)
        if (
            temp_control is not None
            and temp_control.min is not None
            and temp_control.max is not None
        ):
            self._attr_min_color_temp_kelvin = temp_control.min
            self._attr_max_color_temp_kelvin = temp_control.max

        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="light")

        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def is_on(self) -> bool:
        """Return True if the unit is on."""
        return self._obj.is_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness of the unit."""
        unit = self._obj
        if unit.state is not None:
            return unit.state.dimmer
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the rgb color of the unit."""
        unit = self._obj
        if unit.state is not None:
            return unit.state.rgb
        return None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Return the rgbw color of the unit."""
        unit = self._obj
        if (
            unit.state is not None
            and unit.state.rgb is not None
            and unit.state.white is not None
        ):
            return (*unit.state.rgb, unit.state.white)
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        unit = self._obj
        if unit.state is not None:
            return unit.state.temperature
        return None

    @property
    def xy_color(self) -> tuple[float, float] | None:
        """Return the XY color value."""
        unit = self._obj
        if unit.state is not None:
            return unit.state.xy
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the unit."""
        unit = self._obj
        state = copy(unit.state)
        if not state:
            state = UnitState()

        # According to docs (https://developers.home-assistant.io/docs/core/entity/light#turn-on-light-device)
        # we only ever get a single color attribute but there may be other non-color ones.
        set_state = False
        if ATTR_BRIGHTNESS in kwargs:
            state.dimmer = kwargs[ATTR_BRIGHTNESS]
            set_state = True
        if ATTR_RGBW_COLOR in kwargs:
            state.rgb = kwargs[ATTR_RGBW_COLOR][:3]
            state.white = kwargs[ATTR_RGBW_COLOR][3]
            state.colorsource = ColorSource.RGB
            set_state = True
        if ATTR_RGB_COLOR in kwargs:
            state.rgb = kwargs[ATTR_RGB_COLOR]
            state.colorsource = ColorSource.RGB
            set_state = True
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            state.temperature = kwargs[ATTR_COLOR_TEMP_KELVIN]
            state.colorsource = ColorSource.TEMPERATURE
            set_state = True
        if ATTR_XY_COLOR in kwargs:
            state.xy = kwargs[ATTR_XY_COLOR]
            state.colorsource = ColorSource.XY
            set_state = True

        if set_state:
            await self._async_casa_command(self._api.casa.setUnitState(unit, state))
        else:
            await self._async_casa_command(self._api.casa.turnOn(self._obj))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the unit."""
        # Lights only supporting ONOFF need a state write instead of SetLevel.
        if self.color_mode == ColorMode.ONOFF:
            await self._async_casa_command(self._api.casa.turnOff(self._obj))
        else:
            await super().async_turn_off(**kwargs)


class CasambiLightGroup(CasambiLight, CasambiNetworkGroup):
    """Defines a Casambi group entity."""

    def __init__(self, api: CasambiApi, group: Group) -> None:
        """Initialize a Casambi group entity."""

        # Union of the modes of the members that are lights; relays and
        # sensors in the group ignore the light commands.
        lights = [u for u in group.units if api.unit_kind(u) is UnitKind.LIGHT]
        supported_modes: set[ColorMode] = set()
        for unit in lights:
            supported_modes |= capabilities(unit)

        # Color temperature is supported for groups; the range is the
        # intersection of the members' ranges so that every member can
        # reach any temperature the group offers.
        if ColorMode.COLOR_TEMP in supported_modes:
            minima: list[int] = []
            maxima: list[int] = []
            for unit in lights:
                control = unit.unitType.get_control(UnitControlType.TEMPERATURE)
                if control is None or control.min is None or control.max is None:
                    continue
                minima.append(control.min)
                maxima.append(control.max)
            if minima and maxima:
                self._attr_min_color_temp_kelvin = max(minima)
                self._attr_max_color_temp_kelvin = min(maxima)

        # HA doesn't allow combining UNKNOWN, ONOFF, or BRIGHTNESS with more
        # capable modes, which can happen for groups with mixed unit types.
        # Keep only the most capable modes in that case. Only groups with a
        # usable light are created, so something is always left; UNKNOWN on
        # its own would make Home Assistant refuse the entity.
        supported_modes.discard(ColorMode.UNKNOWN)
        if len(supported_modes) > 1:
            supported_modes.discard(ColorMode.ONOFF)
        if len(supported_modes) > 1:
            supported_modes.discard(ColorMode.BRIGHTNESS)

        if len(supported_modes) == 0:
            supported_modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = supported_modes

        desc = TypedEntityDescription(
            key=str(group.groudId), name=group.name, entity_type="light"
        )

        super().__init__(api, desc, group)
        # The state is read from the lights only: a relay in the group
        # ignores the light commands, and while it is on the group would
        # read as on with every light off and could not be switched off.
        self._lights = lights

    @property
    def available(self) -> bool:
        """Return True if any light of the group is available."""
        return self._api.available and any(unit.online for unit in self._lights)

    @property
    def is_on(self) -> bool:
        """Return True if any light in the group is on."""
        return any(u.is_on for u in self._lights)

    @property
    def brightness(self) -> int | None:
        """Return the brightness of the group.

        Taken from the first dimmable member that is on, so that a group
        whose first member is off doesn't read as on at brightness 0.
        """
        level = self._lit_level()
        if level is not None:
            return level
        for unit in self._lights:
            if (
                unit.unitType.get_control(UnitControlType.DIMMER)
                and unit.state is not None
            ):
                return unit.state.dimmer
        return None

    def _lit_level(self) -> int | None:
        """Return the level of the first dimmable member that is on."""
        for unit in self._lights:
            if (
                unit.unitType.get_control(UnitControlType.DIMMER)
                and unit.state is not None
                and unit.state.dimmer
                and unit.is_on
            ):
                return unit.state.dimmer
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the rgb color of the first fitting unit of the group."""
        for unit in self._lights:
            if (
                unit.unitType.get_control(UnitControlType.RGB)
                and unit.state is not None
            ):
                return unit.state.rgb
        return None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Return the rgw color of the first fitting unit of the group."""
        for unit in self._lights:
            if (
                unit.unitType.get_control(UnitControlType.RGB)
                and unit.unitType.get_control(UnitControlType.WHITE)
                and unit.state is not None
            ):
                return (*unit.state.rgb, unit.state.white)  # type: ignore[misc]
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin of the first fitting unit of the group."""
        for unit in self._lights:
            if (
                unit.unitType.get_control(UnitControlType.TEMPERATURE)
                and unit.state is not None
            ):
                return unit.state.temperature
        return None

    @property
    def xy_color(self) -> tuple[float, float] | None:
        """Return the XY color value of the first fitting unit of the group."""
        for unit in self._lights:
            if unit.unitType.get_control(UnitControlType.XY) and unit.state is not None:
                return unit.state.xy
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on all units in the group."""
        was_set = False
        if ATTR_BRIGHTNESS in kwargs:
            await self._async_casa_command(
                self._api.casa.setLevel(self._obj, kwargs[ATTR_BRIGHTNESS])
            )
            was_set = True
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            await self._async_casa_command(
                self._api.casa.setTemperature(self._obj, kwargs[ATTR_COLOR_TEMP_KELVIN])
            )
            was_set = True
        if ATTR_RGB_COLOR in kwargs:
            await self._async_casa_command(
                self._api.casa.setColor(self._obj, kwargs[ATTR_RGB_COLOR])
            )
            was_set = True
        elif ATTR_RGBW_COLOR in kwargs:
            rgb, w = kwargs[ATTR_RGBW_COLOR][:3], kwargs[ATTR_RGBW_COLOR][3]
            await self._async_casa_command(self._api.casa.setColor(self._obj, rgb))
            await self._async_casa_command(self._api.casa.setWhite(self._obj, w))
            was_set = True
        elif ATTR_XY_COLOR in kwargs:
            await self._async_casa_command(
                self._api.casa.setColorXY(self._obj, kwargs[ATTR_XY_COLOR])
            )
            was_set = True

        if not was_set:
            await self._async_casa_command(self._api.casa.turnOn(self._obj))
        elif ATTR_BRIGHTNESS not in kwargs:
            # Sync brightness for group so that everything turns on.
            # This might be a bit confusing because the rest isn't synced.
            # Only a member that is on gives a level worth copying; a level
            # read from one that is off would switch the whole group off.
            level = self._lit_level()
            if level is not None:
                await self._async_casa_command(
                    self._api.casa.setLevel(self._obj, level)
                )
            else:
                await self._async_casa_command(self._api.casa.turnOn(self._obj))
