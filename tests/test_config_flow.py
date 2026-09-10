"""Tests for the config flow."""

import pytest
import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_NAME, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local import (
    async_migrate_entry,
    async_setup_entry,
    config_flow,
    get_device_unique_id,
)
from custom_components.tuya_local.cloud_cache import async_get_cache
from custom_components.tuya_local.const import (
    CONF_AREA_ID,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PRODUCT_ID,
    CONF_PROTOCOL_VERSION,
    CONF_QUICK_ADD,
    CONF_TYPE,
    DATA_DISCOVERY,
    DOMAIN,
)
from custom_components.tuya_local.discovery import DiscoveredDevice

# Designed to contain "special" characters that users constantly suspect.
TESTKEY = ")<jO<@)'P1|kR$Kd"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def prevent_task_creation(mocker):
    mocker.patch("custom_components.tuya_local.device.TuyaLocalDevice.register_entity")
    yield


@pytest.fixture
def bypass_setup(mocker):
    """Prevent actual setup of the integration after config flow."""
    mocker.patch("custom_components.tuya_local.async_setup_entry", return_value=True)
    yield


@pytest.fixture
def bypass_data_fetch(mocker):
    """Prevent actual data fetching from the device."""
    mocker.patch("tinytuya.Device.status", return_value={"1": True})
    yield


@pytest.mark.asyncio
async def test_init_entry(hass, bypass_data_fetch):
    """Test initialisation of the config flow."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=11,
        title="test",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "kogan_kahtp_heater",
            CONF_DEVICE_CID: None,
        },
        options={},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("climate.test")
    assert hass.states.get("lock.test_child_lock")


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_error", [RuntimeError("boom"), None])
async def test_async_setup_entry_cleans_up_failed_device(hass, mocker, refresh_error):
    """Failed runtime setup should not leave stale device state cached."""

    mock_device = mocker.MagicMock()
    if refresh_error is None:
        mock_device.async_refresh = mocker.AsyncMock()
        mock_device.has_returned_state = False
    else:
        mock_device.async_refresh = mocker.AsyncMock(side_effect=refresh_error)

    def fake_setup_device(hass, config):
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN]["deviceid"] = {
            "device": mock_device,
            "tuyadevice": mock_device._api,
            "tuyadevicelock": mocker.MagicMock(),
        }
        return mock_device

    mocker.patch(
        "custom_components.tuya_local.setup_device", side_effect=fake_setup_device
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=18,
        title="test",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.4,
            CONF_TYPE: "kogan_kahtp_heater",
        },
        options={},
    )

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)

    assert "deviceid" not in hass.data.get(DOMAIN, {})
    mock_device._api.set_socketPersistent.assert_called_with(False)


@pytest.mark.asyncio
async def test_migrate_entry(hass, mocker):
    """Test migration from old entry format."""
    mock_device = mocker.MagicMock()
    mock_device.async_inferred_type = mocker.AsyncMock(
        return_value="goldair_gpph_heater"
    )
    mocker.patch("custom_components.tuya_local.setup_device", return_value=mock_device)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="test",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "auto",
            "climate": True,
            "child_lock": True,
            "display_light": True,
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)

    mock_device.async_inferred_type = mocker.AsyncMock(return_value=None)
    mock_device.reset_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="test2",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "unknown",
            "climate": False,
        },
    )
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)
    mock_device.reset_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="test3",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "auto",
        },
        options={
            "climate": False,
        },
    )
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)

    mock_device.async_inferred_type = mocker.AsyncMock(return_value="smartplugv1")
    mock_device.reset_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        title="test4",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "smartplugv1",
        },
        options={
            "switch": True,
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)

    mock_device.async_inferred_type = mocker.AsyncMock(return_value="smartplugv2")
    mock_device.reset_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        title="test5",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "smartplugv1",
        },
        options={
            "switch": True,
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)

    mock_device.async_inferred_type = mocker.AsyncMock(
        return_value="goldair_dehumidifier"
    )
    mock_device.reset_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        title="test6",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "goldair_dehumidifier",
        },
        options={
            "humidifier": True,
            "fan": True,
            "light": True,
            "lock": False,
            "switch": True,
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)

    mock_device.async_inferred_type = mocker.AsyncMock(
        return_value="grid_connect_usb_double_power_point"
    )
    mock_device.reset_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=6,
        title="test7",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "grid_connect_usb_double_power_point",
        },
        options={
            "switch_main_switch": True,
            "switch_left_outlet": True,
            "switch_right_outlet": True,
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)


@pytest.mark.asyncio
async def test_flow_user_init(hass, mocker):
    """Test the initialisation of the form in the first page of the manual config flow path."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "local"}
    )
    expected = {
        "data_schema": mocker.ANY,
        "description_placeholders": mocker.ANY,
        "errors": {},
        "flow_id": mocker.ANY,
        "handler": DOMAIN,
        "step_id": "local",
        "type": "form",
        "last_step": mocker.ANY,
        "preview": mocker.ANY,
    }
    assert expected == result
    # Check the schema.  Simple comparison does not work since they are not
    # the same object
    try:
        result["data_schema"](
            {CONF_DEVICE_ID: "test", CONF_LOCAL_KEY: TESTKEY, CONF_HOST: "test"}
        )
    except vol.MultipleInvalid:
        assert False
    try:
        result["data_schema"]({CONF_DEVICE_ID: "missing_some"})
        assert False
    except vol.MultipleInvalid:
        pass


