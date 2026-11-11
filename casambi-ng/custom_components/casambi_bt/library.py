"""What the integration knows about casambi-bt beyond its public API.

Everything that reads the library's error texts or reaches into its private
state lives here, so that a library update has one place to break. The
contract tests in ``tests/test_lib_compat.py`` check each assumption.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
from typing import Final

from CasambiBt import Casambi
from CasambiBt.errors import (
    AuthenticationError,
    BluetoothError,
    ConnectionStateError,
    NetworkNotFoundError,
    NetworkUpdateError,
)
import httpx

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER: Final = logging.getLogger(__name__)

# The library reports every non-OK answer of the Casambi cloud at login as
# an AuthenticationError("Login failed: <status>..."); only a rejection
# means the password is wrong, an outage is retried like any other.
_LOGIN_STATUS: Final = re.compile(r"Login failed: (\d{3})")
_TRANSIENT_LOGIN_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

# Failures of the BLE link. bleak-esphome reports a GATT timeout through a
# Bluetooth proxy as a bare TimeoutError, which the library lets through;
# a link lost during the key exchange is a connection state error.
LINK_ERRORS: Final = (BluetoothError, ConnectionStateError, TimeoutError)


def get_cache_dir(hass: HomeAssistant) -> Path:
    """Return the directory the library caches the network in."""
    return Path(hass.config.config_dir) / ".storage" / DOMAIN


def login_outage(err: AuthenticationError) -> bool:
    """Return True if the login failed because of the cloud, not the password."""
    match = _LOGIN_STATUS.search(str(err))
    return match is not None and int(match.group(1)) in _TRANSIENT_LOGIN_STATUSES


def cloud_outage(err: Exception) -> bool:
    """Return True if a connect failed because the Casambi cloud did.

    The library only carries on from its cache when the login cannot reach
    the cloud at all; a bad answer to the login, to the network id or to
    the network update stops it although the cache would do. A network the
    cloud says it doesn't know is not an outage.
    """
    if isinstance(err, AuthenticationError):
        return login_outage(err)
    if isinstance(err, NetworkNotFoundError):
        return not str(err).startswith("API failed to find network")
    return isinstance(err, (NetworkUpdateError, httpx.RequestError))


async def async_release_stale_client(casa: Casambi, timeout: float) -> None:
    """Release the Bluetooth client a dropped link left behind.

    The library only disconnects its Bleak client while that reports
    itself connected, so after a drop the old client keeps its BlueZ
    session (or proxy slot), and the next key exchange can stall on it
    (casambi-bt issue #76). Disconnecting it anyway frees it; a failure
    here changes nothing for the reconnect that follows.
    """
    client = getattr(casa, "_casaClient", None)
    gatt = getattr(client, "_gattClient", None)
    if gatt is None or gatt.is_connected:
        return
    try:
        async with asyncio.timeout(timeout):
            await gatt.disconnect()
    except Exception:
        _LOGGER.debug("Releasing the previous Bluetooth client failed", exc_info=True)
