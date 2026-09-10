"""
Discovery of Tuya devices on the local network.

Tuya devices periodically broadcast a small JSON announcement over UDP.
Listening for those broadcasts lets us find devices without scanning, and
lets us notice when a device changes its IP address.

Protocol 3.5 devices do not announce themselves, they only answer a
discovery request, so requests are broadcast as well. Broadcasts do not
cross subnets, so networks that hold devices Home Assistant cannot reach by
broadcast can be probed by unicast instead.
"""

import asyncio
import ipaddress
import json
import logging
import socket
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import tinytuya
from tinytuya import UDPPORT, UDPPORTAPP, UDPPORTS, decrypt_udp
from tinytuya.core.udp_helper import udpkey
from tinytuya.scanner import send_discovery_request

_LOGGER = logging.getLogger(__name__)

# 6666 is plaintext (protocol 3.1), 6667 is AES encrypted (3.2 to 3.4),
# 7000 is used by 3.5 devices and the mobile app.
DISCOVERY_PORTS = (UDPPORT, UDPPORTS, UDPPORTAPP)

# Protocol 3.1 to 3.4 devices announce themselves every few seconds, but 3.5
# devices stay silent until they are asked, so a request has to be broadcast
# for them to be found at all.
PROBE_INTERVAL = 60

# Probing a remote network means one packet per address, so refuse to expand
# a range large enough to make that unreasonable. /22 is 1022 addresses.
MAX_PROBE_HOSTS = 1024

# Devices accept commands on this port. Unlike the UDP announcements it is a
# normal outgoing connection, so it is routed between subnets and its replies
# are part of the same flow, which firewalls between VLANs will allow.
TUYA_TCP_PORT = 6668
SCAN_TIMEOUT = 2.0
SCAN_CONCURRENCY = 64

# Tried in order of how quickly a wrong key is rejected, so that identifying
# a device costs as little time as possible. 3.5 is last because it only
# fails once the connection times out.
IDENTIFY_VERSIONS = (3.3, 3.4, 3.1, 3.5)
IDENTIFY_TIMEOUT = 3
# How long a single scan may spend working out which device is at which
# address, after which the rest are reported as unidentified.
IDENTIFY_BUDGET = 60
# Scanning is far more work than a broadcast, so it runs much less often.
SCAN_INTERVAL = 300


def expand_probe_network(network: str) -> list[str]:
    """List the addresses to probe for a configured network.

    Accepts a single address, or a CIDR range in which case every host
    address in the range is returned.
    """
    try:
        parsed = ipaddress.ip_network(network, strict=False)
    except ValueError as e:
        _LOGGER.error("Ignoring invalid discovery network %s: %s", network, e)
        return []

    if parsed.version != 4:
        _LOGGER.error("Ignoring discovery network %s: only IPv4 is supported", network)
        return []

    if parsed.prefixlen == parsed.max_prefixlen:
        # A plain address, which may also be a directed broadcast address.
        return [str(parsed.network_address)]

    hosts = [str(host) for host in parsed.hosts()]
    if len(hosts) > MAX_PROBE_HOSTS:
        _LOGGER.error(
            "Ignoring discovery network %s: %d addresses is more than the %d allowed",
            network,
            len(hosts),
            MAX_PROBE_HOSTS,
        )
        return []
    return hosts