@pytest.mark.asyncio
async def test_flow_user_init_protocol_options_are_strings(hass, mocker):
    """Test that protocol version dropdown uses strings, not floats."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "local"}
    )
    schema = result["data_schema"]
    # Validate that string protocol versions are accepted
    schema(
        {
            CONF_DEVICE_ID: "test",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_HOST: "test",
            CONF_PROTOCOL_VERSION: "3.3",
            CONF_POLL_ONLY: False,
        }
    )
    # Validate that float protocol versions are rejected
    with pytest.raises(vol.MultipleInvalid):
        schema(
            {
                CONF_DEVICE_ID: "test",
                CONF_LOCAL_KEY: TESTKEY,
                CONF_HOST: "test",
                CONF_PROTOCOL_VERSION: 3.3,
                CONF_POLL_ONLY: False,
            }
        )


@pytest.mark.asyncio
async def test_async_test_connection_valid(hass, mocker):
    """Test that device is returned when connection is valid."""
    mock_device = mocker.patch(
        "custom_components.tuya_local.config_flow.TuyaLocalDevice"
    )
    mock_instance = mocker.AsyncMock()
    mock_instance.has_returned_state = True
    mock_instance.pause = mocker.MagicMock()
    mock_instance.resume = mocker.MagicMock()
    mock_device.return_value = mock_instance
    hass.data[DOMAIN] = {"deviceid": {"device": mock_instance}}

    device = await config_flow.async_test_connection(
        {
            CONF_DEVICE_ID: "deviceid",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_HOST: "hostname",
            CONF_PROTOCOL_VERSION: "auto",
        },
        hass,
    )
    assert device == mock_instance
    mock_instance.pause.assert_called_once()
    mock_instance.resume.assert_called_once()


@pytest.mark.asyncio
async def test_async_test_connection_for_subdevice_valid(hass, mocker):
    """Test that subdevice is returned when connection is valid."""
    mock_device = mocker.patch(
        "custom_components.tuya_local.config_flow.TuyaLocalDevice"
    )
    mock_instance = mocker.AsyncMock()
    mock_instance.has_returned_state = True
    mock_instance.pause = mocker.MagicMock()
    mock_instance.resume = mocker.MagicMock()
    mock_device.return_value = mock_instance
    hass.data[DOMAIN] = {"subdeviceid": {"device": mock_instance}}

    device = await config_flow.async_test_connection(
        {
            CONF_DEVICE_ID: "deviceid",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_HOST: "hostname",
            CONF_PROTOCOL_VERSION: "auto",
            CONF_DEVICE_CID: "subdeviceid",
        },
        hass,
    )
    assert device == mock_instance
    mock_instance.pause.assert_called_once()
    mock_instance.resume.assert_called_once()


@pytest.mark.asyncio
async def test_async_test_connection_invalid(hass, mocker):
    """Test that None is returned when connection is invalid."""
    mock_device = mocker.patch(
        "custom_components.tuya_local.config_flow.TuyaLocalDevice"
    )
    mock_instance = mocker.AsyncMock()
    mock_instance.has_returned_state = False
    mock_instance._api = mocker.MagicMock()
    mock_device.return_value = mock_instance
    device = await config_flow.async_test_connection(
        {
            CONF_DEVICE_ID: "deviceid",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_HOST: "hostname",
            CONF_PROTOCOL_VERSION: "auto",
        },
        hass,
    )
    assert device is None


@pytest.mark.asyncio
async def test_flow_user_init_invalid_config(hass, mocker):
    """Test errors populated when config is invalid."""
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=None,
    )
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "local"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: "badkey",
            CONF_PROTOCOL_VERSION: "auto",
            CONF_POLL_ONLY: False,
        },
    )
    assert {"base": "connection"} == result["errors"]


def setup_device_mock(mock, mocker, failure=False, devtype="test"):
    mock_type = mocker.MagicMock()
    mock_type.legacy_type = devtype
    mock_type.config_type = devtype
    mock_type.match_quality.return_value = 100
    mock_type.product_display_entries.return_value = [(None, None)]
    mock.async_possible_types = mocker.AsyncMock(
        return_value=[mock_type] if not failure else []
    )


@pytest.mark.asyncio
async def test_flow_user_init_data_valid(hass, mocker):
    """Test we advance to the next step when connection config is valid."""
    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "auto"
    setup_device_mock(mock_device, mocker)
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "local"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
        },
    )
    assert "form" == result["type"]
    assert "select_type" == result["step_id"]


@pytest.mark.asyncio
async def test_flow_select_type_init(hass, mocker):
    """Test the initialisation of the form in the 2nd step of the config flow."""
    mock_device = mocker.patch.object(config_flow.ConfigFlowHandler, "device")

    setup_device_mock(mock_device, mocker)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "select_type"}
    )
    expected = {
        "data_schema": mocker.ANY,
        "description_placeholders": {"device_name": ""},
        "errors": None,
        "flow_id": mocker.ANY,
        "handler": DOMAIN,
        "step_id": "select_type",
        "type": "form",
        "last_step": mocker.ANY,
        "preview": mocker.ANY,
    }
    assert expected == result
    # Check the schema.  Simple comparison does not work since they are not
    # the same object
    try:
        result["data_schema"]({CONF_TYPE: "test||||"})
    except vol.MultipleInvalid:
        assert False
    try:
        result["data_schema"]({CONF_TYPE: "not_test||||"})
        assert False
    except vol.MultipleInvalid:
        pass


@pytest.mark.asyncio
async def test_flow_select_type_aborts_when_no_match(hass, mocker):
    """Test the flow aborts when an unsupported device is used."""
    mock_device = mocker.patch.object(config_flow.ConfigFlowHandler, "device")
    setup_device_mock(mock_device, mocker, failure=True)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "select_type"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "not_supported"


@pytest.mark.asyncio
async def test_flow_select_type_data_valid(hass, mocker):
    """Test the flow continues when valid data is supplied."""
    mock_device = mocker.patch.object(config_flow.ConfigFlowHandler, "device")

    setup_device_mock(mock_device, mocker, devtype="smartplugv1")

    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "select_type"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={CONF_TYPE: "smartplugv1||||"},
    )
    assert "form" == result["type"]
    assert "choose_entities" == result["step_id"]


@pytest.mark.asyncio
async def test_flow_choose_entities_init(hass, mocker):
    """Test the initialisation of the form in the 3rd step of the config flow."""

    mocker.patch.dict(config_flow.ConfigFlowHandler.data, {CONF_TYPE: "smartplugv1"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "choose_entities"}
    )

    expected = {
        "data_schema": mocker.ANY,
        "description_placeholders": {"device_name": ""},
        "errors": None,
        "flow_id": mocker.ANY,
        "handler": DOMAIN,
        "step_id": "choose_entities",
        "type": "form",
        "last_step": mocker.ANY,
        "preview": mocker.ANY,
    }
    assert expected == result
    # Check the schema.  Simple comparison does not work since they are not
    # the same object
    try:
        result["data_schema"]({CONF_NAME: "test"})
    except vol.MultipleInvalid:
        assert False
    try:
        result["data_schema"]({"climate": True})
        assert False
    except vol.MultipleInvalid:
        pass


@pytest.mark.asyncio
async def test_flow_choose_entities_creates_config_entry(hass, bypass_setup, mocker):
    """Test the flow ends when data is valid."""

    mocker.patch.dict(
        config_flow.ConfigFlowHandler.data,
        {
            CONF_DEVICE_ID: "deviceid",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_HOST: "hostname",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "kogan_kahtp_heater",
            CONF_DEVICE_CID: None,
        },
    )
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "choose_entities"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={
            CONF_NAME: "test",
        },
    )
    expected = {
        "version": 13,
        "minor_version": mocker.ANY,
        "context": {"source": "choose_entities"},
        "type": FlowResultType.CREATE_ENTRY,
        "flow_id": mocker.ANY,
        "handler": DOMAIN,
        "title": "test",
        "description": None,
        "description_placeholders": None,
        "result": mocker.ANY,
        "subentries": (),
        "options": {},
        "data": {
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "kogan_kahtp_heater",
            CONF_DEVICE_CID: None,
        },
    }
    assert expected == result


@pytest.fixture
def fake_discovery(hass, mocker):
    """Provide a discovery instance with a controllable device list."""

    class FakeDiscovery:
        def __init__(self):
            self.devices = {}
            self.unidentified = []
            self.requests = 0

        async def async_request_devices(self):
            self.requests += 1

        async def async_scan_networks(self):
            pass

        async def async_locate_device(self, device_id, local_key):
            return None

    discovery = FakeDiscovery()
    hass.data.setdefault(DOMAIN, {})[DATA_DISCOVERY] = discovery
    mocker.patch("custom_components.tuya_local.async_start_discovery")
    return discovery


@pytest.mark.asyncio
async def test_flow_auto_lists_discovered_devices(hass, fake_discovery, mocker):
    """Discovered devices that are not set up yet are offered for selection."""
    fake_discovery.devices = {
        "deviceid": DiscoveredDevice(
            device_id="deviceid",
            ip="10.0.0.5",
            product_id="prodid",
            version="3.3",
        ),
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "auto"
    options = result["data_schema"].schema[CONF_DEVICE_ID].config["options"]
    assert options == [{"value": "deviceid", "label": "deviceid (10.0.0.5)"}]


@pytest.mark.asyncio
async def test_flow_auto_aborts_when_nothing_found(hass, fake_discovery, mocker):
    """Aborting is clearer than showing an empty list."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_discovered_devices"


