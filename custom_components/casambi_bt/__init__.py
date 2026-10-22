"""The Casambi Bluetooth integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import inspect
import logging
from pathlib import Path
import re
from time import monotonic
from typing import Any, Final, cast

from CasambiBt import Casambi, Group, Scene, Unit, UnitControlType
from CasambiBt._switch import SwitchEvent
from CasambiBt.errors import (
    AuthenticationError,
    BluetoothDeviceNotFoundError,
    BluetoothError,
    NetworkNotFoundError,
    NetworkUpdateError,
    ProtocolError,
)
import httpx

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.httpx_client import get_async_client

from . import switch_decoder
from .classify import UnitKind, classify_unit
from .const import (
    CONF_FROST_LOCK_TEMPERATURE,
    CONF_TEMPERATURE_ENTITY,
    DEFAULT_FROST_LOCK_TEMPERATURE,
    DOMAIN,
    EVENT_BUTTON,
    PLATFORMS,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_NO_DEVICE,
    RECONNECT_BACKOFF_START,
    RECONNECT_BACKOFF_STEP,
    RECONNECT_WARN_AFTER,
    entry_option,
)
from .readings import entity_temperature_c

_LOGGER: Final = logging.getLogger(__name__)

# The library reports every non-OK answer of the Casambi cloud at login as
# an AuthenticationError("Login failed: <status>..."); only a rejection
# means the password is wrong, an outage is retried like any other.
_LOGIN_STATUS: Final = re.compile(r"Login failed: (\d{3})")
_TRANSIENT_LOGIN_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})


def _login_outage(err: AuthenticationError) -> bool:
    """Return True if the login failed because of the cloud, not the password."""
    match = _LOGIN_STATUS.search(str(err))
    return match is not None and int(match.group(1)) in _TRANSIENT_LOGIN_STATUSES


# Wired push-button interfaces only report on the input stream, which the
# library's decoder drops for presses and releases.
switch_decoder.install()

type CasambiConfigEntry = ConfigEntry[CasambiApi]


async def async_setup_entry(hass: HomeAssistant, entry: CasambiConfigEntry) -> bool:
    """Set up Casambi Bluetooth from a config entry."""
    api = CasambiApi(
        hass,
        entry,
        entry.data.get(CONF_ADDRESS, ""),
        entry.data.get(CONF_PASSWORD, ""),
    )
    await api.connect()
    entry.runtime_data = api

    # Create the network device before the platforms so that unit devices
    # can link to it by id.
    device_registry = dr.async_get(hass)
    network_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, api.casa.networkId)},
        connections={(dr.CONNECTION_BLUETOOTH, api.address)},
        manufacturer="Casambi",
        model="Network",
        name=api.casa.networkName,
    )
    api.network_device_id = network_device.id

    _flag_unsupported_motors(hass, entry, api)

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
    _clear_unsupported_motors(hass, entry)

    return unload_ok


def _unsupported_issue_prefix(entry: CasambiConfigEntry) -> str:
    return f"unsupported_motor_{entry.entry_id}_"


@callback
def _flag_unsupported_motors(
    hass: HomeAssistant, entry: CasambiConfigEntry, api: CasambiApi
) -> None:
    """Raise a repair issue for each motor this integration must not drive."""
    for unit in api.get_units():
        if classify_unit(unit) is not UnitKind.UNSUPPORTED_MOTOR:
            continue
        _LOGGER.warning(
            "%s (%s) is a motor with an untested control layout; "
            "no entities are created for it",
            unit.name,
            unit.unitType.model,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{_unsupported_issue_prefix(entry)}{unit.uuid}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unsupported_motor",
            translation_placeholders={
                "name": unit.name,
                "model": unit.unitType.model,
            },
        )


@callback
def _clear_unsupported_motors(hass: HomeAssistant, entry: CasambiConfigEntry) -> None:
    """Remove this entry's unsupported-motor issues."""
    prefix = _unsupported_issue_prefix(entry)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


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
        self._casa = Casambi(get_async_client(hass), get_cache_dir(hass))
        # Entities talk to the network through the proxy so that a write
        # failing on bluetooth also triggers a reconnect.
        self.casa = cast("Casambi", CasambiProxy(self, self._casa))

        self._callback_map: dict[int, list[Callable[[Unit], None]]] = {}
        self._switch_event_callbacks: list[Callable[[SwitchEvent], None]] = []
        self._connection_callbacks: list[Callable[[], None]] = []

        # Shared state of the louvre automation entities.
        self.sun_offsets: dict[str, float] = {}
        self.temp_control: dict[str, bool] = {}
        self.temp_setpoints: dict[str, float] = {}
        self.rain_active = False
        self.wind_active = False
        self.frost_lock: dict[str, bool] = {}
        # Last numeric reading of the temperature source, for the frost lock.
        self.last_temperature: float | None = None
        # Registry id of the network device, set once it has been created so
        # that unit devices can be linked to it.
        self.network_device_id: str | None = None
        self._cancel_bluetooth_callback: Callable[[], None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        # Whether the link is up as far as the integration knows. The
        # library still reports itself connected while it runs the
        # disconnect callbacks, so its own flag can't be read there.
        self._link_up = False
        # Set once disconnect() starts: no reconnect may be scheduled after.
        self._closing = False
        self._handlers_registered = False

    def _register_bluetooth_callback(self) -> None:
        self._cancel_bluetooth_callback = bluetooth.async_register_callback(
            self.hass,
            self._bluetooth_callback,
            {"address": self.address, "connectable": True},
            # Seeing the network's advertisements is enough to know it is
            # back; no scan responses are needed.
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    async def connect(self) -> None:
        """Connect to the Casmabi network."""
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
            self._link_up = True
        except (BluetoothDeviceNotFoundError, NetworkNotFoundError) as err:
            raise ConfigEntryNotReady(
                f"Network with address {self.address} wasn't found"
            ) from err
        except BluetoothError as err:
            raise ConfigEntryNotReady("Failed to use bluetooth") from err
        except AuthenticationError as err:
            if _login_outage(err):
                raise ConfigEntryNotReady(
                    f"The Casambi cloud did not answer the login: {err}"
                ) from err
            raise ConfigEntryAuthFailed(
                f"Failed to authenticate to network {self.address}"
            ) from err
        except (NetworkUpdateError, httpx.RequestError) as err:
            # The cloud answered badly or not at all while the network
            # configuration was being refreshed (the library only keeps
            # going offline when the login itself can't reach it).
            raise ConfigEntryNotReady(
                f"The Casambi cloud could not be reached: {err}"
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
        started = monotonic()
        warned = False
        reason = ""

        # The task starts eagerly, inside the library's disconnect callback,
        # where the library still reports itself connected and would return
        # from reconnect() at once. Let it finish tearing down first.
        await asyncio.sleep(0)

        while True:
            try:
                device = bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
                if not device:
                    raise BluetoothDeviceNotFoundError  # noqa: TRY301

                await self._casa.reconnect(device)
                if not self._casa.connected:
                    raise BluetoothError(  # noqa: TRY301
                        "reconnect returned without a connection"
                    )
            except BluetoothError as err:
                _LOGGER.debug(
                    "Connecting failed due to bluetooth error. Retrying",
                    exc_info=True,
                )
                reason = f"Bluetooth error: {err}"
            except BluetoothDeviceNotFoundError:
                # Home Assistant reports the device as out of range. We stay
                # registered for bluetooth callbacks, so retrying rarely is
                # enough to recover if that notification never arrives.
                backoff = max(backoff, RECONNECT_BACKOFF_NO_DEVICE)
                reason = "no Bluetooth adapter or proxy in range sees it"
            except ProtocolError as err:
                # A garbled handshake, or the network key changed (the
                # library only logs in on the first connect, so that never
                # surfaces as an authentication error here). Retrying at the
                # slowest pace beats giving up: with the task gone, every
                # advertisement would start a fresh one without any backoff.
                if protocol_error:
                    backoff = max(backoff, RECONNECT_BACKOFF_MAX)
                protocol_error = True
                reason = f"protocol error: {err}"
                _LOGGER.debug("Reconnect failed with a protocol error", exc_info=True)
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
                if warned:
                    _LOGGER.warning(
                        "The Casambi network %s is reachable again after %d minutes",
                        self.address,
                        (monotonic() - started) // 60,
                    )
                else:
                    _LOGGER.info(
                        "Connection to the Casambi network %s was re-established",
                        self.address,
                    )
                self._link_restored()
                return

            if not warned and monotonic() - started >= RECONNECT_WARN_AFTER:
                warned = True
                _LOGGER.warning(
                    "The Casambi network %s has been unreachable for %d minutes "
                    "(%s); still retrying",
                    self.address,
                    (monotonic() - started) // 60,
                    reason or "connection failed",
                )

            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_BACKOFF_MAX, backoff * RECONNECT_BACKOFF_STEP)

    @property
    def available(self) -> bool:
        """Return True if the controller is available."""
        return self._link_up and self._casa.connected

    def frost_lock_temperature(self, unit: Unit) -> float | None:
        """Return the temperature that locks a louvre against frost, or None.

        The louvres are locked while their frost lock is on and the
        temperature source reads below the configured threshold. When the
        source can't tell (unavailable, not numeric), its last reading
        counts, so a dropped weather station doesn't unlock them.
        """
        if not self.frost_lock.get(unit.uuid, True):
            return None
        entity_id = entry_option(self.conf_entry, CONF_TEMPERATURE_ENTITY, None)
        if not entity_id:
            return None
        temperature = entity_temperature_c(self.hass.states.get(entity_id))
        if temperature is not None:
            self.last_temperature = temperature
        if self.last_temperature is None:
            return None
        threshold = entry_option(
            self.conf_entry,
            CONF_FROST_LOCK_TEMPERATURE,
            DEFAULT_FROST_LOCK_TEMPERATURE,
        )
        if self.last_temperature < threshold:
            return self.last_temperature
        return None

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
        self._closing = True
        await self._cancel_reconnect()
        self._link_up = False

        if self._cancel_bluetooth_callback is not None:
            self._cancel_bluetooth_callback()
            self._cancel_bluetooth_callback = None

        # This needs to happen before we disconnect.
        # We don't want to be informed about disconnects initiated by us.
        if self._handlers_registered:
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
        self._link_up = False
        self._notify_connection_state()
        self._schedule_reconnect()

    @callback
    def _link_restored(self) -> None:
        """Mark the link up and tell the entities."""
        self._link_up = True
        self._notify_connection_state()

    @callback
    def _schedule_reconnect(self) -> None:
        """Start the reconnect task unless one is already running."""
        if self._closing:
            _LOGGER.debug("Attempted to schedule a reconnect while disconnecting")
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
        _service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        # Only connectable advertisements match the registered callback.
        if not self.available:
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
                result = await attr(*args, **kwargs)
            except (BluetoothError, BluetoothDeviceNotFoundError, ProtocolError):
                # A write can fail while the library still considers itself
                # connected (a timeout, say); its reconnect would then return
                # at once. Only act when it has given the connection up
                # without running the disconnect callbacks: the link is down
                # for the entities as much as after a reported disconnect.
                if not self._casa.connected:
                    _LOGGER.info("The link is gone after a write failed")
                    self._api._casa_disconnect()  # noqa: SLF001
                raise
            # The library reconnects on its own inside a write; the entities
            # need not wait for the reconnect task's next attempt to know.
            if not self._api.available and self._casa.connected:
                self._api._link_restored()  # noqa: SLF001
            return result

        return async_wrapper
