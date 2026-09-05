"""OAuth state must be bound to the initiating browser and configured account."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from aiohttp.test_utils import make_mocked_request
from twitchio import Scopes

from voxer.oauth import MAX_PENDING_STATES, STATE_COOKIE, SecureOAuthAdapter


def make_adapter(redirect="http://localhost:4343/oauth/callback"):
    adapter = SecureOAuthAdapter(
        redirect_url=redirect,
        expected_user_id="123",
        client_id="application",
        scopes=Scopes(["user:read:chat"]),
        host="0.0.0.0",
        port=4343,
    )
    adapter.client = SimpleNamespace(
        _http=SimpleNamespace(
            user_access_token=AsyncMock(
                return_value=SimpleNamespace(
                    access_token="access", user_id=None, user_login=None
                )
            ),
            validate_token=AsyncMock(
                return_value=SimpleNamespace(
                    user_id="123",
                    client_id="application",
                    scopes=["user:read:chat"],
                    login="bot",
                )
            ),
        ),
        dispatch=Mock(),
    )
    return adapter


async def begin(adapter):
    response = await adapter.oauth_redirect(
        make_mocked_request(
            "GET",
            "/oauth?scopes=channel:manage:ads",
            headers={"Host": "localhost:4343"},
        )
    )
    params = parse_qs(urlsplit(response.headers["Location"]).query)
    assert params["scope"] == ["user:read:chat"]
    assert params["redirect_uri"] == [adapter.redirect_url]
    return params["state"][0], response.cookies[STATE_COOKIE].value


def callback(state, cookie, *, code="code"):
    return make_mocked_request(
        "GET",
        f"/oauth/callback?{urlencode({'state': state, 'code': code})}",
        headers={"Host": "localhost:4343", "Cookie": f"{STATE_COOKIE}={cookie}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["absent", "wrong-browser", "expired"])
async def test_invalid_state_never_exchanges_code(invalid):
    adapter = make_adapter()
    state, cookie = await begin(adapter)
    if invalid == "absent":
        state = ""
    elif invalid == "wrong-browser":
        cookie = "attacker"
    else:
        adapter._pending_states[state] = (cookie, 0)
    response = await adapter.oauth_callback(callback(state, cookie))
    assert response.status == 400
    adapter.client._http.user_access_token.assert_not_awaited()
    adapter.client.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_state_is_single_use_and_exchange_uses_registered_redirect():
    adapter = make_adapter()
    state, cookie = await begin(adapter)
    assert (await adapter.oauth_callback(callback(state, cookie))).status == 200
    assert (await adapter.oauth_callback(callback(state, cookie))).status == 400
    adapter.client._http.user_access_token.assert_awaited_once_with(
        "code", redirect_uri=adapter.redirect_url
    )
    adapter.client.dispatch.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["user", "client", "scopes"])
async def test_wrong_identity_or_scope_is_not_dispatched(invalid):
    adapter = make_adapter()
    result = adapter.client._http.validate_token.return_value
    if invalid == "user":
        result.user_id = "other"
    elif invalid == "client":
        result.client_id = "other"
    else:
        result.scopes = []
    state, cookie = await begin(adapter)
    assert (await adapter.oauth_callback(callback(state, cookie))).status == 403
    adapter.client.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_oauth_start_rejects_host_injection():
    adapter = make_adapter()
    request = make_mocked_request("GET", "/oauth", headers={"Host": "evil.example"})
    assert (await adapter.oauth_redirect(request)).status == 400
    assert not adapter._pending_states


@pytest.mark.asyncio
async def test_oauth_pending_state_is_bounded():
    adapter = make_adapter()
    for _ in range(MAX_PENDING_STATES):
        await begin(adapter)
    request = make_mocked_request("GET", "/oauth", headers={"Host": "localhost:4343"})
    assert (await adapter.oauth_redirect(request)).status == 429
    assert len(adapter._pending_states) == MAX_PENDING_STATES


@pytest.mark.asyncio
async def test_public_callback_cookie_is_secure_and_port_is_preserved():
    adapter = make_adapter("https://bot.example:8443/custom/callback")
    request = make_mocked_request("GET", "/oauth", headers={"Host": "bot.example:8443"})
    response = await adapter.oauth_redirect(request)
    cookie = response.cookies[STATE_COOKIE]
    assert cookie["secure"]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert cookie["path"] == "/custom/callback"