@pytest.mark.asyncio
async def test_flow_auto_skips_configured_devices(hass, fake_discovery, mocker):
    """A device that is already set up should not be offered again."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    MockConfigEntry(
        domain=DOMAIN,
        version=13,
        unique_id="deviceid",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "10.0.0.5",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    ).add_to_hass(hass)
    fake_discovery.devices = {
        "deviceid": DiscoveredDevice(device_id="deviceid", ip="10.0.0.5"),
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_discovered_devices"


@pytest.mark.asyncio
async def test_flow_auto_prefills_local_step(hass, fake_discovery, mocker):
    """Selecting a device carries its address and cached key into the next step."""
    fake_discovery.devices = {
        "deviceid": DiscoveredDevice(
            device_id="deviceid",
            ip="10.0.0.5",
            product_id="prodid",
            version="3.3",
        ),
    }
    cache = await async_get_cache(hass)
    await cache.async_update_devices(
        {
            "deviceid": {
                "id": "deviceid",
                CONF_LOCAL_KEY: TESTKEY,
                "name": "Test light",
                "product_name": "Bulb",
            }
        }
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    options = result["data_schema"].schema[CONF_DEVICE_ID].config["options"]
    assert options == [{"value": "deviceid", "label": "Test light (10.0.0.5)"}]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["deviceid"], CONF_QUICK_ADD: False},
    )
    assert result["step_id"] == "local"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }
    assert defaults[CONF_DEVICE_ID] == "deviceid"
    assert defaults[CONF_HOST] == "10.0.0.5"
    assert defaults[CONF_LOCAL_KEY] == TESTKEY
    assert defaults[CONF_PROTOCOL_VERSION] == "3.3"


@pytest.mark.asyncio
async def test_flow_auto_offers_unidentified_hosts(hass, fake_discovery, mocker):
    """Devices found by scanning but not named are offered by address."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    assert result["step_id"] == "auto"
    options = result["data_schema"].schema[CONF_DEVICE_ID].config["options"]
    assert options == [
        {"value": "host:192.168.3.11", "label": "Unknown device (192.168.3.11)"}
    ]

    # Naming it needs a local key, so we ask which account holds it.
    mocker.patch.object(
        config_flow,
        "Cloud",
        return_value=fake_cloud(mocker, {}, authenticated=False),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["host:192.168.3.11"]},
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "account"
    assert set(result["menu_options"]) == {"cloud", "oem", "manual_entry"}

    # Choosing to fill it in by hand keeps the address that was found.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "manual_entry"},
    )
    assert result["step_id"] == "local"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }
    assert defaults[CONF_HOST] == "192.168.3.11"


