"""Tests for the cloud behind a rebranded Tuya app."""

import hashlib

import pytest

from custom_components.tuya_local.oem_cloud import (
    OEM_BRANDS,
    OemCloud,
    OemCloudAuthError,
    OemCloudError,
    _encrypt_password,
    _mobile_hash,
)

# Small enough to check by hand, large enough to hold a 32 byte message.
MODULUS = str(2**1023 + 1235)
EXPONENT = "3"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def test_mobile_hash_swaps_the_blocks_of_an_md5():
    """The app rearranges the digest before signing with it."""
    digest = hashlib.md5(b"payload").hexdigest()  # noqa: S324
    assert _mobile_hash("payload") == (
        digest[8:16] + digest[0:8] + digest[24:32] + digest[16:24]
    )


def test_password_is_hashed_then_encrypted_without_padding():
    """The server expects textbook RSA over the hash of the password."""
    encrypted = _encrypt_password(MODULUS, EXPONENT, "hunter2")

    ciphertext = bytes.fromhex(encrypted)
    # Padded out to the width of the key rather than of the number.
    assert len(ciphertext) == (int(MODULUS).bit_length() + 7) // 8

    expected = hashlib.md5(b"hunter2").hexdigest().encode("utf-8")  # noqa: S324
    assert int.from_bytes(ciphertext, "big") == pow(
        int.from_bytes(expected, "big"),
        int(EXPONENT),
        int(MODULUS),
    )


def test_encrypted_password_matches_the_reference_implementation():
    """The live keys are 1024 bit with exponent 3, as the app's are.

    The published implementation left pads with 32 zero bytes, which is
    only correct because the cube of a 32 byte number is 96 bytes wide.
    Padding to the width of the key gives the same answer without relying
    on that, and keeps working if the server ever offers a wider key.
    """
    modulus = str(2**1023 + 12345)
    encrypted = _encrypt_password(modulus, "3", "hunter2")

    digest = hashlib.md5(b"hunter2").hexdigest().encode("utf-8")  # noqa: S324
    reference = pow(int.from_bytes(digest, "big"), 3, int(modulus))
    width = (reference.bit_length() + 7) // 8
    assert width == 96
    assert encrypted == "0" * 64 + reference.to_bytes(width, "big").hex()
    assert len(encrypted) == 256


def test_signature_covers_only_the_agreed_fields(hass):
    """Fields outside the agreed list, such as the home id, are not signed."""
    cloud = OemCloud(hass, "ledvance", "eu")
    signed = cloud._sign({"a": "action", "time": "1", "gid": "99"})
    unsigned = cloud._sign({"a": "action", "time": "1"})
    assert signed == unsigned


def test_signature_hashes_the_body_rather_than_including_it(hass, mocker):
    """A long body is represented by its rearranged digest."""
    cloud = OemCloud(hass, "ledvance", "eu")
    sign = mocker.spy(
        __import__(
            "custom_components.tuya_local.oem_cloud",
            fromlist=["_mobile_hash"],
        ),
        "_mobile_hash",
    )
    cloud._sign({"a": "action", "postData": '{"x":1}'})
    sign.assert_called_once_with('{"x":1}')


def test_brands_have_distinct_credentials():
    """Each brand's app is signed with its own key pair."""
    ids = {brand["client_id"] for brand in OEM_BRANDS.values()}
    secrets = {brand["secret"] for brand in OEM_BRANDS.values()}
    assert len(ids) == len(OEM_BRANDS)
    assert len(secrets) == len(OEM_BRANDS)


def test_unknown_brand_and_region_fall_back(hass):
    """A bad choice must not build a nonsense endpoint."""
    cloud = OemCloud(hass, "nosuchbrand", "nosuchregion")
    assert cloud._endpoint == "https://a1.tuyaeu.com/api.json"
    assert cloud._client_id == OEM_BRANDS["ledvance"]["client_id"]


def test_region_selects_the_endpoint(hass):
    """An account is only visible in the region it was created in."""
    assert OemCloud(hass, "ledvance", "us")._endpoint == (
        "https://a1.tuyaus.com/api.json"
    )


@pytest.mark.asyncio
async def test_devices_are_collected_from_every_home(hass, mocker):
    """Devices belong to homes, so every home has to be asked."""
    cloud = OemCloud(hass)
    cloud._sid = "session"

    async def call(action, post_data=None, version="1.0", extra=None, **kwargs):
        if action == "tuya.m.location.list":
            return [{"groupId": 1}, {"groupId": 2}]
        assert action == "tuya.m.my.group.device.list"
        if extra["gid"] == "1":
            return [
                {
                    "devId": "first",
                    "localKey": "key1",
                    "name": "Kitchen light",
                    "productId": "prod",
                    "isOnline": True,
                }
            ]
        return [{"devId": "second", "localKey": "key2", "name": "Hall light"}]

    mocker.patch.object(cloud, "_async_call", side_effect=call)
    devices = await cloud.async_get_devices()

    assert set(devices) == {"first", "second"}
    assert devices["first"]["local_key"] == "key1"
    assert devices["first"]["name"] == "Kitchen light"
    assert devices["second"]["local_key"] == "key2"


