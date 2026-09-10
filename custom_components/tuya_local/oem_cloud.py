"""Local keys from the cloud behind a rebranded Tuya app.

Brands like LEDVANCE and Sylvania ship their own phone app rather than
SmartLife, so their devices never appear in a Tuya developer account and the
QR code login cannot see them. Those apps talk to the same Tuya mobile API as
SmartLife does, identified only by a brand specific key pair, so signing in
with the app's own credentials gives us the local keys for those devices.

The API description this is built from was published by FlagX under the MIT
licence in ha-ledvance-tuya-resync-localkey.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Each brand's app is the same software signed with a different key pair.
OEM_BRANDS = {
    "ledvance": {
        "name": "LEDVANCE SMART+",
        "client_id": "fx3fvkvusmw45d7jn8xh",
        "secret": "A_armptsqyfpxa4ftvtc739ardncett3uy_cgqx3ku34mh5qdesd7fcaru3gx7tyurr",
    },
    "sylvania": {
        "name": "SYLVANIA Smart",
        "client_id": "creq75hn4vdg5qvrgryp",
        "secret": "A_ag4xcmp9rjttkj9yf9e8c3wfxry7yr44_wparh3scdv8dc7rrnuegaf9mqmn4snpk",
    },
}
# An account lives in one region and is invisible from the others.
OEM_REGIONS = ("eu", "us", "cn")
DEFAULT_REGION = "eu"

# Identifies the app to the API. The app version is part of the signature
# contract rather than a description of this code, so it is fixed.
APP_VERSION = "1.1.6"
APP_RN_VERSION = "5.14"
SDK_VERSION = "3.10.0"
USER_AGENT = "TY-UA=APP/Android/1.1.6/SDK/null"
# Stands in for a phone's install id. The API only requires it to be stable.
DEVICE_ID = "5fe5abb36728cce7b9cd2185625edccbd6d9bd787e40"

# Only these fields take part in the signature, in this order once sorted.
SIGNED_KEYS = frozenset(
    {
        "a",
        "v",
        "lat",
        "lon",
        "lang",
        "deviceId",
        "imei",
        "imsi",
        "appVersion",
        "ttid",
        "isH5",
        "h5Token",
        "os",
        "clientId",
        "postData",
        "time",
        "requestId",
        "n4h5",
        "sid",
        "sp",
        "et",
    }
)

TIMEOUT = 30


class OemCloudError(Exception):
    """The cloud refused or could not answer a request."""


class OemCloudAuthError(OemCloudError):
    """The credentials were not accepted."""


def _mobile_hash(data: str) -> str:
    """Hash a request body the way the app does.

    It is an ordinary md5 with its four 8 character blocks swapped in pairs.
    """
    prehash = hashlib.md5(data.encode("utf-8")).hexdigest()  # noqa: S324
    return prehash[8:16] + prehash[0:8] + prehash[24:32] + prehash[16:24]


def _encrypt_password(public_key: str, exponent: str, password: str) -> str:
    """Encrypt a password the way the app does.

    The password is hashed, then encrypted with the public key the server
    just offered, using RSA without any padding. Padding would normally be
    essential, but the server does it this way and both sides have to agree.

    The result is left padded with zeros to the width of the key, which is
    what the server expects and is not the natural width of the number.
    """
    modulus = int(public_key)
    digest = hashlib.md5(password.encode("utf-8")).hexdigest().encode("utf-8")  # noqa: S324
    encrypted = pow(int.from_bytes(digest, "big"), int(exponent), modulus)
    width = (modulus.bit_length() + 7) // 8
    return encrypted.to_bytes(width, "big").hex()


class OemCloud:
    """Client for the mobile API behind a rebranded Tuya app."""

    def __init__(
        self,
        hass: HomeAssistant,
        brand: str = "ledvance",
        region: str = DEFAULT_REGION,
    ) -> None:
        self._hass = hass
        self._brand = brand if brand in OEM_BRANDS else "ledvance"
        self._region = region if region in OEM_REGIONS else DEFAULT_REGION
        self._client_id = OEM_BRANDS[self._brand]["client_id"]
        self._secret = OEM_BRANDS[self._brand]["secret"]
        self._endpoint = f"https://a1.tuya{self._region}.com/api.json"
        self._sid: str | None = None

    def _sign(self, params: dict[str, str]) -> str:
        """Sign a request with the brand's secret."""
        parts = []
        for key in sorted(params):
            value = params[key]
            if key not in SIGNED_KEYS or not value:
                continue
            if key == "postData":
                value = _mobile_hash(value)
            parts.append(f"{key}={value}")
        return hmac.new(
            self._secret.encode("utf-8"),
            msg="||".join(parts).encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    async def _async_call(
        self,
        action: str,
        post_data: dict[str, Any] | None = None,
        version: str = "1.0",
        extra: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> Any:
        """Make one signed call and unwrap its answer."""
        params = {
            "a": action,
            "appVersion": APP_VERSION,
            "appRnVersion": APP_RN_VERSION,
            "channel": "oem",
            "clientId": self._client_id,
            "deviceId": DEVICE_ID,
            "et": "0.0.1",
            "lang": "en",
            "os": "Android",
            "osSystem": "9",
            "platform": "Linux",
            "requestId": str(uuid.uuid4()),
            "sdkVersion": SDK_VERSION,
            "time": str(int(time.time())),
            "timeZoneId": str(self._hass.config.time_zone or "UTC"),
            "ttid": f"sdk_tuya@{self._client_id}",
            "v": version,
        }
        if authenticated:
            if not self._sid:
                raise OemCloudAuthError("Not signed in")
            params["sid"] = self._sid
        if extra:
            params.update(extra)

        body = None
        if post_data is not None:
            body = json.dumps(post_data, separators=(",", ":"))
            params["postData"] = body

        params["sign"] = self._sign(params)

        session = async_get_clientsession(self._hass)
        response = await session.post(
            self._endpoint,
            params=params,
            data={"postData": body} if body is not None else None,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        )
        result = await response.json(content_type=None)

        if result.get("success"):
            return result.get("result")

        code = result.get("errorCode")
        message = result.get("errorMsg", code)
        _LOGGER.debug("Cloud call %s failed: %s", action, message)
        if code in ("USER_PASSWD_WRONG", "USER_NOT_EXISTS", "USER_SESSION_INVALID"):
            raise OemCloudAuthError(message)
        raise OemCloudError(message)

    async def async_login(self, email: str, password: str) -> None:
        """Sign in to the brand's cloud with the app's credentials."""
        token = await self._async_call(
            "tuya.m.user.email.token.create",
            {"countryCode": "", "email": email},
            authenticated=False,
        )
        login = await self._async_call(
            "tuya.m.user.email.password.login",
            {
                "countryCode": "",
                "email": email,
                "ifencrypt": 1,
                "options": '{"group": 1}',
                "passwd": _encrypt_password(
                    token["publicKey"],
                    token["exponent"],
                    password,
                ),
                "token": token["token"],
            },
            authenticated=False,
        )
        self._sid = login.get("sid")
        if not self._sid:
            raise OemCloudAuthError("No session was returned")

    async def async_get_devices(self) -> dict[str, dict[str, Any]]:
        """Return every device in the account, keyed by device id.

        Devices belong to homes, so the homes have to be listed first. The
        local key is part of each device, which is the whole point of asking.
        """
        devices: dict[str, dict[str, Any]] = {}
        for home in await self._async_call("tuya.m.location.list") or []:
            group_id = home.get("groupId")
            if group_id is None:
                continue
            found = await self._async_call(
                "tuya.m.my.group.device.list",
                extra={"gid": str(group_id)},
            )
            for device in found or []:
                device_id = device.get("devId")
                if not device_id:
                    continue
                devices[device_id] = {
                    "id": device_id,
                    "ip": "",
                    "local_key": device.get("localKey", ""),
                    "name": device.get("name") or device_id,
                    "product_id": device.get("productId", ""),
                    "product_name": device.get("productName")
                    or OEM_BRANDS[self._brand]["name"],
                    "category": device.get("category", ""),
                    "online": bool(device.get("isOnline")),
                    "node_id": device.get("nodeId", ""),
                    "uuid": device.get("uuid", ""),
                    "is_hub": False,
                    "sub": bool(device.get("nodeId")),
                }
        _LOGGER.debug("Found %d devices in the %s account", len(devices), self._brand)
        return devices

    async def async_get_datamodel(self, device_id: str) -> list[dict[str, Any]] | None:
        """Return the datapoint spec of a device, as the SmartLife cloud does.

        Without this, devices bought under a brand's own label can only be
        reported as a bare list of datapoint values, which is rarely enough
        to tell what a datapoint means.
        """
        info = await self._async_call("tuya.m.device.get", {"devId": device_id})
        if not info:
            return None

        schema = info.get("schema")
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except ValueError:
                _LOGGER.debug("Could not read the schema of %s", device_id)
                return None
        if not isinstance(schema, list):
            return None

        transform = []
        for entry in schema:
            if not isinstance(entry, dict):
                continue
            # Values live under "property", except for its type, which the
            # rest of the integration reports separately.
            spec = entry.get("property")
            spec = dict(spec) if isinstance(spec, dict) else {}
            datatype = spec.pop("type", entry.get("type"))
            transform.append(
                {
                    "id": entry.get("id"),
                    "name": entry.get("code") or entry.get("name"),
                    "type": datatype,
                    "format": spec,
                    "mode": entry.get("mode"),
                }
            )
        return transform