def fake_cloud(mocker, devices, authenticated=True):
    """Build a cloud interface that returns a fixed set of devices."""
    cloud = mocker.MagicMock()
    cloud.is_authenticated = authenticated
    cloud.async_get_devices = mocker.AsyncMock(return_value=devices)
    return cloud


@pytest.mark.asyncio
async def test_flow_auto_identifies_host_from_the_account(hass, fake_discovery, mocker):
    """A scanned address is named by the keys of the cloud account."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    fake_discovery.unidentified = ["192.168.3.11"]
    mocker.patch.object(
        config_flow,
        "Cloud",
        return_value=fake_cloud(
            mocker,
            {
                "deviceid": {
                    "id": "deviceid",
                    "ip": "",
                    CONF_LOCAL_KEY: TESTKEY,
                    "name": "Test light",
                    "product_id": "prodid",
                    "product_name": "Light",
                },
            },
        ),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        return_value=DiscoveredDevice(
            device_id="deviceid",
            ip="192.168.3.11",
            version="3.4",
        ),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["host:192.168.3.11"]},
    )
    assert result["step_id"] == "local"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }
    assert defaults[CONF_DEVICE_ID] == "deviceid"
    assert defaults[CONF_HOST] == "192.168.3.11"
    assert defaults[CONF_LOCAL_KEY] == TESTKEY
    assert defaults[CONF_PROTOCOL_VERSION] == "3.4"


@pytest.mark.asyncio
async def test_flow_auto_falls_back_when_the_account_has_no_match(
    hass, fake_discovery, mocker
):
    """A device outside the account still has to be filled in by hand."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    fake_discovery.unidentified = ["192.168.3.11"]
    mocker.patch.object(
        config_flow,
        "Cloud",
        return_value=fake_cloud(
            mocker,
            {"other": {"id": "other", CONF_LOCAL_KEY: TESTKEY}},
        ),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        return_value=None,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["host:192.168.3.11"]},
    )
    assert result["step_id"] == "local"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }
    assert defaults[CONF_DEVICE_ID] == ""
    assert defaults[CONF_HOST] == "192.168.3.11"


