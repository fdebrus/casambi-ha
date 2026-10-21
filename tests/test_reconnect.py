"""Tests for the connection robustness of the Casambi API."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from CasambiBt.errors import AuthenticationError, BluetoothError, ProtocolError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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

from .conftest import LIGHT_UUID, NETWORK_ID, setup_entry as _setup


def _disconnect_callback(mock_casambi: MagicMock):
    """Return the disconnect callback the integration registered."""
    mock_casambi.registerDisconnectCallback.assert_called_once()
    return mock_casambi.registerDisconnectCallback.call_args[0][0]


def _outcomes(mock_casambi: MagicMock, *outcomes: type[Exception] | None):
    """Return a reconnect side effect: raise each error, connect on None."""
    pending = list(outcomes)

    async def _reconnect(_device: object) -> None:
        outcome = pending.pop(0)
        if outcome is not None:
            raise outcome
        mock_casambi.connected = True

    return _reconnect


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
    mock_casambi.reconnect.side_effect = _outcomes(
        mock_casambi, BluetoothError, BluetoothError, None
    )

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
    mock_casambi.reconnect.side_effect = _outcomes(
        mock_casambi, *[BluetoothError] * 20, None
    )

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
    """Test that repeated protocol errors slow the retries down, not stop them.

    Giving up would only move the retries to the advertisement callback,
    which starts a fresh task without any backoff.
    """
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)

    mock_casambi.connected = False
    mock_casambi.reconnect.side_effect = _outcomes(
        mock_casambi, ProtocolError, ProtocolError, ProtocolError, None
    )

    with patch(
        "custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        disconnect()
        await hass.async_block_till_done()

    assert mock_casambi.reconnect.await_count == 4
    # The first protocol error is retried quickly, the next ones at the
    # slowest pace.
    assert _backoffs(sleep) == [
        RECONNECT_BACKOFF_START,
        RECONNECT_BACKOFF_MAX,
        RECONNECT_BACKOFF_MAX,
    ]
    assert mock_config_entry.runtime_data.available


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

    # The library gave the link up inside the write, without running its
    # disconnect callbacks, so the entity was still available.
    async def _failing_write(*_args: object) -> None:
        mock_casambi.connected = False
        raise BluetoothError

    mock_casambi.setLevel.side_effect = _failing_write

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


async def test_status_goes_off_while_the_library_still_reports_connected(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the status when the library calls back before updating its state.

    The library runs the disconnect callbacks while it still considers
    itself authenticated and only clears that afterwards, so the status
    must not be read from it at that moment.
    """
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)
    status = next(
        s.entity_id
        for s in hass.states.async_all("binary_sensor")
        if "status" in s.entity_id
    )

    started = asyncio.Event()

    async def _hang(_device: object) -> None:
        started.set()
        await asyncio.Event().wait()

    mock_casambi.reconnect.side_effect = _hang

    assert mock_casambi.connected is True
    disconnect()
    mock_casambi.connected = False
    async with asyncio.timeout(5):
        await started.wait()

    assert hass.states.get(status).state == "off"
    assert not mock_config_entry.runtime_data.available


async def test_long_outage_is_logged(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a reconnect failing for minutes is reported, and its end."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)
    status = next(
        s.entity_id
        for s in hass.states.async_all("binary_sensor")
        if "status" in s.entity_id
    )
    mock_casambi.connected = False

    def _reconnected(_device: object) -> None:
        mock_casambi.connected = True

    mock_casambi.reconnect.side_effect = _reconnected

    with (
        patch("custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            side_effect=[None, mock_bluetooth],
        ),
        patch(
            "custom_components.casambi_bt.monotonic",
            side_effect=[0.0, 400.0, 400.0, 460.0],
        ),
    ):
        disconnect()
        await hass.async_block_till_done()

    assert (
        "has been unreachable for 6 minutes (no Bluetooth adapter or proxy in "
        "range sees it); still retrying" in caplog.text
    )
    assert "is reachable again after 7 minutes" in caplog.text
    assert hass.states.get(status).state == "on"


async def test_reconnect_waits_for_the_library_to_tear_down(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a disconnect announced while the library still reports connected.

    The reconnect task starts eagerly inside the disconnect callback. The
    library returns from reconnect() at once while it considers itself
    connected, which used to end the reconnect loop without a connection.
    """
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)
    seen: list[bool] = []

    async def _library_reconnect(_device: object) -> None:
        seen.append(mock_casambi.connected)
        if mock_casambi.connected:
            return
        mock_casambi.connected = True

    mock_casambi.reconnect.side_effect = _library_reconnect

    disconnect()
    mock_casambi.connected = False
    await hass.async_block_till_done()

    assert seen == [False]
    assert mock_casambi.connected is True
    assert mock_config_entry.runtime_data.available


async def test_reconnect_without_connection_is_retried(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a reconnect returning without a connection is not trusted."""
    await _setup(hass, mock_config_entry)
    disconnect = _disconnect_callback(mock_casambi)
    mock_casambi.connected = False
    attempts = 0

    async def _flaky(_device: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            mock_casambi.connected = True

    mock_casambi.reconnect.side_effect = _flaky

    with patch(
        "custom_components.casambi_bt.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        disconnect()
        await hass.async_block_till_done()

    assert attempts == 2
    assert _backoffs(sleep) == [RECONNECT_BACKOFF_START]
    assert mock_config_entry.runtime_data.available


async def test_write_failure_while_connected_does_not_reconnect(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a write timeout on a live link is reported but not acted on.

    The library's reconnect returns at once while it still considers itself
    connected, so scheduling one only logged a lost/re-established pair and
    refreshed every entity for nothing.
    """
    await _setup(hass, mock_config_entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LIGHT_UUID}-light"
    )
    assert entity_id is not None
    mock_casambi.setLevel.side_effect = BluetoothError

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    await hass.async_block_till_done()

    mock_casambi.reconnect.assert_not_awaited()
    assert mock_config_entry.runtime_data.available


async def test_no_reconnect_once_disconnecting(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a disconnect reported during unload starts no reconnect."""
    await _setup(hass, mock_config_entry)
    api = mock_config_entry.runtime_data
    disconnect = _disconnect_callback(mock_casambi)

    async def _late_disconnect() -> None:
        # The library announces the drop while disconnect() is tearing down.
        mock_casambi.connected = False
        disconnect()

    mock_casambi.disconnect.side_effect = _late_disconnect

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mock_casambi.reconnect.assert_not_awaited()
    assert api._reconnect_task is None  # noqa: SLF001
