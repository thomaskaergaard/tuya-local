"""Tests for local network discovery of Tuya devices."""

import asyncio
import json

import pytest
import tinytuya
import voluptuous as vol
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tinytuya import TuyaMessage, pack_message
from tinytuya.core.udp_helper import encrypt, udpkey

from custom_components.tuya_local import async_resync_local_key
from custom_components.tuya_local.cloud_cache import CloudCache, async_get_cache
from custom_components.tuya_local.const import (
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_QUICK_ADD,
    CONF_TYPE,
    DOMAIN,
)
from custom_components.tuya_local.discovery import (
    DISCOVERY_PORTS,
    DiscoveredDevice,
    TuyaLocalDiscovery,
    async_find_tuya_hosts,
    build_probe_request,
    expand_probe_network,
    identify_host,
    parse_discovery_message,
)

TESTKEY = ")<jO<@)'P1|kR$Kd"
DEVICE_ID = "eb1234567890abcdef"

# Real broadcasts carry a 4 byte return code ahead of the JSON payload.
RETCODE = b"\x00\x00\x00\x00"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _announcement(**overrides):
    message = {
        "ip": "192.168.1.50",
        "gwId": DEVICE_ID,
        "active": 2,
        "encrypt": True,
        "productKey": "abcdefghijklmnop",
        "version": "3.3",
    }
    message.update(overrides)
    return json.dumps(message).encode()


def plaintext_packet(**overrides):
    """Build an unencrypted (protocol 3.1) broadcast, as sent to port 6666."""
    return pack_message(
        TuyaMessage(
            0,
            0x0A,
            None,
            RETCODE + _announcement(**overrides),
            0,
            True,
            tinytuya.PREFIX_55AA_VALUE,
            False,
        )
    )


def encrypted_packet(**overrides):
    """Build an AES-ECB broadcast, as sent to port 6667."""
    payload = encrypt(_announcement(**overrides), udpkey)
    return pack_message(
        TuyaMessage(
            0,
            0x13,
            None,
            RETCODE + payload,
            0,
            True,
            tinytuya.PREFIX_55AA_VALUE,
            False,
        )
    )


def gcm_packet(**overrides):
    """Build an AES-GCM broadcast, as sent by 3.5 devices to port 7000."""
    return pack_message(
        TuyaMessage(
            0,
            0x13,
            0,
            _announcement(**overrides),
            0,
            True,
            tinytuya.PREFIX_6699_VALUE,
            b"0123456789ab",
        ),
        hmac_key=udpkey,
    )


@pytest.mark.parametrize(
    "builder",
    [plaintext_packet, encrypted_packet, gcm_packet],
)
def test_parses_all_broadcast_formats(builder):
    """All three Tuya broadcast encodings should decode identically."""
    device = parse_discovery_message(builder())
    assert device == DiscoveredDevice(
        device_id=DEVICE_ID,
        ip="192.168.1.50",
        product_id="abcdefghijklmnop",
        version="3.3",
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a tuya packet",
        b"\x00\x00U\xaa" + b"\x00" * 12,
    ],
)
def test_ignores_undecodable_packets(data):
    assert parse_discovery_message(data) is None


def test_ignores_announcement_without_id_or_ip():
    assert parse_discovery_message(plaintext_packet(gwId="")) is None
    assert parse_discovery_message(plaintext_packet(ip="")) is None


@pytest.mark.asyncio
async def test_notifies_once_per_device_until_ip_changes():
    """Devices rebroadcast constantly, so only changes should be reported."""
    seen = []

    async def on_device(device):
        seen.append(device)

    discovery = TuyaLocalDiscovery(on_device)
    discovery._handle_datagram(plaintext_packet(), "192.168.1.50")
    discovery._handle_datagram(plaintext_packet(), "192.168.1.50")
    await asyncio.sleep(0)
    assert [d.ip for d in seen] == ["192.168.1.50"]

    discovery._handle_datagram(plaintext_packet(ip="192.168.1.60"), "192.168.1.60")
    await asyncio.sleep(0)
    assert [d.ip for d in seen] == ["192.168.1.50", "192.168.1.60"]
    assert discovery.devices[DEVICE_ID].ip == "192.168.1.60"


@pytest.mark.asyncio
async def test_ignores_announcement_from_a_different_address():
    """A forged announcement must not be able to redirect a device."""
    seen = []

    async def on_device(device):
        seen.append(device)

    discovery = TuyaLocalDiscovery(on_device)
    # Claims to be at .50, but was actually sent by .200
    discovery._handle_datagram(plaintext_packet(), "192.168.1.200")
    await asyncio.sleep(0)
    assert seen == []
    assert discovery.devices == {}