def fake_oem_cloud(mocker, devices, error=None):
    """Build a brand cloud that either signs in or refuses to."""
    cloud = mocker.MagicMock()
    cloud.async_login = mocker.AsyncMock(side_effect=error)
    cloud.async_get_devices = mocker.AsyncMock(return_value=devices)
    return cloud


LEDVANCE_DEVICE = {
    "id": "ledvanceid",
    "ip": "",
    CONF_LOCAL_KEY: TESTKEY,
    "name": "Kitchen light",
    "product_id": "prodid",
    "product_name": "LEDVANCE SMART+",
    "online": True,
    "is_hub": False,
    "sub": False,
    "node_id": "",
    "uuid": "",
}


async def _account_menu(hass, mocker):
    """Get as far as being asked which account holds a scanned device."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    mocker.patch.object(
        config_flow,
        "Cloud",
        return_value=fake_cloud(mocker, {}, authenticated=False),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["host:192.168.3.11"], CONF_QUICK_ADD: False},
    )


@pytest.mark.asyncio
async def test_brand_login_identifies_a_scanned_device(hass, fake_discovery, mocker):
    """A LEDVANCE device is named by the keys of the LEDVANCE account."""
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await _account_menu(hass, mocker)

    mocker.patch.object(
        config_flow,
        "OemCloud",
        return_value=fake_oem_cloud(mocker, {"ledvanceid": LEDVANCE_DEVICE}),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        return_value=DiscoveredDevice(
            device_id="ledvanceid",
            ip="192.168.3.11",
            version="3.4",
        ),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "oem"},
    )
    assert result["step_id"] == "oem"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "eu",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    assert result["step_id"] == "local"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }
    assert defaults[CONF_DEVICE_ID] == "ledvanceid"
    assert defaults[CONF_HOST] == "192.168.3.11"
    assert defaults[CONF_LOCAL_KEY] == TESTKEY
    assert defaults[CONF_PROTOCOL_VERSION] == "3.4"

    # The keys are kept, so discovery can name these devices unaided later.
    cache = await async_get_cache(hass)
    assert cache.get_local_key("ledvanceid") == TESTKEY


@pytest.mark.asyncio
async def test_brand_login_reports_bad_credentials(hass, fake_discovery, mocker):
    """A refused login has to be correctable rather than fatal."""
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await _account_menu(hass, mocker)
    mocker.patch.object(
        config_flow,
        "OemCloud",
        return_value=fake_oem_cloud(
            mocker,
            {},
            error=config_flow.OemCloudAuthError("wrong"),
        ),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "oem"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "eu",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "wrong",
        },
    )
    assert result["step_id"] == "oem"
    assert result["errors"] == {"base": "login_error"}


@pytest.mark.asyncio
async def test_brand_login_reports_an_empty_account(hass, fake_discovery, mocker):
    """Signing in to the wrong region finds nothing, which needs saying."""
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await _account_menu(hass, mocker)
    mocker.patch.object(
        config_flow,
        "OemCloud",
        return_value=fake_oem_cloud(mocker, {}),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "oem"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "us",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    assert result["step_id"] == "oem"
    assert result["errors"] == {"base": "no_devices"}


@pytest.mark.asyncio
async def test_brand_setup_mode_lists_the_account_devices(hass, mocker):
    """A brand account can be used without discovery finding anything."""
    mocker.patch.object(
        config_flow,
        "OemCloud",
        return_value=fake_oem_cloud(mocker, {"ledvanceid": LEDVANCE_DEVICE}),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "oem"},
    )
    assert result["step_id"] == "oem"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "eu",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    assert result["step_id"] == "choose_device"
    options = result["data_schema"].schema["device_id"].config["options"]
    assert options == [
        {"value": "ledvanceid", "label": "Kitchen light (LEDVANCE SMART+)"}
    ]


@pytest.mark.asyncio
async def test_brand_device_does_not_ask_for_a_gateway(hass, mocker):
    """A brand cloud says whether a device is behind a gateway, so believe it."""
    mocker.patch.object(
        config_flow,
        "OemCloud",
        return_value=fake_oem_cloud(mocker, {"ledvanceid": dict(LEDVANCE_DEVICE)}),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "oem"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "eu",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    # An empty address would otherwise be read as needing a gateway.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"device_id": "ledvanceid", "hub_id": "None"},
    )
    assert result["step_id"] == "search"


@pytest.mark.asyncio
async def test_flow_auto_hides_configured_hosts(hass, fake_discovery, mocker):
    """An address that is already set up is not offered again."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    MockConfigEntry(
        domain=DOMAIN,
        version=13,
        unique_id="deviceid",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "192.168.3.11",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    ).add_to_hass(hass)
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_discovered_devices"


