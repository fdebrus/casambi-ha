"""Tests for the Casambi Bluetooth config flow."""

from unittest.mock import MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from CasambiBt.errors import AuthenticationError, NetworkNotFoundError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import (
    CONF_FROST_LOCK_TEMPERATURE,
    CONF_IMPORT_GROUPS,
    CONF_LOUVRE_AZIMUTH,
    CONF_VERTICAL_AS_COVER,
    CONF_WIND_THRESHOLD,
    DOMAIN,
)
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SOURCE_BLUETOOTH,
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
)
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import NETWORK_ADDRESS, NETWORK_NAME

USER_INPUT = {
    CONF_ADDRESS: "aa:bb:cc:dd:ee:ff",
    CONF_PASSWORD: "password",
    CONF_IMPORT_GROUPS: True,
    CONF_VERTICAL_AS_COVER: True,
}


async def _start_network_flow(hass: HomeAssistant) -> dict:
    """Open the config flow on the network form."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


def _discovery(connectable: bool = True) -> BluetoothServiceInfoBleak:
    """Return a discovery of the test network as the bluetooth component sends it."""
    device = BLEDevice(NETWORK_ADDRESS, NETWORK_NAME, {})
    advertisement = AdvertisementData(
        local_name=NETWORK_NAME,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak.from_scan(
        "local", device, advertisement, 0.0, connectable
    )


async def test_bluetooth_discovery(
    hass: HomeAssistant, mock_casambi: MagicMock, mock_bluetooth: MagicMock
) -> None:
    """Test that a discovered network is offered with its address filled in."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "network"

    # Confirming opens the form, with the discovered address suggested.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested[CONF_ADDRESS] == NETWORK_ADDRESS

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NETWORK_NAME
    assert result["result"].unique_id == NETWORK_ADDRESS


async def test_bluetooth_discovery_not_connectable(
    hass: HomeAssistant, mock_casambi: MagicMock, mock_bluetooth: MagicMock
) -> None:
    """Test that a network seen by a scanner that can't connect is skipped."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery(connectable=False)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_bluetooth_discovery_already_configured(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a configured network is not offered again."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_success(
    hass: HomeAssistant, mock_casambi: MagicMock, mock_bluetooth: MagicMock
) -> None:
    """Test a successful user config flow."""
    result = await _start_network_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "network"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NETWORK_NAME
    assert result["data"][CONF_ADDRESS] == NETWORK_ADDRESS
    assert result["data"][CONF_VERTICAL_AS_COVER] is True


async def test_user_flow_invalid_address(
    hass: HomeAssistant, mock_casambi: MagicMock, mock_bluetooth: MagicMock
) -> None:
    """Test that a malformed address is rejected."""
    result = await _start_network_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_ADDRESS: "not-a-mac"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_address"}


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (NetworkNotFoundError, "cannot_connect"),
        (AuthenticationError, "invalid_auth"),
        (ValueError, "unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    side_effect: type[Exception],
    error: str,
) -> None:
    """Test that connection problems are shown as form errors."""
    mock_casambi.connect.side_effect = side_effect

    result = await _start_network_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    # Recovering from the error must be possible.
    mock_casambi.connect.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_no_scanner(
    hass: HomeAssistant, mock_casambi: MagicMock, mock_bluetooth: MagicMock
) -> None:
    """Test that the flow aborts without a bluetooth scanner."""
    with patch(
        "custom_components.casambi_bt.config_flow.async_scanner_count", return_value=0
    ):
        result = await _start_network_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "bluetooth_error"


async def test_options_flow(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test changing options."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_IMPORT_GROUPS: False, CONF_VERTICAL_AS_COVER: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_IMPORT_GROUPS: False,
        CONF_VERTICAL_AS_COVER: True,
        CONF_LOUVRE_AZIMUTH: 180,
        CONF_WIND_THRESHOLD: 35,
        CONF_FROST_LOCK_TEMPERATURE: 2.0,
    }
    # The entry was reloaded once to apply them.
    assert mock_casambi.connect.await_count == 2


async def test_reauth_flow(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the reauthentication flow."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": mock_config_entry.entry_id},
        data=mock_config_entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # The refused password is not sent back to the browser.
    assert not any(
        (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "new-password"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"
    # Setup, the check of the new password, then a single reload.
    assert mock_casambi.connect.await_count == 3


async def test_reconfigure_flow(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the reconfiguration flow."""
    mock_config_entry.add_to_hass(hass)
    # The options flow was used before: its values are the ones in force.
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_IMPORT_GROUPS: False}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_RECONFIGURE,
            "entry_id": mock_config_entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    # The form shows the value in force, not the one entered at setup.
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert suggested[CONF_IMPORT_GROUPS] is False

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_PASSWORD: "changed"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "changed"
    # The flags entered here take effect even though options exist.
    assert mock_config_entry.options[CONF_IMPORT_GROUPS] is True
    # Setup, the check of the new settings, then a single reload.
    assert mock_casambi.connect.await_count == 3


async def test_reconfigure_flow_entry_without_flags(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
) -> None:
    """Test reconfiguring an entry whose data predates the setup flags.

    Entries created by upstream or by early versions of this fork have no
    vertical_as_cover in their data; the form used to raise KeyError.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NETWORK_ADDRESS,
        title=NETWORK_NAME,
        data={
            CONF_ADDRESS: NETWORK_ADDRESS,
            CONF_PASSWORD: "password",
            CONF_IMPORT_GROUPS: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert suggested[CONF_VERTICAL_AS_COVER] is False


async def test_reconfigure_flow_wrong_network(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that reconfiguring with a different address aborts."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_RECONFIGURE,
            "entry_id": mock_config_entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_ADDRESS: "11:22:33:44:55:66"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_network"
