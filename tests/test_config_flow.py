"""Tests for the Casambi Bluetooth config flow."""

from unittest.mock import MagicMock, patch

from CasambiBt.errors import AuthenticationError, NetworkNotFoundError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import (
    CONF_IMPORT_GROUPS,
    CONF_LOUVRE_AZIMUTH,
    CONF_VERTICAL_AS_COVER,
    CONF_WIND_THRESHOLD,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
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


async def test_user_flow_success(
    hass: HomeAssistant, mock_casambi: MagicMock, mock_bluetooth: MagicMock
) -> None:
    """Test a successful user config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
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
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_error"


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
    }


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

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "new-password"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_flow(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the reconfiguration flow."""
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
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_PASSWORD: "changed"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "changed"


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