@pytest.mark.asyncio
async def test_options_flow_init(hass, bypass_data_fetch):
    """Test config flow options."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        unique_id="uniqueid",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_NAME: "test",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "smartplugv1",
            CONF_DEVICE_CID: "",
        },
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # show initial form
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert "form" == result["type"]
    assert "user" == result["step_id"]
    assert {} == result["errors"]
    assert result["data_schema"](
        {
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
        }
    )


@pytest.mark.asyncio
async def test_options_flow_modifies_config(hass, bypass_setup, mocker):
    mock_device = mocker.MagicMock()
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        unique_id="uniqueid",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_NAME: "test",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "ble_pt216_temp_humidity",
            CONF_DEVICE_CID: "subdeviceid",
        },
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # show initial form
    form = await hass.config_entries.options.async_init(config_entry.entry_id)
    # submit updated config
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        user_input={
            CONF_HOST: "new_hostname",
            CONF_LOCAL_KEY: "new_key",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "3.3",
        },
    )
    expected = {
        CONF_HOST: "new_hostname",
        CONF_LOCAL_KEY: "new_key",
        CONF_POLL_ONLY: False,
        CONF_PROTOCOL_VERSION: 3.3,
    }
    assert "create_entry" == result["type"]
    assert "" == result["title"]
    assert expected == result["data"]


@pytest.mark.asyncio
async def test_options_flow_fails_when_connection_fails(
    hass, bypass_data_fetch, mocker
):
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=None,
    )
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        unique_id="uniqueid",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_NAME: "test",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "smartplugv1",
            CONF_DEVICE_CID: "",
        },
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # show initial form
    form = await hass.config_entries.options.async_init(config_entry.entry_id)
    # submit updated config
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        user_input={
            CONF_HOST: "new_hostname",
            CONF_LOCAL_KEY: "new_key",
        },
    )
    assert "form" == result["type"]
    assert "user" == result["step_id"]
    assert {"base": "connection"} == result["errors"]


@pytest.mark.asyncio
async def test_options_flow_fails_when_config_is_missing(hass, mocker):
    mock_device = mocker.MagicMock()
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        unique_id="uniqueid",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_NAME: "test",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "non_existing",
        },
    )
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # show initial form
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] == "abort"
    assert result["reason"] == "not_supported"


def test_migration_gets_correct_device_id():
    """Test that migration gets the correct device id."""
    # Normal device
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="test",
        data={
            CONF_DEVICE_ID: "deviceid",
            CONF_HOST: "hostname",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_TYPE: "auto",
        },
    )
    assert get_device_unique_id(entry) == "deviceid"


@pytest.mark.asyncio
async def test_brand_login_is_kept_for_the_device_spec(hass, fake_discovery, mocker):
    """The brand account is the only one that can describe a brand device."""
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await _account_menu(hass, mocker)

    oem_cloud = fake_oem_cloud(mocker, {"ledvanceid": LEDVANCE_DEVICE})
    mocker.patch.object(config_flow, "OemCloud", return_value=oem_cloud)
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        return_value=DiscoveredDevice(
            device_id="ledvanceid",
            ip="192.168.3.11",
            version="3.4",
        ),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "oem"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "eu",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    assert result["step_id"] == "local"

    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._ConfigFlowHandler__oem_cloud is oem_cloud


@pytest.mark.asyncio
async def test_cloud_product_id_is_kept_in_the_entry(hass, fake_discovery, mocker):
    """Otherwise the product id survives only as a line in the log."""
    fake_discovery.unidentified = ["192.168.3.11"]
    result = await _account_menu(hass, mocker)

    mocker.patch.object(
        config_flow,
        "OemCloud",
        return_value=fake_oem_cloud(mocker, {"ledvanceid": LEDVANCE_DEVICE}),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        return_value=DiscoveredDevice(
            device_id="ledvanceid",
            ip="192.168.3.11",
            version="3.4",
        ),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "oem"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "brand": "ledvance",
            "region": "eu",
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
        },
    )
    assert result["step_id"] == "local"

    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "3.4"
    setup_device_mock(mock_device, mocker)
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_ID: "ledvanceid",
            CONF_HOST: "192.168.3.11",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_PROTOCOL_VERSION: "3.4",
            CONF_POLL_ONLY: False,
        },
    )

    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow.data[CONF_PRODUCT_ID] == "prodid"


async def _discovered_with_key(hass, mocker, version="3.3"):
    """A discovered device whose key is already known from the cloud."""
    cache = await async_get_cache(hass)
    await cache.async_update_devices(
        {
            "deviceid": {
                "id": "deviceid",
                CONF_LOCAL_KEY: TESTKEY,
                "name": "Kitchen light",
                "product_name": "Bulb",
            }
        }
    )
    return DiscoveredDevice(
        device_id="deviceid",
        ip="10.0.0.5",
        product_id="prodid",
        version=version,
    )


@pytest.mark.asyncio
async def test_quick_add_skips_the_remaining_questions(
    hass, bypass_setup, fake_discovery, mocker
):
    """Nothing is worth asking when the address, key and type are all known."""
    fake_discovery.devices = {"deviceid": await _discovered_with_key(hass, mocker)}

    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "3.3"
    mock_device._product_ids = []
    setup_device_mock(mock_device, mocker, devtype="kogan_kahtp_heater")
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["deviceid"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Named from the cloud, not from the configuration it matched.
    assert result["title"] == "Kitchen light"
    assert result["data"][CONF_DEVICE_ID] == "deviceid"
    assert result["data"][CONF_HOST] == "10.0.0.5"
    assert result["data"][CONF_LOCAL_KEY] == TESTKEY
    assert result["data"][CONF_TYPE] == "kogan_kahtp_heater"


@pytest.mark.asyncio
async def test_quick_add_still_asks_when_the_match_is_ambiguous(
    hass, fake_discovery, mocker
):
    """Choosing between equally good configurations is the whole question."""
    fake_discovery.devices = {"deviceid": await _discovered_with_key(hass, mocker)}

    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "3.3"
    mock_device._product_ids = []
    types = []
    for name in ("kogan_kahtp_heater", "goldair_gpph_heater"):
        t = mocker.MagicMock()
        t.legacy_type = name
        t.config_type = name
        t.match_quality.return_value = 100
        t.product_display_entries.return_value = [(None, None)]
        types.append(t)
    mock_device.async_possible_types = mocker.AsyncMock(return_value=types)
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["deviceid"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "select_type"


@pytest.mark.asyncio
async def test_quick_add_still_asks_when_the_match_is_imperfect(
    hass, fake_discovery, mocker
):
    """A partial match is a guess, so it has to be confirmed."""
    fake_discovery.devices = {"deviceid": await _discovered_with_key(hass, mocker)}

    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "3.3"
    mock_device._product_ids = []
    setup_device_mock(mock_device, mocker, devtype="kogan_kahtp_heater")
    mock_device.async_possible_types.return_value[0].match_quality.return_value = 85
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["deviceid"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "select_type"


@pytest.mark.asyncio
async def test_quick_add_falls_back_when_the_key_is_unknown(
    hass, fake_discovery, mocker
):
    """Without a key there is still a form to fill in."""
    mocker.patch.object(config_flow, "DISCOVERY_WAIT", 0)
    fake_discovery.devices = {
        "deviceid": DiscoveredDevice(
            device_id="deviceid",
            ip="10.0.0.5",
            version="3.3",
        ),
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["deviceid"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local"


async def _cache_devices(hass, names):
    """Put devices with known keys into the cloud cache."""
    cache = await async_get_cache(hass)
    await cache.async_update_devices(
        {
            device_id: {
                "id": device_id,
                CONF_LOCAL_KEY: TESTKEY,
                "name": name,
                "product_name": "Bulb",
            }
            for device_id, name in names.items()
        }
    )


def _discovered(*device_ids):
    return {
        device_id: DiscoveredDevice(
            device_id=device_id,
            ip=f"10.0.0.{n + 5}",
            product_id="prodid",
            version="3.3",
        )
        for n, device_id in enumerate(device_ids)
    }


def _connecting_device(mocker, quality=100):
    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "3.3"
    mock_device._product_ids = []
    setup_device_mock(mock_device, mocker, devtype="kogan_kahtp_heater")
    mock_device.async_possible_types.return_value[
        0
    ].match_quality.return_value = quality
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )
    return mock_device


@pytest.mark.asyncio
async def test_bulk_add_adds_every_selected_device(
    hass, bypass_setup, fake_discovery, mocker
):
    """Choosing several devices is a request not to be asked about each."""
    fake_discovery.devices = _discovered("dev1", "dev2", "dev3")
    await _cache_devices(
        hass, {"dev1": "Lamp one", "dev2": "Lamp two", "dev3": "Lamp three"}
    )
    _connecting_device(mocker)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["dev1", "dev2", "dev3"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "bulk_added"
    assert result["description_placeholders"]["added_count"] == "3"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert {entry.title for entry in entries} == {
        "Lamp one",
        "Lamp two",
        "Lamp three",
    }
    assert {entry.data[CONF_DEVICE_ID] for entry in entries} == {
        "dev1",
        "dev2",
        "dev3",
    }
    assert all(entry.data[CONF_TYPE] == "kogan_kahtp_heater" for entry in entries)


@pytest.mark.asyncio
async def test_bulk_add_files_devices_in_the_chosen_area(
    hass, bypass_setup, fake_discovery, mocker
):
    """The point of adding a room at a time is that they land in the room."""
    fake_discovery.devices = _discovered("dev1", "dev2")
    await _cache_devices(hass, {"dev1": "Lamp one", "dev2": "Lamp two"})
    _connecting_device(mocker)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_ID: ["dev1", "dev2"],
            CONF_AREA_ID: "living_room",
            CONF_QUICK_ADD: True,
        },
    )

    assert result["reason"] == "bulk_added"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert all(entry.data[CONF_AREA_ID] == "living_room" for entry in entries)


@pytest.mark.asyncio
async def test_bulk_add_reports_the_devices_it_left_out(
    hass, bypass_setup, fake_discovery, mocker
):
    """A device without a key cannot be added silently, so say so."""
    fake_discovery.devices = _discovered("dev1", "keyless")
    await _cache_devices(hass, {"dev1": "Lamp one"})
    _connecting_device(mocker)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["dev1", "keyless"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "bulk_partial"
    assert result["description_placeholders"]["added"] == "Lamp one"
    assert result["description_placeholders"]["skipped"] == "keyless"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.asyncio
async def test_bulk_add_leaves_out_devices_that_need_a_choice(
    hass, bypass_setup, fake_discovery, mocker
):
    """An imperfect match is a question, and questions cannot be asked here."""
    fake_discovery.devices = _discovered("dev1", "dev2")
    await _cache_devices(hass, {"dev1": "Lamp one", "dev2": "Lamp two"})
    _connecting_device(mocker, quality=85)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["dev1", "dev2"], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "bulk_none_added"
    assert hass.config_entries.async_entries(DOMAIN) == []
    # The abandoned attempts must not be left sitting in the flow list.
    assert hass.config_entries.flow.async_progress() == []


@pytest.mark.asyncio
async def test_bulk_add_leaves_out_devices_it_cannot_connect_to(
    hass, bypass_setup, fake_discovery, mocker
):
    """A device that does not answer needs its details checked by hand."""
    fake_discovery.devices = _discovered("dev1", "dev2")
    await _cache_devices(hass, {"dev1": "Lamp one", "dev2": "Lamp two"})
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=None,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["dev1", "dev2"], CONF_QUICK_ADD: True},
    )

    assert result["reason"] == "bulk_none_added"
    assert hass.config_entries.flow.async_progress() == []


@pytest.mark.asyncio
async def test_auto_step_requires_a_device_to_be_selected(hass, fake_discovery, mocker):
    """An empty selection is a slip, not a request to add nothing."""
    fake_discovery.devices = _discovered("dev1")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: [], CONF_QUICK_ADD: True},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "auto"
    assert result["errors"] == {CONF_DEVICE_ID: "no_device_selected"}


@pytest.mark.asyncio
async def test_single_selection_keeps_the_chosen_area(
    hass, bypass_setup, fake_discovery, mocker
):
    """One device picked with an area should be filed there too."""
    fake_discovery.devices = _discovered("dev1")
    await _cache_devices(hass, {"dev1": "Lamp one"})
    _connecting_device(mocker)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_ID: ["dev1"],
            CONF_AREA_ID: "living_room",
            CONF_QUICK_ADD: True,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AREA_ID] == "living_room"


@pytest.mark.asyncio
async def test_bulk_add_survives_a_device_that_raises(
    hass, bypass_setup, fake_discovery, mocker
):
    """One awkward device must not cost the rest of the batch."""
    fake_discovery.devices = _discovered("dev1", "dev2")
    await _cache_devices(hass, {"dev1": "Lamp one", "dev2": "Lamp two"})
    mock_device = _connecting_device(mocker)

    def explode_for_dev1(config, hass):
        if config[CONF_DEVICE_ID] == "dev1":
            raise RuntimeError("boom")
        return mock_device

    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        side_effect=explode_for_dev1,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"setup_mode": "auto"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: ["dev1", "dev2"], CONF_QUICK_ADD: True},
    )

    assert result["reason"] == "bulk_partial"
    assert result["description_placeholders"]["added"] == "Lamp two"
    assert result["description_placeholders"]["skipped"] == "Lamp one"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