@pytest.mark.asyncio
async def test_discovery_failure_is_not_fatal(mocker, hass):
    """Being unable to bind the ports must not stop the integration."""
    endpoint = mocker.patch.object(
        hass.loop,
        "create_datagram_endpoint",
        side_effect=OSError("address in use"),
    )
    discovery = TuyaLocalDiscovery(mocker.AsyncMock())
    await discovery.async_start()

    # Every port was tried, none bound, and no exception escaped.
    assert endpoint.call_count == len(DISCOVERY_PORTS)
    assert discovery._transports == []
    assert discovery.devices == {}
    discovery.async_stop()


@pytest.mark.asyncio
async def test_discovery_probes_for_silent_devices(mocker, hass):
    """Protocol 3.5 devices only answer when asked, so a request is sent."""
    mocker.patch.object(
        hass.loop,
        "create_datagram_endpoint",
        return_value=(mocker.MagicMock(), mocker.MagicMock()),
    )
    request = mocker.patch(
        "custom_components.tuya_local.discovery.send_discovery_request"
    )
    discovery = TuyaLocalDiscovery(mocker.AsyncMock())
    await discovery.async_start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert request.call_count == 1
    assert discovery._probe_task is not None
    discovery.async_stop()
    assert discovery._probe_task is None


@pytest.mark.asyncio
async def test_probe_failure_is_not_fatal(mocker, hass):
    """A network that cannot broadcast must not break passive discovery."""
    mocker.patch(
        "custom_components.tuya_local.discovery.send_discovery_request",
        side_effect=OSError("no broadcast route"),
    )
    discovery = TuyaLocalDiscovery(mocker.AsyncMock())
    await discovery.async_request_devices()


@pytest.mark.parametrize(
    "network,expected",
    [
        ("192.168.3.11", ["192.168.3.11"]),
        ("192.168.3.255", ["192.168.3.255"]),
        ("192.168.3.0/30", ["192.168.3.1", "192.168.3.2"]),
        # Too large to probe one address at a time.
        ("10.0.0.0/8", []),
        # Not addresses at all.
        ("not an address", []),
        ("fd00::/64", []),
    ],
)
def test_probe_network_expansion(network, expected):
    """Configured networks are expanded to the addresses to be probed."""
    assert expand_probe_network(network) == expected


def test_probe_request_asks_for_reply_to_us():
    """Devices reply to the address in the request, not to its sender."""
    request = build_probe_request("192.168.0.7")
    assert json.loads(tinytuya.decrypt_udp(request)) == {
        "from": "app",
        "ip": "192.168.0.7",
    }


@pytest.mark.asyncio
async def test_configured_networks_are_probed_by_address(mocker, hass):
    """Devices on a VLAN cannot hear broadcasts, so they are probed directly."""
    mocker.patch("custom_components.tuya_local.discovery.send_discovery_request")
    probes = mocker.patch("custom_components.tuya_local.discovery.send_unicast_probes")
    discovery = TuyaLocalDiscovery(
        mocker.AsyncMock(),
        ["192.168.3.0/30", "192.168.5.11"],
    )
    await discovery.async_request_devices()

    probes.assert_called_once_with(
        ["192.168.3.1", "192.168.3.2", "192.168.5.11"],
    )


@pytest.mark.asyncio
async def test_no_probing_without_configured_networks(mocker, hass):
    """Nothing is sent by address unless networks have been configured."""
    mocker.patch("custom_components.tuya_local.discovery.send_discovery_request")
    probes = mocker.patch("custom_components.tuya_local.discovery.send_unicast_probes")
    discovery = TuyaLocalDiscovery(mocker.AsyncMock())
    await discovery.async_request_devices()

    probes.assert_not_called()


@pytest.mark.asyncio
async def test_scan_finds_hosts_that_accept_connections(mocker):
    """Only addresses that answer on the device port are devices."""

    async def connect(host, port):
        if host != "192.168.3.11":
            raise OSError("no route to host")
        return mocker.MagicMock(), mocker.MagicMock(wait_closed=mocker.AsyncMock())

    mocker.patch("asyncio.open_connection", side_effect=connect)
    found = await async_find_tuya_hosts(["192.168.3.10", "192.168.3.11"])
    assert found == ["192.168.3.11"]


@pytest.mark.asyncio
async def test_scan_without_targets_does_nothing(mocker):
    """No configured networks means there is nothing to connect to."""
    connect = mocker.patch("asyncio.open_connection")
    assert await async_find_tuya_hosts([]) == []
    connect.assert_not_called()


