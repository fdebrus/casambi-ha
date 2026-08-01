"""The Casambi Bluetooth integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import logging
from pathlib import Path
from typing import Final

from CasambiBt import Casambi, Group, Scene, Unit, UnitControlType
from CasambiBt._switch import SwitchEvent
from CasambiBt.errors import (
    AuthenticationError,
    BluetoothDeviceNotFoundError,
    BluetoothError,
    NetworkNotFoundError,
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

from .const import DOMAIN, EVENT_BUTTON, PLATFORMS

_LOGGER: Final = logging.getLogger(__name__)

type CasambiConfigEntry = ConfigEntry[CasambiApi]


async def async_setup_entry(hass: HomeAssistant, entry: CasambiConfigEntry) -> bool:
    """Set up Casambi Bluetooth from a config entry."""
    api = CasambiApi(hass, entry, entry.data[CONF_ADDRESS], entry.data[CONF_PASSWORD])
    await api.connect()
    entry.runtime_data = api

    # Create the network device before the platforms so that unit devices
    # can reference it via via_device.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, api.casa.networkId)},
        connections={(dr.CONNECTION_BLUETOOTH, api.address)},
        manufacturer="Casambi",
        model="Network",
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
    ) -> None:
        """Initialize a Casambi API."""

        self.hass = hass
        self.conf_entry = conf_entry
        self.address = address
        self.password = password
        self.casa: Casambi = Casambi(get_async_client(hass), get_cache_dir(hass))

        self._callback_map: dict[int, list[Callable[[Unit], None]]] = {}
        self._switch_event_callbacks: list[Callable[[SwitchEvent], None]] = []
        self._cancel_bluetooth_callback: Callable[[], None] | None = None
        self._reconnect_lock = asyncio.Lock()
        self._first_disconnect = True
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
        try:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if not device:
                raise NetworkNotFoundError  # noqa: TRY301

            # Register the handlers only once. The Casambi object keeps them
            # across reconnects, so registering on every connect would make
            # each handler fire multiple times after a reconnect.
            if not self._handlers_registered:
                self.casa.registerDisconnectCallback(self._casa_disconnect)
                self.casa.registerUnitChangedHandler(self._unit_changed_handler)
                self.casa.registerSwitchEventHandler(self._switch_event_handler)
                self._handlers_registered = True

            await self.casa.connect(device, self.password)
            if not self._first_disconnect:
                _LOGGER.info(
                    "Connection to the Casambi network %s was re-established",
                    self.address,
                )
            self._first_disconnect = True
            self._check_network_changes()
        except (BluetoothError, BluetoothDeviceNotFoundError) as err:
            raise ConfigEntryNotReady("Failed to use bluetooth") from err
        except NetworkNotFoundError as err:
            raise ConfigEntryNotReady(
                f"Network with address {self.address} wasn't found"
            ) from err
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                f"Failed to authenticate to network {self.address}"
            ) from err
        except Exception as err:  # pylint: disable=broad-except
            raise ConfigEntryError(
                f"Unexpected error creating network {self.address}"
            ) from err

        # Only register bluetooth callback after connection.
        # Otherwise we get an immediate callback and attempt two connections at once.
        if not self._cancel_bluetooth_callback:
            self._register_bluetooth_callback()

    @callback
    def _check_network_changes(self) -> None:
        """Reload the config entry if units were added or removed.

        The network configuration is re-fetched on every connect, so a
        reconnect may reveal units that were added or removed via the
        Casambi app. A reload recreates the entities to match.
        """
        snapshot = frozenset(unit.uuid for unit in self.casa.units)
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
        return self.casa.connected

    def get_units(
        self, control_types: list[UnitControlType] | None = None
    ) -> Iterable[Unit]:
        """Return all units in the network optionally filtered by control type."""

        if not control_types:
            return self.casa.units

        return filter(
            lambda u: any(uc.type in control_types for uc in u.unitType.controls),
            self.casa.units,
        )

    def get_groups(self) -> Iterable[Group]:
        """Return all groups in the network."""

        return self.casa.groups

    def get_scenes(self) -> Iterable[Scene]:
        """Return all scenes in the network."""

        return self.casa.scenes

    async def disconnect(self) -> None:
        """Disconnects from the controller and disables automatic reconnect."""
        async with self._reconnect_lock:
            if self._cancel_bluetooth_callback is not None:
                self._cancel_bluetooth_callback()
                self._cancel_bluetooth_callback = None

            # This needs to happen before we disconnect.
            # We don't want to be informed about disconnects initiated by us.
            if self._handlers_registered:
                self.casa.unregisterDisconnectCallback(self._casa_disconnect)

            try:
                await self.casa.disconnect()
            except Exception:
                _LOGGER.exception("Error during disconnect.")
            if self._handlers_registered:
                self.casa.unregisterUnitChangedHandler(self._unit_changed_handler)
                self.casa.unregisterSwitchEventHandler(self._switch_event_handler)
                self._handlers_registered = False

    @callback
    def _casa_disconnect(self) -> None:
        if self._first_disconnect:
            self._first_disconnect = False
            _LOGGER.info(
                "Connection to the Casambi network %s was lost; "
                "reconnecting in the background",
                self.address,
            )
            self.conf_entry.async_create_background_task(
                self.hass, self._delayed_reconnect(), "Delayed reconnect"
            )

    async def _delayed_reconnect(self) -> None:
        await asyncio.sleep(30)

        async with self._reconnect_lock:
            if self.casa.connected:
                return

        _LOGGER.debug("Starting delayed reconnect.")
        device = bluetooth.async_ble_device_from_address(self.hass, self.address)
        if device is not None:
            try:
                await self.try_reconnect()
            except Exception:
                _LOGGER.exception("Error during reconnect. This is not unusual.")
        else:
            _LOGGER.debug("Skipping reconnect. HA reports device not present.")

    async def try_reconnect(self) -> None:
        """Attemtps to reconnect to the Casambi network. Disconnects first to ensure a consitent state."""
        if self._reconnect_lock.locked():
            return

        # Use locking to ensure that only one reconnect can happen at a time.
        # Not sure if this is necessary.
        await self._reconnect_lock.acquire()

        try:
            try:
                await self.casa.disconnect()
            # HACK: This is a workaround for https://github.com/lkempf/casambi-bt-hass/issues/26
            # We don't actually need to disconnect except to clean up so this should be ok to ignore.
            except AttributeError:
                _LOGGER.debug("Unexpected failure during disconnect.")
            await self.connect()
        finally:
            self._reconnect_lock.release()

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
        if not self.casa.connected and service_info.connectable:
            self.conf_entry.async_create_background_task(
                self.hass, self.try_reconnect(), "Reconnect"
            )
