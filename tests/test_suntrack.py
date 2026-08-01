"""Tests for the louvre sun tracking feature."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.casambi_bt.const import DOMAIN
from custom_components.casambi_bt.suntrack import compute_louvre_angle
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import WINSOL_LOUVRE_UUID


@pytest.mark.parametrize(
    ("elevation", "azimuth", "louvre_azimuth", "offset", "expected"),
    [
        # Sun due south at 30 deg elevation, south-facing pergola:
        # profile angle 30 -> slats at 60 deg.
        (30.0, 180.0, 180, 0.0, 60.0),
        # Higher sun -> flatter slats.
        (60.0, 180.0, 180, 0.0, 30.0),
        # Offset toward more sun.
        (60.0, 180.0, 180, 20.0, 50.0),
        # Sun below horizon -> leave the louvres alone.
        (-5.0, 180.0, 180, 0.0, None),
        # Sun behind the pergola -> no direct sun.
        (30.0, 0.0, 180, 0.0, None),
        # Very low sun at an angle: clamped to the hardware maximum.
        (2.0, 180.0, 180, 80.0, 142.0),
    ],
)
def test_compute_louvre_angle(
    elevation: float,
    azimuth: float,
    louvre_azimuth: int,
    offset: float,
    expected: float | None,
) -> None:
    """Test the louvre angle geometry."""
    result = compute_louvre_angle(elevation, azimuth, louvre_azimuth, offset)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result == pytest.approx(expected, abs=0.1)


async def test_sun_tracking_switch(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that enabling sun tracking positions the louvres."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    switch_id = registry.async_get_entity_id(
        "switch",
        DOMAIN,
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-sun-tracking",
    )
    assert switch_id is not None
    offset_id = registry.async_get_entity_id(
        "number",
        DOMAIN,
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-sun-offset",
    )
    assert offset_id is not None

    # Sun at 45 deg elevation due south; default south-facing pergola:
    # angle 45 deg -> raw round(45 / 142 * 255) = 81.
    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {ATTR_ENTITY_ID: switch_id},
            blocking=True,
        )

    mock_casambi.setSlider.assert_awaited_once()
    _, raw = mock_casambi.setSlider.await_args.args
    assert raw == 81

    # Changing the offset and waiting for the next interval repositions.
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: offset_id, "value": 20},
        blocking=True,
    )
    mock_casambi.setSlider.reset_mock()

    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()

    mock_casambi.setSlider.assert_awaited_once()
    _, raw = mock_casambi.setSlider.await_args.args
    # angle 65 deg -> raw round(65 / 142 * 255) = 117.
    assert raw == 117

    # Turning the switch off stops the tracking.
    await hass.services.async_call(
        "switch",
        "turn_off",
        {ATTR_ENTITY_ID: switch_id},
        blocking=True,
    )
    mock_casambi.setSlider.reset_mock()
    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=12))
        await hass.async_block_till_done()
    mock_casambi.setSlider.assert_not_awaited()


async def test_sun_tracking_deadband(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that small changes don't move the motor."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    switch_id = registry.async_get_entity_id(
        "switch",
        DOMAIN,
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-sun-tracking",
    )
    assert switch_id is not None

    # The louvre fixture starts with slider 128; a target within the
    # deadband (raw 130 -> angle ~72.4 deg) must not move the motor.
    # angle = raw * 142 / 255 -> 130 raw = 72.39 deg; profile = 90 - 72.39.
    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(17.61, 180.0),
    ):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {ATTR_ENTITY_ID: switch_id},
            blocking=True,
        )

    mock_casambi.setSlider.assert_not_awaited()
