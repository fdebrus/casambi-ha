"""The connection to a Casambi network.

The hub owns the casambi-bt object and its Bluetooth link: it connects,
keeps reconnecting when the link drops, and hands the network's pushes
(unit states, wall button events, link up and down) to whoever subscribed.
Commands go through ``async_command`` so that a failed write is seen by the
link bookkeeping and reaches the caller as a Home Assistant error.

A BLE mesh link drops often, so losing it is normal operation: one
background task retries with a growing backoff until the network answers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import logging
from time import monotonic
from typing import Any, Final

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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.httpx_client import get_async_client

from .classify import UnitKind, classify_unit
from .const import (
    DOMAIN,
    EVENT_BUTTON,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_NO_DEVICE,
    RECONNECT_BACKOFF_START,
    RECONNECT_BACKOFF_STEP,
    RECONNECT_WARN_AFTER,
    STALE_LINK_RELEASE_TIMEOUT,
)
from .library import (
    LINK_ERRORS,
    async_release_stale_client,
    cloud_outage,
    get_cache_dir,
    login_outage,
)
from .platform_packets import decode_platform_packet

_LOGGER: Final = logging.getLogger(__name__)

type UnitListener = Callable[[Unit], None]
type ConnectionListener = Callable[[], None]
type ButtonListener = Callable[[SwitchEvent], None]


class CasambiHub:
    """The link to one Casambi network and what it pushes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the hub of a config entry."""
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data.get(CONF_ADDRESS, "")
        self._password: str = entry.data.get(CONF_PASSWORD, "")
        self.casa = Casambi(get_async_client(hass), get_cache_dir(hass))
        # Registry id of the network device, set once it has been created
        # so that unit devices can be linked to it.
        self.network_device_id: str | None = None

        self._unit_listeners: dict[int, list[UnitListener]] = {}
        self._connection_listeners: list[ConnectionListener] = []
        self._button_listeners: list[ButtonListener] = []
        # A unit's kind follows from its type, which is fixed while the
        # entry is loaded; it is asked for on every push.
        self._kinds: dict[str, UnitKind] = {}
        # The latest reading of each sensor platform packet: uuid ->
        # packet -> value, decoded from the pushed states (see
        # platform_packets for why not from the library's sensor_cache).
        self._platform_readings: dict[str, dict[int, int]] = {}

        # Whether the link is up as far as the integration knows. The
        # library still reports itself connected while it runs the
        # disconnect callbacks, so its own flag can't be read there.
        self._link_up = False
        # Set once the hub is told to stop: no reconnect may start after.
        self._closing = False
        self._handlers_registered = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._cancel_bluetooth_callback: CALLBACK_TYPE | None = None
        # Set by an advertisement of the network, or by a write that brought
        # the link back, to cut the reconnect task's wait short.
        self._wake = asyncio.Event()
        # Whether an advertisement may cut the wait short: only while the
        # last attempt found no device, so it cannot bypass the backoff of
        # a network that is seen but refuses the connection.
        self._wake_on_advertisement = False

    # --- The network ---------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True while the network can be reached."""
        return self._link_up and self.casa.connected

    @property
    def network_id(self) -> str:
        """Return the id of the network, the base of every unique id."""
        return self.casa.networkId

    @property
    def network_name(self) -> str:
        """Return the name the network was given in the Casambi app."""
        return self.casa.networkName

    def get_units(
        self, control_types: Iterable[UnitControlType] | None = None
    ) -> list[Unit]:
        """Return the units, optionally only those with one of the controls."""
        if control_types is None:
            return list(self.casa.units)
        wanted = set(control_types)
        return [
            unit
            for unit in self.casa.units
            if any(control.type in wanted for control in unit.unitType.controls)
        ]

    def get_groups(self) -> list[Group]:
        """Return the groups of the network."""
        return list(self.casa.groups)

    def get_scenes(self) -> list[Scene]:
        """Return the scenes of the network."""
        return list(self.casa.scenes)

    def kind(self, unit: Unit) -> UnitKind:
        """Return what kind of hardware a unit is, worked out once."""
        kind = self._kinds.get(unit.uuid)
        if kind is None:
            kind = self._kinds[unit.uuid] = classify_unit(unit)
        return kind

    def platform_reading(self, unit: Unit, packet: int) -> int | None:
        """Return the latest value of a sensor platform packet, if any."""
        return self._platform_readings.get(unit.uuid, {}).get(packet)

    def platform_readings(self, unit: Unit) -> dict[int, int]:
        """Return the latest value of every packet of a sensor platform."""
        return dict(self._platform_readings.get(unit.uuid, {}))

    # --- Subscriptions -------------------------------------------------

    @callback
    def subscribe_unit(self, unit: Unit, listener: UnitListener) -> CALLBACK_TYPE:
        """Call ``listener`` with the unit on each of its state pushes."""
        listeners = self._unit_listeners.setdefault(unit.deviceId, [])
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    @callback
    def subscribe_connection(self, listener: ConnectionListener) -> CALLBACK_TYPE:
        """Call ``listener`` when the link drops or comes back."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    @callback
    def subscribe_buttons(self, listener: ButtonListener) -> CALLBACK_TYPE:
        """Call ``listener`` with each wall button event."""
        self._button_listeners.append(listener)
        return lambda: self._button_listeners.remove(listener)

    # --- Commands ------------------------------------------------------

    async def async_command[**P](
        self,
        method: Callable[P, Awaitable[Any]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        """Send one command of the library to the network.

        A write that fails on Bluetooth means the connection is gone even
        though the library has not reported a disconnect yet, so the link
        state is brought up to date before the error reaches the caller as
        a HomeAssistantError.
        """
        try:
            await method(*args, **kwargs)
        except Exception as err:
            # A write can fail while the library still considers itself
            # connected (a timeout, say); its reconnect would then return at
            # once. Only act when it has given the connection up without
            # running the disconnect callbacks. Whatever was raised: a proxy
            # timeout inside the library's own reconnect is a bare
            # TimeoutError.
            if not self.casa.connected:
                if self._link_up:
                    _LOGGER.info("The link is gone after a write failed")
                    self._handle_disconnect()
                else:
                    self._schedule_reconnect()
            elif not self.available:
                # The library reconnected inside the write, then the send
                # failed: the link itself is back.
                self._link_restored()
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_error"
            ) from err
        # The library reconnects on its own inside a write; nobody needs to
        # wait for the reconnect task's next attempt to know.
        if not self.available and self.casa.connected:
            self._link_restored()

    # --- Connecting ----------------------------------------------------

    async def async_connect(self) -> None:
        """Connect to the network for the first time.

        :raises ConfigEntryAuthFailed: The password was refused.
        :raises ConfigEntryNotReady: Anything else; setup is retried.
        """
        try:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if not device:
                raise BluetoothDeviceNotFoundError  # noqa: TRY301

            # The library keeps its handlers across reconnects, so they are
            # registered once.
            if not self._handlers_registered:
                self.casa.registerDisconnectCallback(self._handle_disconnect)
                self.casa.registerUnitChangedHandler(self._handle_unit_changed)
                self.casa.registerSwitchEventHandler(self._handle_button_event)
                self._handlers_registered = True

            try:
                await self.casa.connect(device, self._password)
            except Exception as err:
                if not cloud_outage(err):
                    raise
                # A cloud incident should not keep the pergola out of Home
                # Assistant: the cached network is enough to connect.
                _LOGGER.warning(
                    "The Casambi cloud could not be used (%s); connecting to %s "
                    "from the cached network",
                    err,
                    self.address,
                )
                await self.casa.connect(device, self._password, forceOffline=True)
            self._link_up = True
        except (BluetoothDeviceNotFoundError, NetworkNotFoundError) as err:
            raise ConfigEntryNotReady(
                f"Network with address {self.address} wasn't found"
            ) from err
        except LINK_ERRORS as err:
            raise ConfigEntryNotReady("Failed to use bluetooth") from err
        except AuthenticationError as err:
            if login_outage(err):
                raise ConfigEntryNotReady(
                    f"The Casambi cloud did not answer the login: {err}"
                ) from err
            raise ConfigEntryAuthFailed(
                f"Failed to authenticate to network {self.address}"
            ) from err
        except (NetworkUpdateError, httpx.RequestError) as err:
            # The cloud failed and there is no cache to fall back on yet.
            raise ConfigEntryNotReady(
                f"The Casambi cloud could not be reached: {err}"
            ) from err
        except Exception as err:
            # The BLE stack below the library raises more kinds of error
            # than it documents; failing the entry for good would need a
            # restart to recover from what is usually a passing hiccup.
            _LOGGER.exception(
                "Unexpected error connecting to the Casambi network %s", self.address
            )
            raise ConfigEntryNotReady(
                f"Unexpected error connecting to network {self.address}"
            ) from err

        self._notify_connection()

        # Only after connecting: the registration calls back at once with
        # the last advertisement, which would start a second connection.
        if self._cancel_bluetooth_callback is None:
            self._cancel_bluetooth_callback = bluetooth.async_register_callback(
                self.hass,
                self._handle_advertisement,
                {"address": self.address, "connectable": True},
                # Seeing the network's advertisements is enough to know it
                # is back; no scan responses are needed.
                bluetooth.BluetoothScanningMode.PASSIVE,
            )

    async def async_disconnect(self) -> None:
        """Disconnect from the network for good."""
        self._closing = True
        await self._cancel_reconnect()
        self._link_up = False

        if self._cancel_bluetooth_callback is not None:
            self._cancel_bluetooth_callback()
            self._cancel_bluetooth_callback = None

        # Before disconnecting: a disconnect asked for is not to be
        # answered with a reconnect.
        if self._handlers_registered:
            self.casa.unregisterDisconnectCallback(self._handle_disconnect)

        try:
            await self.casa.disconnect()
        except Exception:
            _LOGGER.exception("Error during disconnect.")
        if self._handlers_registered:
            self.casa.unregisterUnitChangedHandler(self._handle_unit_changed)
            self.casa.unregisterSwitchEventHandler(self._handle_button_event)
            self._handlers_registered = False

    async def async_shut_down(self) -> None:
        """Stop reconnecting: Home Assistant is stopping.

        Bluetooth proxies drop their links as Home Assistant stops; that is
        no reason to start reconnecting during the shutdown.
        """
        self._closing = True
        await self._cancel_reconnect()

    # --- Reconnecting --------------------------------------------------

    async def _reconnect(self) -> None:
        """Reconnect to the network, retrying with a growing backoff.

        This runs as a single long-lived background task: it keeps trying
        until the network answers again, the entry is unloaded (the task is
        cancelled) or the password was refused, which only the user can fix.
        """
        backoff: float = RECONNECT_BACKOFF_START
        protocol_error = False
        unexpected_error = False
        started = monotonic()
        warned = False
        reason = ""

        # The task starts eagerly, inside the library's disconnect callback,
        # where the library still reports itself connected and would return
        # from reconnect() at once. Let it finish tearing down first.
        await asyncio.sleep(0)

        while True:
            self._wake.clear()
            self._wake_on_advertisement = False
            try:
                device = bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
                if not device:
                    raise BluetoothDeviceNotFoundError  # noqa: TRY301

                await async_release_stale_client(self.casa, STALE_LINK_RELEASE_TIMEOUT)
                await self.casa.reconnect(device)
                if not self.casa.connected:
                    raise BluetoothError(  # noqa: TRY301
                        "reconnect returned without a connection"
                    )
            except LINK_ERRORS as err:
                # A link lost during the handshake surfaces as a connection
                # state error, a proxy timeout as a bare TimeoutError; all
                # are worth a retry.
                _LOGGER.debug(
                    "Connecting failed due to bluetooth error. Retrying",
                    exc_info=True,
                )
                reason = f"Bluetooth error: {err}"
            except BluetoothDeviceNotFoundError:
                # Home Assistant reports the device as out of range. Retrying
                # rarely is enough to recover if the advertisement that
                # should wake this task never arrives.
                backoff = max(backoff, RECONNECT_BACKOFF_NO_DEVICE)
                reason = "no Bluetooth adapter or proxy in range sees it"
                # The network's first advertisement once it is back ends
                # the wait: Home Assistant passes on only that one.
                self._wake_on_advertisement = True
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
                self.entry.async_start_reauth(self.hass)
                return
            except asyncio.CancelledError:
                _LOGGER.debug("Reconnect task cancelled")
                raise
            except Exception as err:
                # Retry at the slowest pace rather than give up: with the
                # task gone only an advertisement could start a new one,
                # and Home Assistant does not repeat identical ones.
                if not unexpected_error:
                    _LOGGER.exception(
                        "Unexpected error reconnecting to the Casambi network %s",
                        self.address,
                    )
                unexpected_error = True
                backoff = RECONNECT_BACKOFF_MAX
                reason = f"unexpected error: {err!r}"
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

            await self._wait_backoff(backoff)
            backoff = min(RECONNECT_BACKOFF_MAX, backoff * RECONNECT_BACKOFF_STEP)

    async def _wait_backoff(self, delay: float) -> None:
        """Wait out a reconnect backoff, or less if woken."""
        sleeper = asyncio.ensure_future(asyncio.sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait((sleeper, waker), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()

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
        except Exception:
            _LOGGER.debug(
                "Got exception when cancelling reconnect. Ignoring", exc_info=True
            )

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
            self._reconnect_task = self.entry.async_create_background_task(
                self.hass, self._reconnect(), "Reconnect"
            )

    @callback
    def _link_restored(self) -> None:
        """Mark the link up and tell the listeners."""
        self._link_up = True
        # A reconnect task still waiting out its backoff can end now.
        self._wake.set()
        self._notify_connection()

    # --- What the library and Home Assistant report --------------------

    @callback
    def _handle_disconnect(self) -> None:
        self._link_up = False
        # Before telling the listeners: one that raises must not leave the
        # network without a reconnect.
        self._schedule_reconnect()
        self._notify_connection()

    @callback
    def _notify_connection(self) -> None:
        """Tell the listeners that the link dropped or came back."""
        for listener in list(self._connection_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Error handling a connection change")

    @callback
    def _handle_unit_changed(self, unit: Unit) -> None:
        if self.kind(unit) is UnitKind.SENSOR_PLATFORM:
            state = unit.state
            decoded = decode_platform_packet(state.raw_state if state else None)
            if decoded is not None:
                packet, value = decoded
                self._platform_readings.setdefault(unit.uuid, {})[packet] = value
        # The library swallows what a handler raises; one listener failing
        # must not keep the others from the push.
        for listener in list(self._unit_listeners.get(unit.deviceId, ())):
            try:
                listener(unit)
            except Exception:
                _LOGGER.exception("Error handling an update of %s", unit.name)

    @callback
    def _handle_button_event(self, event: SwitchEvent) -> None:
        _LOGGER.debug(
            "Switch event: unit %i button %i %s",
            event.unit_id,
            event.button,
            event.event.name,
        )
        self.hass.bus.async_fire(
            EVENT_BUTTON,
            {
                "network_id": self.network_id,
                "unit_id": event.unit_id,
                "button": event.button,
                "event_type": event.event.name.lower(),
            },
        )
        for listener in list(self._button_listeners):
            listener(event)

    @callback
    def _handle_advertisement(
        self,
        _service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        # Only connectable advertisements match the registered callback.
        if self.available:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            if self._wake_on_advertisement:
                self._wake.set()
            return
        self._schedule_reconnect()
