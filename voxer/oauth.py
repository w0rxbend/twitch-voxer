"""Single-account Twitch OAuth with fixed redirects and browser-bound state."""

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit

from aiohttp import ClientError, web
from twitchio import HTTPException, Scopes
from twitchio.web import AiohttpAdapter
from twitchio.web.utils import FetchTokenPayload

LOGGER = logging.getLogger(__name__)
STATE_TTL_SECONDS = 300
MAX_PENDING_STATES = 64
STATE_COOKIE = "voxer_oauth_state"


class SecureOAuthAdapter(AiohttpAdapter):
    """Keep TwitchIO's lifecycle while validating the app's OAuth boundary."""

    def __init__(
        self,
        *,
        redirect_url: str,
        expected_user_id: str,
        client_id: str,
        scopes: Scopes,
        host: str,
        port: int,
    ) -> None:
        parsed = urlsplit(redirect_url)
        self._registered_redirect = redirect_url
        self._expected_user_id = expected_user_id
        self._client_id = client_id
        self._required_scopes = scopes
        self._pending_states: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._exchanges = 0
        super().__init__(
            host=host,
            port=port,
            domain=parsed.netloc if parsed.scheme == "https" else None,
            redirect_path=parsed.path,
        )

    @property
    def redirect_url(self) -> str:
        return self._registered_redirect

    def _find_redirect(self, request: web.Request) -> str:
        return self._registered_redirect

    def _valid_host(self, request: web.Request) -> bool:
        expected = urlsplit(self._registered_redirect)
        try:
            actual = urlsplit(f"{expected.scheme}://{request.host}")
            default_port = 443 if expected.scheme == "https" else 80
            return (
                actual.hostname == expected.hostname
                and (actual.port or default_port) == (expected.port or default_port)
                and actual.username is None
                and actual.password is None
            )
        except ValueError:
            return False

    def _prune_states(self) -> None:
        now = time.monotonic()
        while self._pending_states:
            _, (_, expiry) = next(iter(self._pending_states.items()))
            if expiry > now:
                break
            self._pending_states.popitem(last=False)

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    async def oauth_redirect(self, request: web.Request) -> web.Response:
        if not self._valid_host(request):
            return web.Response(status=400, text="Invalid OAuth host.")
        self._prune_states()
        if len(self._pending_states) >= MAX_PENDING_STATES:
            return web.Response(status=429, text="Too many pending authorizations.")
        state, browser_nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._pending_states[state] = (
            browser_nonce,
            time.monotonic() + STATE_TTL_SECONDS,
        )
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._registered_redirect,
                "response_type": "code",
                "scope": " ".join(self._required_scopes.selected),
                "state": state,
                "force_verify": "true",
            }
        )
        response = web.HTTPFound(
            f"https://id.twitch.tv/oauth2/authorize?{query}", headers=self._headers()
        )
        response.set_cookie(
            STATE_COOKIE,
            browser_nonce,
            max_age=STATE_TTL_SECONDS,
            httponly=True,
            secure=self._registered_redirect.startswith("https://"),
            samesite="Lax",
            path=self._redirect_path,
        )
        return response

    async def oauth_callback(self, request: web.Request) -> web.Response:
        self._prune_states()
        state = request.query.get("state", "")
        cookie = request.cookies.get(STATE_COOKIE, "")
        pending = self._pending_states.get(state)
        if (
            not self._valid_host(request)
            or len(request.query.getall("state", [])) != 1
            or pending is None
            or not cookie.isascii()
            or not secrets.compare_digest(pending[0], cookie)
        ):
            return web.Response(
                status=400,
                text="Invalid or expired OAuth state.",
                headers=self._headers(),
            )
        del self._pending_states[state]
        if "error" in request.query:
            response = web.Response(status=400, text="Twitch authorization was denied.")
        elif len(request.query.getall("code", [])) != 1 or not request.query["code"]:
            response = web.Response(status=400, text="Missing authorization code.")
        elif self._exchanges >= 4:
            response = web.Response(
                status=429, text="Authorization is busy. Try again."
            )
        else:
            self._exchanges += 1
            try:
                payload = await self.fetch_token(request)
                if not isinstance(payload.response, web.Response):
                    raise TypeError("Expected an aiohttp OAuth response")
                response = payload.response
            finally:
                self._exchanges -= 1
        response.headers.update(self._headers())
        response.del_cookie(STATE_COOKIE, path=self._redirect_path)
        return response

    async def fetch_token(self, request: web.Request) -> FetchTokenPayload:
        """Validate the grant before TwitchIO can dispatch or store it."""
        try:
            async with asyncio.timeout(15):
                payload = await self.client._http.user_access_token(
                    request.query["code"], redirect_uri=self._registered_redirect
                )
                validated = await self.client._http.validate_token(payload.access_token)
        except HTTPException, ClientError, TimeoutError:
            LOGGER.warning("Twitch OAuth token exchange failed")
            return FetchTokenPayload(
                status=502,
                response=web.Response(status=502, text="Twitch authorization failed."),
            )
        if (
            validated.client_id != self._client_id
            or validated.user_id != self._expected_user_id
            or not set(self._required_scopes.selected).issubset(validated.scopes)
        ):
            return FetchTokenPayload(
                status=403,
                response=web.Response(
                    status=403,
                    text="Authorize the configured bot account with all requested scopes.",
                ),
            )
        payload.user_id = validated.user_id
        payload.user_login = validated.login
        self.client.dispatch(event="oauth_authorized", payload=payload)
        return FetchTokenPayload(
            status=200,
            response=web.Response(text="Authorized. You can close this page."),
            payload=payload,
        )