def _source_address_for(target: str) -> str:
    """The local address that will be used to reach a target.

    Connecting a UDP socket sends nothing, it just applies the routing table,
    which tells us the address the device should reply to.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((target, UDPPORTAPP))
        return sock.getsockname()[0]


def build_probe_request(source_ip: str) -> bytes:
    """Build a discovery request that asks for a reply to source_ip.

    Devices reply to the address carried in the request rather than to the
    address the request came from, so this has to be the address that is
    reachable from the device's network.
    """
    payload = json.dumps({"from": "app", "ip": source_ip}).encode()
    message = tinytuya.TuyaMessage(
        0,
        tinytuya.REQ_DEVINFO,
        None,
        payload,
        0,
        True,
        tinytuya.PREFIX_6699_VALUE,
        True,
    )
    return tinytuya.pack_message(message, hmac_key=udpkey)


def send_unicast_probes(targets: list[str]) -> None:
    """Send a discovery request to each address individually.

    Used for networks that broadcasts do not reach, such as devices on a
    separate VLAN. Unicast is routed normally, and so is the reply.
    """
    if not targets:
        return
    try:
        source_ip = _source_address_for(targets[0])
    except OSError as e:
        _LOGGER.debug("No route to %s for discovery: %s", targets[0], e)
        return

    request = build_probe_request(source_ip)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            try:
                sock.sendto(request, (target, UDPPORTAPP))
            except OSError as e:
                _LOGGER.debug("Could not probe %s: %s", target, e)


@dataclass(frozen=True)
class DiscoveredDevice:
    """A device that announced itself on the local network."""

    device_id: str
    ip: str
    product_id: str | None = None
    version: str | None = None


def parse_discovery_message(data: bytes) -> DiscoveredDevice | None:
    """Decode a raw UDP broadcast into a DiscoveredDevice.

    Returns None if the packet is not a Tuya announcement we understand.
    """
    try:
        decoded = decrypt_udp(data)
        message = json.loads(decoded)
    except Exception as e:
        # Anything that is not a well formed Tuya broadcast is ignored. The
        # ports are shared with other Tuya tooling, so this is expected.
        _LOGGER.debug("Ignoring undecodable discovery packet: %s %s", type(e), e)
        return None

    if not isinstance(message, dict):
        return None

    device_id = message.get("gwId")
    ip = message.get("ip")
    if not device_id or not ip:
        return None

    version = message.get("version")
    return DiscoveredDevice(
        device_id=str(device_id),
        ip=str(ip),
        product_id=message.get("productKey"),
        version=str(version) if version else None,
    )


async def async_find_tuya_hosts(
    targets: list[str],
    timeout: float = SCAN_TIMEOUT,
    concurrency: int = SCAN_CONCURRENCY,
) -> list[str]:
    """Find the addresses that accept Tuya device connections.

    Broadcasts do not cross subnets, and a device's reply to a discovery
    request arrives as a new inbound connection which a firewall between
    VLANs will usually drop. Connecting to each address instead is ordinary
    routed traffic, so it works wherever the devices are reachable at all.
    """
    if not targets:
        return []

    semaphore = asyncio.Semaphore(concurrency)

    async def check(host: str) -> str | None:
        async with semaphore:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, TUYA_TCP_PORT),
                    timeout,
                )
            except OSError, TimeoutError:
                return None
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return host

    found = await asyncio.gather(*(check(host) for host in targets))
    return [host for host in found if host]


def identify_host(host: str, keys: dict[str, str]) -> DiscoveredDevice | None:
    """Work out which device is at an address, using known local keys.

    A device only identifies itself to someone who already holds its key, so
    this can only name devices that have been seen in the cloud account.
    """
    for device_id, local_key in keys.items():
        for version in IDENTIFY_VERSIONS:
            try:
                device = tinytuya.Device(
                    device_id,
                    host,
                    local_key,
                    version=version,
                )
                device.set_socketTimeout(IDENTIFY_TIMEOUT)
                status = device.status()
            except Exception as e:
                _LOGGER.debug("Error identifying %s as %s: %s", host, device_id, e)
                continue
            if isinstance(status, dict) and "dps" in status:
                return DiscoveredDevice(
                    device_id=device_id,
                    ip=host,
                    version=str(version),
                )
    return None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Handle datagrams received on a single discovery port."""

    def __init__(self, on_datagram: Callable[[bytes, str], None]) -> None:
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._on_datagram(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Discovery socket error: %s", exc)


class TuyaLocalDiscovery:
    """Listen for Tuya device broadcasts on the local network."""

    def __init__(
        self,
        on_device: Callable[[DiscoveredDevice], Coroutine[Any, Any, None]],
        networks: list[str] | None = None,
        key_lookup: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._on_device = on_device
        self._key_lookup = key_lookup
        self._transports: list[asyncio.DatagramTransport] = []
        self._seen: dict[str, DiscoveredDevice] = {}
        self._unidentified: list[str] = []
        self._tasks: set[asyncio.Task] = set()
        self._probe_task: asyncio.Task | None = None
        self._probe_targets: list[str] = []
        for network in networks or []:
            self._probe_targets.extend(expand_probe_network(network))
        if self._probe_targets:
            _LOGGER.debug(
                "Discovery will also probe %d configured addresses",
                len(self._probe_targets),
            )

    @property
    def unidentified(self) -> list[str]:
        """Addresses of devices found by scanning that could not be named.

        Naming a device needs its local key, so devices that are not in the
        cloud account, or that were found before logging in, end up here.
        """
        return list(self._unidentified)

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        """All devices seen since discovery started, keyed by device id."""
        return dict(self._seen)

    async def async_start(self) -> None:
        """Begin listening on all of the Tuya discovery ports."""
        loop = asyncio.get_running_loop()
        for port in DISCOVERY_PORTS:
            transport = await self._async_listen(loop, port)
            if transport is not None:
                self._transports.append(transport)

        if not self._transports:
            _LOGGER.warning(
                "Could not listen on any Tuya discovery port, "
                "automatic device discovery is unavailable",
            )
            return

        self._probe_task = loop.create_task(self._async_probe_loop())

    async def async_request_devices(self) -> None:
        """Ask devices on the local network to announce themselves.

        Protocol 3.5 devices only answer when asked, so without this they are
        never discovered. Older devices ignore the request and keep to their
        own broadcast schedule.
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, send_discovery_request)
        except Exception as e:
            # Broadcasting can fail on unusual network setups (no broadcast
            # capable interface, container networking). Devices that announce
            # themselves are still found, so this is not fatal.
            _LOGGER.debug("Unable to broadcast a discovery request: %s %s", type(e), e)

        if not self._probe_targets:
            return
        try:
            await loop.run_in_executor(
                None,
                send_unicast_probes,
                self._probe_targets,
            )
        except Exception as e:
            _LOGGER.debug("Unable to probe configured networks: %s %s", type(e), e)

    async def async_scan_networks(self) -> None:
        """Find devices on configured networks by connecting to them.

        Devices on a separate VLAN never announce themselves to us, and their
        answer to a discovery request is a new inbound connection that a
        firewall between the networks will usually drop. Connecting to them
        is ordinary routed traffic, so it works whenever the devices are
        reachable at all.
        """
        if not self._probe_targets:
            return

        hosts = await async_find_tuya_hosts(self._probe_targets)
        known_ips = {device.ip for device in self._seen.values()}
        hosts = [host for host in hosts if host not in known_ips]
        if not hosts:
            self._unidentified = []
            return

        _LOGGER.debug("Found %d devices on configured networks", len(hosts))
        keys = self._key_lookup() if self._key_lookup else {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + IDENTIFY_BUDGET
        unidentified = []

        for host in hosts:
            device = None
            if keys and loop.time() < deadline:
                device = await loop.run_in_executor(None, identify_host, host, keys)
            if device is None:
                unidentified.append(host)
            else:
                self._record(device)

        self._unidentified = unidentified

    async def async_locate_device(
        self,
        device_id: str,
        local_key: str,
    ) -> DiscoveredDevice | None:
        """Find the address of one known device on the configured networks."""
        if not self._probe_targets or not local_key:
            return None

        hosts = await async_find_tuya_hosts(self._probe_targets)
        loop = asyncio.get_running_loop()
        for host in hosts:
            device = await loop.run_in_executor(
                None,
                identify_host,
                host,
                {device_id: local_key},
            )
            if device is not None:
                self._record(device)
                return device
        return None

    async def _async_probe_loop(self) -> None:
        """Broadcast discovery requests until discovery is stopped."""
        loop = asyncio.get_running_loop()
        next_scan = 0.0
        while True:
            await self.async_request_devices()
            if self._probe_targets and loop.time() >= next_scan:
                # Scanning is much more work than a broadcast, so it is done
                # far less often.
                await self.async_scan_networks()
                next_scan = loop.time() + SCAN_INTERVAL
            await asyncio.sleep(PROBE_INTERVAL)

    async def _async_listen(
        self,
        loop: asyncio.AbstractEventLoop,
        port: int,
    ) -> asyncio.DatagramTransport | None:
        """Bind a single discovery port, returning None if unavailable."""
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(self._handle_datagram),
                local_addr=("0.0.0.0", port),  # noqa: S104
                reuse_port=True,
                allow_broadcast=True,
            )
        except Exception as e:
            # Another application (or another HA integration) may already own
            # the port without SO_REUSEPORT, some platforms do not support
            # reuse_port, and sandboxed environments may forbid sockets
            # entirely. Discovery is best effort, so none of this is fatal.
            _LOGGER.warning(
                "Unable to listen for Tuya discovery on UDP port %d: %s",
                port,
                e,
            )
            return None

        _LOGGER.debug("Listening for Tuya discovery on UDP port %d", port)
        return transport

    def async_stop(self) -> None:
        """Stop listening."""
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None
        for transport in self._transports:
            transport.close()
        self._transports = []
        for task in self._tasks:
            task.cancel()
        self._tasks = set()

    def _handle_datagram(self, data: bytes, source_ip: str) -> None:
        """Process a raw broadcast, notifying only on new or changed devices."""
        device = parse_discovery_message(data)
        if device is None:
            return

        # The announcement carries its own IP address, but the encryption key
        # for these broadcasts is public, so anything on the LAN could claim
        # any address. Only trust an announcement that actually came from the
        # address it claims, otherwise a forged packet could redirect an
        # existing device entry to a host of the sender's choosing.
        if device.ip != source_ip:
            _LOGGER.debug(
                "Ignoring announcement for %s claiming address %s but sent from %s",
                device.device_id,
                device.ip,
                source_ip,
            )
            return

        self._record(device)

    def _record(self, device: DiscoveredDevice) -> None:
        """Remember a device, notifying only if it is new or has moved."""
        known = self._seen.get(device.device_id)
        self._seen[device.device_id] = device
        if known is not None and known.ip == device.ip:
            # Devices rebroadcast every few seconds, only act on changes.
            return

        _LOGGER.debug("Discovered Tuya device %s at %s", device.device_id, device.ip)
        task = asyncio.get_running_loop().create_task(self._notify(device))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, device: DiscoveredDevice) -> None:
        """Pass a discovered device to the callback, swallowing failures."""
        try:
            await self._on_device(device)
        except Exception as e:
            _LOGGER.error(
                "Error handling discovery of %s: %s %s",
                device.device_id,
                type(e),
                e,
            )