def test_identify_host_matches_the_key_that_works(mocker):
    """A device is named by the key that it accepts."""

    def make_device(device_id, host, local_key, version):
        device = mocker.MagicMock()
        device.status.return_value = (
            {"dps": {"1": True}}
            if local_key == "rightkey" and version == 3.4
            else {"Error": "Check device key or version"}
        )
        return device

    mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device",
        side_effect=make_device,
    )
    found = identify_host(
        "192.168.3.11", {"wrongid": "wrongkey", "rightid": "rightkey"}
    )
    assert found == DiscoveredDevice(
        device_id="rightid",
        ip="192.168.3.11",
        version="3.4",
    )


def test_identify_host_gives_up_when_no_key_works(mocker):
    """Devices that are not in the cloud account cannot be named."""
    device = mocker.MagicMock()
    device.status.return_value = {"Error": "Check device key or version"}
    mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device",
        return_value=device,
    )
    assert identify_host("192.168.3.11", {"id": "key"}) is None


def test_identify_host_tries_cheap_versions_across_all_keys_first(mocker):
    """The version that rejects a wrong key slowest is left until last."""
    tried = []

    def make_device(device_id, host, local_key, version):
        tried.append((device_id, version))
        device = mocker.MagicMock()
        device.status.return_value = {"Error": "Check device key or version"}
        return device

    mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device",
        side_effect=make_device,
    )
    identify_host("192.168.3.11", {"a": "keya", "b": "keyb"})

    assert tried == [
        ("a", 3.3),
        ("b", 3.3),
        ("a", 3.4),
        ("b", 3.4),
        ("a", 3.1),
        ("b", 3.1),
        ("a", 3.5),
        ("b", 3.5),
    ]


def test_identify_host_stops_when_the_budget_runs_out(mocker):
    """A large account must not leave the user waiting indefinitely."""
    device = mocker.MagicMock()
    device.status.return_value = {"Error": "Check device key or version"}
    made = mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device",
        return_value=device,
    )
    assert identify_host("192.168.3.11", {"id": "key"}, budget=0) is None
    made.assert_not_called()


def test_identify_host_abandons_a_version_that_does_not_answer(mocker):
    """Silence is about the protocol, so the other keys would only wait too."""
    tried = []

    def make_device(device_id, host, local_key, version):
        tried.append((device_id, version))
        device = mocker.MagicMock()
        device.status.return_value = {"Error": "Network Error"}
        return device

    mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device",
        side_effect=make_device,
    )
    # Every attempt now looks like it ran out of time.
    mocker.patch("custom_components.tuya_local.discovery.IDENTIFY_TIMEOUT", 0)
    assert identify_host("192.168.3.11", {"a": "keya", "b": "keyb"}) is None

    assert tried == [("a", 3.3), ("a", 3.4), ("a", 3.1), ("a", 3.5)]


@pytest.mark.asyncio
async def test_scan_without_keys_reports_every_host(mocker, hass):
    """Nothing can be named before a cloud login, so do not try."""
    mocker.patch(
        "custom_components.tuya_local.discovery.async_find_tuya_hosts",
        return_value=["192.168.3.11", "192.168.3.12"],
    )
    identify = mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
    )
    discovery = TuyaLocalDiscovery(mocker.AsyncMock(), ["192.168.3.0/30"])
    await discovery.async_scan_networks()

    identify.assert_not_called()
    assert discovery.unidentified == ["192.168.3.11", "192.168.3.12"]


@pytest.mark.asyncio
async def test_scan_reports_devices_it_cannot_name(mocker, hass):
    """Devices found without a matching key are offered by address."""
    mocker.patch(
        "custom_components.tuya_local.discovery.async_find_tuya_hosts",
        return_value=["192.168.3.11", "192.168.3.12"],
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        side_effect=lambda host, keys, budget=None: (
            DiscoveredDevice(device_id=DEVICE_ID, ip=host, version="3.4")
            if host == "192.168.3.11"
            else None
        ),
    )
    on_device = mocker.AsyncMock()
    discovery = TuyaLocalDiscovery(
        on_device,
        ["192.168.3.0/30"],
        lambda: {DEVICE_ID: TESTKEY},
    )
    await discovery.async_scan_networks()
    await asyncio.sleep(0)

    assert discovery.devices[DEVICE_ID].ip == "192.168.3.11"
    assert discovery.unidentified == ["192.168.3.12"]


