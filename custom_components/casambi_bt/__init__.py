"""The Casambi Bluetooth integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import inspect
import logging
from pathlib import Path
from typing import Any, Final, cast

from CasambiBt import Casambi, Group, Scene, Unit, UnitControlType
from CasambiBt._switch import SwitchEvent
from CasambiBt.errors import (
    AuthenticationError,
    BluetoothDeviceNotFoundError,
    BluetoothError,
    NetworkNotFoundError,
    ProtocolError,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.httpx_client import get_async_client

from .const import (
    CONF_DEMO,
    DOMAIN,
    EVENT_BUTTON,
    PLATFORMS,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_NO_DEVICE,
    RECONNECT_BACKOFF_START,
    RECONNECT_BACKOFF_STEP,
)
from .demo import DemoCasambi

_LOGGER: Final = logging.getLogger(__name__)

type CasambiConfigEntry = ConfigEntry[CasambiApi]


async def async_setup_entry(hass: HomeAssistant, entry: CasambiConfigEntry) -> bool:
    """Set up Casambi Bluetooth from a config entry."""
    api = CasambiApi(
        hass,
        entry,
        entry.data.get(CONF_ADDRESS, ""),
        entry.data.get(CONF_PASSWORD, ""),
        demo=entry.data.get(CONF_DEMO, False),
    )
    await api.connect()
    entry.runtime_data = api

    # Create the network device before the platforms so that unit devices
    # can reference it via via_device.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, api.casa.networkId)},
        connections=(set() if api.demo else {(dr.CONNECTION_BLUETOOTH, api.address)}),
        manufacturer="Casambi",
        model="Demo network" if api.demo else "Network",
        name=api.casa.networkName,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: CasambiConfigEntry
) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CasambiConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    await entry.runtime_data.disconnect()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removal of devices for units no longer in the network."""
    api = entry.runtime_data
    current: set[tuple[str, str]] = {(DOMAIN, unit.uuid) for unit in api.get_units()}
    current.add((DOMAIN, api.casa.networkId))
    return not any(identifier in current for identifier in device_entry.identifiers)


def get_cache_dir(hass: HomeAssistant) -> Path:
    """Return the cache dir that should be used by CasambiBt."""
    conf_path = Path(hass.config.config_dir)
    return conf_path / ".storage" / DOMAIN


