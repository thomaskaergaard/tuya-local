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
    ) -> None:
        self._on_device = on_device
        self._transports: list[asyncio.DatagramTransport] = []
        self._seen: dict[str, DiscoveredDevice] = {}
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

    async def _async_probe_loop(self) -> None:
        """Broadcast discovery requests until discovery is stopped."""
        while True:
            await self.async_request_devices()
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
