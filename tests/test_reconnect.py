"""Tests for the connection robustness of the Casambi API."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from CasambiBt.errors import AuthenticationError, BluetoothError, ProtocolError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt import CasambiApi
from custom_components.casambi_bt.const import (
    DOMAIN,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_NO_DEVICE,
    RECONNECT_BACKOFF_START,
    RECONNECT_BACKOFF_STEP,
)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN, SERVICE_TURN_OFF
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import LIGHT_UUID, NETWORK_ID


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> CasambiApi:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _disconnect_callback(mock_casambi: MagicMock):
    """Return the disconnect callback the integration registered."""
    mock_casambi.registerDisconnectCallback.assert_called_once()
    return mock_casambi.registerDisconnectCallback.call_args[0][0]


def _backoffs(sleep: AsyncMock) -> list[float]:
    """Return the reconnect delays, ignoring Home Assistant's own sleep(0)."""
    return [c.args[0] for c in sleep.await_args_list if c.args[0]]


async def test_reconnect_retries_until_success(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a failed reconnect is retried after the backoff."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    mock_casambi.reconnect.side_effect = [BluetoothError, BluetoothError, None]

    with patch(
        "custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        disconnect()
        await hass.async_block_till_done()

    assert mock_casambi.reconnect.await_count == 3
    # The delay doubles after every failed attempt.
    assert _backoffs(sleep) == [
        RECONNECT_BACKOFF_START,
        RECONNECT_BACKOFF_START * RECONNECT_BACKOFF_STEP,
    ]


async def test_reconnect_backoff_is_capped(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the growing backoff never exceeds the maximum."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    mock_casambi.reconnect.side_effect = [BluetoothError] * 20 + [None]

    with patch(
        "custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        disconnect()
        await hass.async_block_till_done()

    delays = _backoffs(sleep)
    assert max(delays) == RECONNECT_BACKOFF_MAX
    assert delays[-1] == RECONNECT_BACKOFF_MAX


async def test_reconnect_waits_longer_without_device(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that an out-of-range device raises the backoff immediately."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False

    with (
        patch(
            "custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            side_effect=[None, mock_bluetooth],
        ),
    ):
        disconnect()
        await hass.async_block_till_done()

    assert _backoffs(sleep) == [RECONNECT_BACKOFF_NO_DEVICE]
    mock_casambi.reconnect.assert_awaited_once()


async def test_reconnect_retries_protocol_error_once(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a repeated protocol error ends the reconnect attempts."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    mock_casambi.reconnect.side_effect = ProtocolError

    with patch("custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock):
        disconnect()
        await hass.async_block_till_done()

    # Tried once, retried once, then gave up instead of looping forever.
    assert mock_casambi.reconnect.await_count == 2


async def test_reconnect_auth_failure_starts_reauth(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a password change during a reconnect asks the user."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    mock_casambi.reconnect.side_effect = AuthenticationError

    with patch("custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock):
        disconnect()
        await hass.async_block_till_done()

    mock_casambi.reconnect.assert_awaited_once()
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]
    assert len(flows) == 1


async def test_only_one_reconnect_task(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that repeated disconnects don't stack up reconnect tasks."""
    api = await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hang(_device: object) -> None:
        started.set()
        await release.wait()

    mock_casambi.reconnect.side_effect = _hang

    disconnect()
    async with asyncio.timeout(5):
        await started.wait()

    first_task = api._reconnect_task  # noqa: SLF001
    assert first_task is not None

    # A second disconnect (or a bluetooth advertisement) must reuse the task.
    disconnect()
    assert api._reconnect_task is first_task  # noqa: SLF001

    release.set()
    await hass.async_block_till_done()
    mock_casambi.reconnect.assert_awaited_once()


async def test_unload_cancels_reconnect(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that unloading the entry stops a running reconnect."""
    api = await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    started = asyncio.Event()

    async def _hang(_device: object) -> None:
        started.set()
        await asyncio.Event().wait()

    mock_casambi.reconnect.side_effect = _hang

    disconnect()
    async with asyncio.timeout(5):
        await started.wait()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    task = api._reconnect_task  # noqa: SLF001
    assert task is not None
    assert task.done()


async def test_write_failure_triggers_reconnect(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a bluetooth error on a write reconnects and still reports."""
    await _setup(hass, mock_config_entry)

    entity_id = er.async_get(hass).async_get_entity_id(
        LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LIGHT_UUID}-light"
    )
    assert entity_id is not None

    # The library has not noticed the connection is gone yet, so the entity
    # is still available and the write is attempted.
    mock_casambi.setLevel.side_effect = BluetoothError

    with patch("custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                LIGHT_DOMAIN,
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        await hass.async_block_till_done()

    # The failed write is reported to the caller *and* starts a reconnect.
    mock_casambi.reconnect.assert_awaited_once()


async def test_entities_follow_the_connection_state(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that losing the connection updates the entities right away."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    status = next(
        s.entity_id
        for s in hass.states.async_all("binary_sensor")
        if "status" in s.entity_id
    )
    light = er.async_get(hass).async_get_entity_id(
        LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LIGHT_UUID}-light"
    )
    assert light is not None
    assert hass.states.get(status).state == "on"
    assert hass.states.get(light).state == "on"

    mock_casambi.connected = False
    started = asyncio.Event()

    async def _hang(_device: object) -> None:
        started.set()
        await asyncio.Event().wait()

    mock_casambi.reconnect.side_effect = _hang

    disconnect()
    async with asyncio.timeout(5):
        await started.wait()

    assert hass.states.get(status).state == "off"
    assert hass.states.get(light).state == "unavailable"