@pytest.mark.asyncio
async def test_devices_without_an_id_are_ignored(hass, mocker):
    """A device we cannot name cannot be matched to anything either."""
    cloud = OemCloud(hass)
    cloud._sid = "session"

    async def call(action, post_data=None, version="1.0", extra=None, **kwargs):
        if action == "tuya.m.location.list":
            return [{"groupId": 1}]
        return [{"localKey": "key"}, {"devId": "ok", "localKey": "key"}]

    mocker.patch.object(cloud, "_async_call", side_effect=call)
    assert set(await cloud.async_get_devices()) == {"ok"}


@pytest.mark.asyncio
async def test_calls_need_a_session(hass):
    """Asking for devices before logging in is a programming error."""
    with pytest.raises(OemCloudAuthError):
        await OemCloud(hass)._async_call("tuya.m.location.list")


@pytest.mark.asyncio
async def test_login_stores_the_session(hass, mocker):
    """The session id is what authenticates every later call."""
    cloud = OemCloud(hass)
    calls = []

    async def call(action, post_data=None, **kwargs):
        calls.append((action, post_data))
        if action == "tuya.m.user.email.token.create":
            return {"publicKey": MODULUS, "exponent": EXPONENT, "token": "tok"}
        return {"sid": "session"}

    mocker.patch.object(cloud, "_async_call", side_effect=call)
    await cloud.async_login("me@example.com", "hunter2")

    assert cloud._sid == "session"
    assert calls[0][0] == "tuya.m.user.email.token.create"
    assert calls[1][0] == "tuya.m.user.email.password.login"
    # The password itself must never be sent.
    assert "hunter2" not in str(calls[1][1])
    assert calls[1][1]["token"] == "tok"  # noqa: S105


@pytest.mark.asyncio
async def test_login_without_a_session_is_an_error(hass, mocker):
    """An answer with no session cannot be used, so do not pretend it can."""
    cloud = OemCloud(hass)

    async def call(action, post_data=None, **kwargs):
        if action == "tuya.m.user.email.token.create":
            return {"publicKey": MODULUS, "exponent": EXPONENT, "token": "tok"}
        return {}

    mocker.patch.object(cloud, "_async_call", side_effect=call)
    with pytest.raises(OemCloudAuthError):
        await cloud.async_login("me@example.com", "hunter2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,expected",
    [
        ("USER_PASSWD_WRONG", OemCloudAuthError),
        ("USER_NOT_EXISTS", OemCloudAuthError),
        ("USER_SESSION_INVALID", OemCloudAuthError),
        ("SOMETHING_ELSE", OemCloudError),
    ],
)
async def test_failures_are_reported_as_the_right_kind(hass, mocker, code, expected):
    """A wrong password has to be told apart from the cloud being unwell."""
    cloud = OemCloud(hass)
    response = mocker.AsyncMock()
    response.json = mocker.AsyncMock(
        return_value={"success": False, "errorCode": code, "errorMsg": code}
    )
    session = mocker.MagicMock()
    session.post = mocker.AsyncMock(return_value=response)
    mocker.patch(
        "custom_components.tuya_local.oem_cloud.async_get_clientsession",
        return_value=session,
    )

    with pytest.raises(expected):
        await cloud._async_call(
            "tuya.m.user.email.token.create",
            {"email": "me@example.com"},
            authenticated=False,
        )


@pytest.mark.asyncio
async def test_request_is_signed_and_carries_the_body(hass, mocker):
    """The body travels as a form field and its hash travels in the signature."""
    cloud = OemCloud(hass)
    response = mocker.AsyncMock()
    response.json = mocker.AsyncMock(return_value={"success": True, "result": ["ok"]})
    session = mocker.MagicMock()
    session.post = mocker.AsyncMock(return_value=response)
    mocker.patch(
        "custom_components.tuya_local.oem_cloud.async_get_clientsession",
        return_value=session,
    )

    result = await cloud._async_call(
        "tuya.m.user.email.token.create",
        {"email": "me@example.com"},
        authenticated=False,
    )

    assert result == ["ok"]
    params = session.post.call_args.kwargs["params"]
    assert params["a"] == "tuya.m.user.email.token.create"
    assert params["clientId"] == OEM_BRANDS["ledvance"]["client_id"]
    assert params["sign"] == cloud._sign({k: v for k, v in params.items()})
    assert "sid" not in params
    assert session.post.call_args.kwargs["data"] == {
        "postData": '{"email":"me@example.com"}'
    }