@pytest.mark.asyncio
async def test_locate_device_finds_one_known_device(mocker, hass):
    """The cloud flow knows the key, so it can pinpoint one device."""
    mocker.patch(
        "custom_components.tuya_local.discovery.async_find_tuya_hosts",
        return_value=["192.168.3.11", "192.168.3.12"],
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.identify_host",
        side_effect=lambda host, keys, budget=None: (
            DiscoveredDevice(device_id=DEVICE_ID, ip=host, version="3.5")
            if host == "192.168.3.12"
            else None
        ),
    )
    discovery = TuyaLocalDiscovery(mocker.AsyncMock(), ["192.168.3.0/30"])
    found = await discovery.async_locate_device(DEVICE_ID, TESTKEY)

    assert found is not None
    assert found.ip == "192.168.3.12"
    assert found.version == "3.5"


@pytest.mark.asyncio
async def test_discovery_offers_new_device(hass):
    """An unknown device should present a confirmation step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            "product_id": "abcdefghijklmnop",
            "version": "3.3",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


@pytest.mark.asyncio
async def test_discovery_prefills_cached_local_key(hass):
    """A key cached from a cloud login should be offered as the default."""
    cache = await async_get_cache(hass)
    await cache.async_update_devices(
        {
            DEVICE_ID: {
                "id": DEVICE_ID,
                CONF_LOCAL_KEY: TESTKEY,
                "name": "Cached light",
                "product_id": "abcdefghijklmnop",
                "product_name": "Light",
            }
        }
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_DEVICE_ID: DEVICE_ID, CONF_HOST: "192.168.1.50"},
    )
    assert result["step_id"] == "discovery_confirm"
    assert "Cached light" in result["description_placeholders"]["device_name"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_QUICK_ADD: False}
    )
    assert result["step_id"] == "local"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if marker.default is not vol.UNDEFINED
    }
    assert defaults[CONF_LOCAL_KEY] == TESTKEY
    assert defaults[CONF_HOST] == "192.168.1.50"
    assert defaults[CONF_DEVICE_ID] == DEVICE_ID


@pytest.mark.asyncio
async def test_discovery_repairs_changed_ip(hass):
    """A device that moves to a new address should be updated in place."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="test",
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_DEVICE_ID: DEVICE_ID, CONF_HOST: "192.168.1.99"},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.99"