class CasambiApi:
    """Defines a Casambi API."""

    def __init__(
        self,
        hass: HomeAssistant,
        conf_entry: CasambiConfigEntry,
        address: str,
        password: str,
        demo: bool = False,
    ) -> None:
        """Initialize a Casambi API."""

        self.hass = hass
        self.conf_entry = conf_entry
        self.address = address
        self.password = password
        self.demo = demo
        if demo:
            # The demo network implements the parts of the library API that
            # the integration uses, so the rest of the code is unchanged.
            # It never disconnects, so it needs no reconnect proxy.
            self._casa = cast("Casambi", DemoCasambi(hass))
            self.casa = self._casa
        else:
            self._casa = Casambi(get_async_client(hass), get_cache_dir(hass))
            # Entities talk to the network through the proxy so that a
            # write failing on bluetooth also triggers a reconnect.
            self.casa = cast("Casambi", CasambiProxy(self, self._casa))

        self._callback_map: dict[int, list[Callable[[Unit], None]]] = {}
        self._switch_event_callbacks: list[Callable[[SwitchEvent], None]] = []
        self._connection_callbacks: list[Callable[[], None]] = []

        # Shared state of the louvre automation entities.
        self.sun_offsets: dict[str, float] = {}
        self.temp_control: dict[str, bool] = {}
        self.temp_setpoints: dict[str, float] = {}
        self.rain_active = False
        self._cancel_bluetooth_callback: Callable[[], None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._handlers_registered = False
        self._unit_snapshot: frozenset[str] | None = None

    def _register_bluetooth_callback(self) -> None:
        self._cancel_bluetooth_callback = bluetooth.async_register_callback(
            self.hass,
            self._bluetooth_callback,
            {"address": self.address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def connect(self) -> None:
        """Connect to the Casmabi network."""
        if self.demo:
            demo = cast("DemoCasambi", self._casa)
            demo.registerUnitChangedHandler(self._unit_changed_handler)
            demo.registerSwitchEventHandler(self._switch_event_handler)
            self._handlers_registered = True
            await demo.connect()
            self._check_network_changes()
            return

        try:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if not device:
                raise BluetoothDeviceNotFoundError  # noqa: TRY301

            # Register the handlers only once. The Casambi object keeps them
            # across reconnects, so registering on every connect would make
            # each handler fire multiple times after a reconnect.
            if not self._handlers_registered:
                self._casa.registerDisconnectCallback(self._casa_disconnect)
                self._casa.registerUnitChangedHandler(self._unit_changed_handler)
                self._casa.registerSwitchEventHandler(self._switch_event_handler)
                self._handlers_registered = True

            await self._casa.connect(device, self.password)
            self._check_network_changes()
        except (BluetoothDeviceNotFoundError, NetworkNotFoundError) as err:
            raise ConfigEntryNotReady(
                f"Network with address {self.address} wasn't found"
            ) from err
        except BluetoothError as err:
            raise ConfigEntryNotReady("Failed to use bluetooth") from err
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                f"Failed to authenticate to network {self.address}"
            ) from err
        except Exception as err:  # pylint: disable=broad-except
            raise ConfigEntryError(
                f"Unexpected error creating network {self.address}"
            ) from err

        self._notify_connection_state()

        # Only register bluetooth callback after connection.
        # Otherwise we get an immediate callback and attempt two connections at once.
        if not self._cancel_bluetooth_callback:
            self._register_bluetooth_callback()

    async def reconnect(self) -> None:
        """Reconnect to the Casambi network, retrying with a growing backoff.

        This runs as a single long-lived background task: it keeps trying
        until the network answers again, the entry is unloaded (the task is
        cancelled) or the failure is one that retrying cannot fix.
        """
        backoff = RECONNECT_BACKOFF_START
        protocol_error = False

        while True:
            try:
                device = bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
                if not device:
                    raise BluetoothDeviceNotFoundError  # noqa: TRY301

                await self._casa.reconnect(device)
            except BluetoothError:
                _LOGGER.debug(
                    "Connecting failed due to bluetooth error. Retrying",
                    exc_info=True,
                )
            except BluetoothDeviceNotFoundError:
                # Home Assistant reports the device as out of range. We stay
                # registered for bluetooth callbacks, so retrying rarely is
                # enough to recover if that notification never arrives.
                backoff = max(backoff, RECONNECT_BACKOFF_NO_DEVICE)
            except ProtocolError:
                # Retry once to see whether the error is permanent.
                if protocol_error:
                    _LOGGER.exception(
                        "Giving up on reconnecting to the Casambi network %s "
                        "after repeated protocol errors",
                        self.address,
                    )
                    return
                _LOGGER.debug("Retrying once on protocol error", exc_info=True)
                protocol_error = True
            except AuthenticationError:
                # The password changed; retrying cannot help, ask the user.
                _LOGGER.error(
                    "Authentication to the Casambi network %s failed while "
                    "reconnecting",
                    self.address,
                )
                self.conf_entry.async_start_reauth(self.hass)
                return
            except asyncio.CancelledError:
                _LOGGER.debug("Reconnect task cancelled")
                raise
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "Unexpected error reconnecting to the Casambi network %s",
                    self.address,
                )
                return
            else:
                _LOGGER.info(
                    "Connection to the Casambi network %s was re-established",
                    self.address,
                )
                self._check_network_changes()
                self._notify_connection_state()
                return

            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_BACKOFF_MAX, backoff * RECONNECT_BACKOFF_STEP)

    @callback
    def _check_network_changes(self) -> None:
        """Reload the config entry if units were added or removed.

        The network configuration is re-fetched on every connect, so a
        reconnect may reveal units that were added or removed via the
        Casambi app. A reload recreates the entities to match.
        """
        snapshot = frozenset(unit.uuid for unit in self._casa.units)
        if self._unit_snapshot is None:
            self._unit_snapshot = snapshot
            return
        if snapshot != self._unit_snapshot:
            _LOGGER.info(
                "Units in the Casambi network %s changed; reloading", self.address
            )
            self._unit_snapshot = snapshot
            self.hass.config_entries.async_schedule_reload(self.conf_entry.entry_id)

    @property
    def available(self) -> bool:
        """Return True if the controller is available."""
        return self._casa.connected

    def get_units(
        self, control_types: list[UnitControlType] | None = None
    ) -> Iterable[Unit]:
        """Return all units in the network optionally filtered by control type."""

        if not control_types:
            return self._casa.units

        return filter(
            lambda u: any(uc.type in control_types for uc in u.unitType.controls),
            self._casa.units,
        )

    def get_groups(self) -> Iterable[Group]:
        """Return all groups in the network."""

        return self._casa.groups

    def get_scenes(self) -> Iterable[Scene]:
        """Return all scenes in the network."""

        return self._casa.scenes

    async def disconnect(self) -> None:
        """Disconnects from the controller and disables automatic reconnect."""
        await self._cancel_reconnect()

        if self._cancel_bluetooth_callback is not None:
            self._cancel_bluetooth_callback()
            self._cancel_bluetooth_callback = None

        # This needs to happen before we disconnect.
        # We don't want to be informed about disconnects initiated by us.
        # The demo network never disconnects on its own, so no disconnect
        # callback was registered for it.
        if self._handlers_registered and not self.demo:
            self._casa.unregisterDisconnectCallback(self._casa_disconnect)

        try:
            await self._casa.disconnect()
        except Exception:
            _LOGGER.exception("Error during disconnect.")
        if self._handlers_registered:
            self._casa.unregisterUnitChangedHandler(self._unit_changed_handler)
            self._casa.unregisterSwitchEventHandler(self._switch_event_handler)
            self._handlers_registered = False

    async def _cancel_reconnect(self) -> None:
        """Stop a running reconnect task and wait for it to finish."""
        task = self._reconnect_task
        if task is None or task.done():
            return

        task.cancel()
        try:
            async with asyncio.timeout(5):
                await task
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Got exception when cancelling reconnect. Ignoring", exc_info=True
            )

    @callback
    def _casa_disconnect(self) -> None:
        self._notify_connection_state()
        self._schedule_reconnect()

    @callback
    def _schedule_reconnect(self) -> None:
        """Start the reconnect task unless one is already running."""
        # A cancelled task means disconnect() ran, so the entry is going away
        # and a reconnect must never be scheduled again.
        if self._reconnect_task is not None and self._reconnect_task.cancelled():
            _LOGGER.debug(
                "Attempted to schedule a reconnect after the task was cancelled"
            )
            return

        if self._reconnect_task is None or self._reconnect_task.done():
            _LOGGER.info(
                "Connection to the Casambi network %s was lost; "
                "reconnecting in the background",
                self.address,
            )
            self._reconnect_task = self.conf_entry.async_create_background_task(
                self.hass, self.reconnect(), "Reconnect"
            )

    def register_unit_updates(self, unit: Unit, c: Callable[[Unit], None]) -> None:
        """Register a callback for unit updates.

        :param unit: The unit for which changes should be reported.
        :param c: The callback.
        """
        self._callback_map.setdefault(unit.deviceId, []).append(c)

    def unregister_unit_updates(self, unit: Unit, c: Callable[[Unit], None]) -> None:
        """Unregister a callback for unit updates.

        :param unit: The unit for which changes should no longer be reported.
        :param c: The callback.
        """
        self._callback_map[unit.deviceId].remove(c)

    def register_connection_updates(self, c: Callable[[], None]) -> None:
        """Register a callback for connection state changes."""
        self._connection_callbacks.append(c)

    def unregister_connection_updates(self, c: Callable[[], None]) -> None:
        """Unregister a callback for connection state changes."""
        self._connection_callbacks.remove(c)

    @callback
    def _notify_connection_state(self) -> None:
        """Tell the entities that the connection was lost or re-established."""
        for c in self._connection_callbacks:
            c()

    def register_switch_events(self, c: Callable[[SwitchEvent], None]) -> None:
        """Register a callback for switch (wall switch button) events."""
        self._switch_event_callbacks.append(c)

    def unregister_switch_events(self, c: Callable[[SwitchEvent], None]) -> None:
        """Unregister a callback for switch events."""
        self._switch_event_callbacks.remove(c)

    @callback
    def _unit_changed_handler(self, unit: Unit) -> None:
        if unit.deviceId not in self._callback_map:
            return
        for c in self._callback_map[unit.deviceId]:
            c(unit)

    @callback
    def _switch_event_handler(self, event: SwitchEvent) -> None:
        _LOGGER.debug(
            "Switch event: unit %i button %i %s",
            event.unit_id,
            event.button,
            event.event.name,
        )
        self.hass.bus.async_fire(
            EVENT_BUTTON,
            {
                "network_id": self.casa.networkId,
                "unit_id": event.unit_id,
                "button": event.button,
                "event_type": event.event.name.lower(),
            },
        )
        for c in self._switch_event_callbacks:
            c(event)

    @callback
    def _bluetooth_callback(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        if not self._casa.connected and service_info.connectable:
            self._schedule_reconnect()


class CasambiProxy:
    """Proxy write operations so that bluetooth errors trigger a reconnect.

    Every coroutine call to the library is wrapped: a bluetooth failure
    means the connection is gone even though the library has not reported
    a disconnect yet, so a reconnect is scheduled. Unlike upstream the
    error is re-raised, so the entity still reports the failed command
    back to the caller instead of silently doing nothing.
    """

    def __init__(self, api: CasambiApi, casa: Casambi) -> None:
        """Initialize a CasambiProxy."""
        self._api = api
        self._casa = casa

    def __getattr__(self, name: str) -> Any:
        """Wrap coroutine functions, pass everything else through."""
        attr = getattr(self._casa, name)

        if not inspect.iscoroutinefunction(attr):
            return attr

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await attr(*args, **kwargs)
            except BluetoothError:
                _LOGGER.info("Triggering a reconnect after a write failed")
                self._api._schedule_reconnect()  # noqa: SLF001
                raise

        return async_wrapper