@pytest.mark.asyncio
async def test_discovery_leaves_unchanged_ip_alone(hass):
    """Rediscovering a device at its known address should change nothing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="test",
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_DEVICE_ID: DEVICE_ID, CONF_HOST: "192.168.1.50"},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert entry.data[CONF_HOST] == "192.168.1.50"


@pytest.mark.asyncio
async def test_resync_updates_changed_local_key(hass, mocker):
    """A key regenerated by a device reset should be repaired from cloud."""
    hass.data.setdefault(DOMAIN, {})
    cache = await async_get_cache(hass)
    await cache.async_set_auth({"user_code": "abc"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="test",
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: "stalekey",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    )
    entry.add_to_hass(hass)

    async def fake_get_devices():
        await cache.async_update_devices(
            {"a": {"id": DEVICE_ID, CONF_LOCAL_KEY: TESTKEY, "name": "Light"}}
        )
        return {}

    cloud = mocker.MagicMock()
    cloud.is_authenticated = True
    cloud.async_get_devices = fake_get_devices
    mocker.patch("custom_components.tuya_local.cloud.Cloud", return_value=cloud)

    assert await async_resync_local_key(hass, entry) is True
    assert entry.data[CONF_LOCAL_KEY] == TESTKEY


@pytest.mark.asyncio
async def test_resync_is_throttled(hass, mocker):
    """Offline devices must not cause a cloud lookup on every retry."""
    hass.data.setdefault(DOMAIN, {})
    cache = await async_get_cache(hass)
    await cache.async_set_auth({"user_code": "abc"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="test",
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    )
    entry.add_to_hass(hass)

    cloud = mocker.MagicMock()
    cloud.is_authenticated = True
    cloud.async_get_devices = mocker.AsyncMock(return_value={})
    mocker.patch("custom_components.tuya_local.cloud.Cloud", return_value=cloud)

    assert await async_resync_local_key(hass, entry) is False
    assert await async_resync_local_key(hass, entry) is False
    assert cloud.async_get_devices.await_count == 1


@pytest.mark.asyncio
async def test_resync_skipped_without_cloud_credentials(hass, mocker):
    """Users who never logged in to the cloud should not trigger lookups."""
    hass.data.setdefault(DOMAIN, {})
    await async_get_cache(hass)
    cloud = mocker.patch("custom_components.tuya_local.cloud.Cloud")

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="test",
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    )
    entry.add_to_hass(hass)

    assert await async_resync_local_key(hass, entry) is False
    cloud.assert_not_called()


@pytest.mark.asyncio
async def test_resync_uses_cid_for_gateway_subdevices(hass, mocker):
    """A sub device's key must not be replaced by its gateway's key."""
    hass.data.setdefault(DOMAIN, {})
    cache = await async_get_cache(hass)
    await cache.async_set_auth({"user_code": "abc"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="subnode",
        title="test",
        data={
            CONF_DEVICE_ID: "gatewayid",
            CONF_DEVICE_CID: "subnode",
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: "stalekey",
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "kogan_kahtp_heater",
        },
    )
    entry.add_to_hass(hass)

    async def fake_get_devices():
        await cache.async_update_devices(
            {
                "hub": {
                    "id": "gatewayid",
                    CONF_LOCAL_KEY: "gatewaykey",
                    "name": "Gateway",
                },
                "sub": {
                    "id": "subdeviceid",
                    CONF_LOCAL_KEY: TESTKEY,
                    "name": "Sub device",
                    "node_id": "subnode",
                },
            }
        )
        return {}

    cloud = mocker.MagicMock()
    cloud.is_authenticated = True
    cloud.async_get_devices = fake_get_devices
    mocker.patch("custom_components.tuya_local.cloud.Cloud", return_value=cloud)

    assert await async_resync_local_key(hass, entry) is True
    # The sub device's own key, not the gateway's.
    assert entry.data[CONF_LOCAL_KEY] == TESTKEY


@pytest.mark.asyncio
async def test_cloud_cache_keeps_known_key_on_partial_response(hass):
    """A cloud response missing a key must not erase the cached one."""
    cache = CloudCache(hass)
    await cache.async_update_devices(
        {"a": {"id": DEVICE_ID, CONF_LOCAL_KEY: TESTKEY, "name": "Light"}}
    )
    assert cache.get_local_key(DEVICE_ID) == TESTKEY

    await cache.async_update_devices({"a": {"id": DEVICE_ID, CONF_LOCAL_KEY: ""}})
    assert cache.get_local_key(DEVICE_ID) == TESTKEY


@pytest.mark.asyncio
async def test_cloud_cache_survives_reload(hass):
    """Credentials should be readable by a fresh cache instance."""
    cache = CloudCache(hass)
    await cache.async_set_auth({"user_code": "abc"})
    await cache.async_update_devices(
        {"a": {"id": DEVICE_ID, CONF_LOCAL_KEY: TESTKEY, "name": "Light"}}
    )

    reloaded = CloudCache(hass)
    await reloaded.async_load()
    assert reloaded.auth == {"user_code": "abc"}
    assert reloaded.get_local_key(DEVICE_ID) == TESTKEY
    assert reloaded.get_device(DEVICE_ID)["name"] == "Light"


@pytest.mark.asyncio
async def test_discovery_quick_add_creates_entry(hass, mocker):
    """A cached key and an exact match leave nothing worth asking."""
    mocker.patch("custom_components.tuya_local.async_setup_entry", return_value=True)
    cache = await async_get_cache(hass)
    await cache.async_update_devices(
        {
            DEVICE_ID: {
                "id": DEVICE_ID,
                CONF_LOCAL_KEY: TESTKEY,
                "name": "Cached light",
                "product_id": "abcdefghijklmnop",
                "product_name": "Light",
            }
        }
    )
    mock_device = mocker.MagicMock()
    mock_device._protocol_configured = "3.3"
    mock_device._product_ids = []
    mock_type = mocker.MagicMock()
    mock_type.legacy_type = "kogan_kahtp_heater"
    mock_type.config_type = "kogan_kahtp_heater"
    mock_type.match_quality.return_value = 100
    mock_type.product_display_entries.return_value = [(None, None)]
    mock_device.async_possible_types = mocker.AsyncMock(return_value=[mock_type])
    mocker.patch(
        "custom_components.tuya_local.config_flow.async_test_connection",
        return_value=mock_device,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_HOST: "192.168.1.50",
            "version": "3.3",
        },
    )
    assert result["step_id"] == "discovery_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_QUICK_ADD: True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cached light"
    assert result["data"][CONF_HOST] == "192.168.1.50"
    assert result["data"][CONF_LOCAL_KEY] == TESTKEY
    assert result["data"][CONF_TYPE] == "kogan_kahtp_heater"


@pytest.mark.asyncio
async def test_discovery_quick_add_falls_back_without_a_key(hass):
    """Without a key the connection form is still needed."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_DEVICE_ID: DEVICE_ID, CONF_HOST: "192.168.1.50"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_QUICK_ADD: True}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local"
