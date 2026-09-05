"""Authenticated overlay delivery with bounded, per-client audio lifetimes."""

import asyncio
import contextlib
import dataclasses
import hmac
import json
import logging
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from .models import AUDIO_URL_PREFIX, BroadcastEvent, is_audio_filename

LOGGER = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).parent / "static"
DONE_FIELD: Final[str] = "done"
_COOKIE = "voxer_overlay"
_MAX_WS_MESSAGE = 1024
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; img-src 'self' https: data:; "
        "media-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


class _OverlayAccess:
    """Authenticate every HTTP/WS route before serving files or accepting a socket.

    OBS URLs can include ?token=... once. Only overlay page requests exchange
    that token for an HttpOnly cookie and immediately redirect to a clean URL.
    Native clients may use an Authorization: Bearer header instead.
    """

    def __init__(self, app: ASGIApp, *, token: str, hosts: Collection[str]) -> None:
        self.app = app
        self.token = token
        self.hosts = {host.lower().strip("[]") for host in hosts}

    def _matches(self, candidate: str) -> bool:
        return hmac.compare_digest(
            candidate.encode("utf-8"), self.token.encode("utf-8")
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        host = conn.headers.get("host", "")
        try:
            parsed = urlsplit("//" + host)
            hostname = parsed.hostname or ""
            # Reading port validates malformed/out-of-range ports too.
            parsed.port
            valid_host = (
                hostname.lower() in self.hosts
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and len(conn.headers.getlist("host")) == 1
            )
        except ValueError:
            valid_host = False
        if not valid_host:
            await self._deny(scope, receive, send, 400)
            return

        origin = conn.headers.get("origin")
        scheme = "https" if scope["scheme"] in {"https", "wss"} else "http"
        if origin is not None and origin != f"{scheme}://{host}":
            await self._deny(scope, receive, send, 403)
            return

        if self.token and scope["path"] != "/healthz":
            auth = conn.headers.get("authorization", "")
            bearer = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
            authenticated = self._matches(bearer) or self._matches(
                conn.cookies.get(_COOKIE, "")
            )
            if (
                scope["type"] == "http"
                and scope["method"] == "GET"
                and scope["path"] in {"/", "/simple"}
            ):
                query_token = conn.query_params.get("token")
                if query_token is not None:
                    if not self._matches(query_token):
                        await self._deny(scope, receive, send, 401)
                        return
                    query = urlencode(
                        [
                            (key, value)
                            for key, value in conn.query_params.multi_items()
                            if key != "token"
                        ]
                    )
                    target = scope["path"] + ("?" + query if query else "")
                    response = RedirectResponse(
                        target, status_code=303, headers=_SECURITY_HEADERS
                    )
                    response.set_cookie(
                        _COOKIE,
                        self.token,
                        httponly=True,
                        secure=scheme == "https",
                        samesite="strict",
                    )
                    await response(scope, receive, send)
                    return
            if not authenticated:
                await self._deny(scope, receive, send, 401)
                return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for key, value in _SECURITY_HEADERS.items():
                    encoded = key.lower().encode("ascii")
                    headers = [
                        (name, item)
                        for name, item in headers
                        if name.lower() != encoded
                    ]
                    headers.append((encoded, value.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secure_send)

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, status: int) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await Response(status_code=status, headers=_SECURITY_HEADERS)(
                scope, receive, send
            )


def resolve_audio_file(audio_dir: Path, filename: object) -> Path | None:
    """Accept only simple MP3 basenames, excluding symlinks and traversal."""
    if not is_audio_filename(filename):
        return None
    try:
        root = audio_dir.resolve()
        candidate = root / filename
        if candidate.is_symlink():
            return None
        path = candidate.resolve()
        return path if path.parent == root else None
    except OSError, RuntimeError, ValueError:
        return None


def sweep_audio_dir(
    audio_dir: Path, min_age_secs: float = 0.0, spare: Collection[str] = ()
) -> int:
    """Remove abandoned MP3s; live receipts are spared until their hard TTL."""
    cutoff = time.time() - min_age_secs
    removed = 0
    for mp3 in audio_dir.glob("*.mp3"):
        if mp3.name in spare:
            continue
        try:
            if min_age_secs > 0 and mp3.stat().st_mtime > cutoff:
                continue
            mp3.unlink()
        except OSError:
            LOGGER.debug("Could not remove an abandoned audio file", exc_info=True)
            continue
        removed += 1
    return removed


async def reap_audio(
    audio_dir: Path,
    interval: float,
    min_age: float,
    outstanding: Callable[[], Collection[str]] = lambda: (),
) -> None:
    """Sweep off the event loop; snapshot receipt ownership on its owning loop."""
    while True:
        await asyncio.sleep(interval)
        removed = await asyncio.to_thread(
            sweep_audio_dir, audio_dir, min_age, outstanding()
        )
        if removed:
            LOGGER.info(
                "Reaped %d orphaned audio file(s) older than %ss", removed, min_age
            )


class _QuietServer(uvicorn.Server):
    """Leave signal handling to the application's TaskGroup composition root."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass
class _Receipt:
    created_at: float
    clients: set[WebSocket] = field(default_factory=set)


class AudioServer:
    """Fan out events concurrently and retain clips until every recipient is done."""

    def __init__(
        self,
        audio_dir: Path,
        host: str,
        port: int,
        send_timeout: float,
        *,
        overlay_token: str = "",
        allowed_hosts: tuple[str, ...] = (),
        max_clients: int = 8,
        max_pending_per_client: int = 64,
        audio_max_age: float = 300,
        trusted_proxies: tuple[str, ...] = (),
    ) -> None:
        self._audio_dir = audio_dir
        self._host = host
        self._port = port
        self._send_timeout = send_timeout
        self._max_clients = max_clients
        self._max_pending = max_pending_per_client
        self._audio_max_age = audio_max_age
        self._trusted_proxies = trusted_proxies
        self._clients: set[WebSocket] = set()
        self._outstanding: dict[str, _Receipt] = {}
        self._pending: dict[WebSocket, set[str]] = {}
        hosts = set(allowed_hosts) | {"localhost", "127.0.0.1", "::1"}
        if host not in {"0.0.0.0", "::", "[::]"}:
            hosts.add(host)
        self._app = Starlette(
            middleware=[Middleware(_OverlayAccess, token=overlay_token, hosts=hosts)],
            routes=[
                Route("/", self._handle_index),
                Route("/simple", self._handle_simple),
                Route("/favicon.ico", self._handle_favicon),
                Route("/healthz", self._handle_health),
                WebSocketRoute("/ws", self._handle_ws),
                Mount("/static", StaticFiles(directory=_STATIC_DIR)),
                Route(f"{AUDIO_URL_PREFIX}/{{filename}}", self._handle_audio),
            ],
        )

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    def outstanding_files(self) -> frozenset[str]:
        """Expire receipts even when a connected browser never acknowledges."""
        cutoff = time.monotonic() - self._audio_max_age
        for filename, receipt in list(self._outstanding.items()):
            if receipt.created_at <= cutoff:
                for client in receipt.clients:
                    self._pending.get(client, set()).discard(filename)
                del self._outstanding[filename]
                self._unlink_audio(filename)
        return frozenset(self._outstanding)

    async def _handle_index(self, request: Request) -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    async def _handle_simple(self, request: Request) -> FileResponse:
        return FileResponse(_STATIC_DIR / "simple.html")

    async def _handle_favicon(self, request: Request) -> Response:
        return Response(content=b"", media_type="image/x-icon")

    async def _handle_health(self, request: Request) -> Response:
        return Response(content="ok", media_type="text/plain")

    async def _handle_audio(self, request: Request) -> Response:
        path = resolve_audio_file(self._audio_dir, request.path_params["filename"])
        if path is None or not await asyncio.to_thread(path.is_file):
            return Response(status_code=404)
        return FileResponse(path, media_type="audio/mpeg")

    async def _handle_ws(self, websocket: WebSocket) -> None:
        # Reserve the slot before accept() yields, preventing concurrent joins
        # from all passing the same capacity check.
        if len(self._clients) >= self._max_clients:
            await websocket.close(code=1013)
            return
        self._clients.add(websocket)
        self._pending[websocket] = set()
        try:
            await websocket.accept()
            LOGGER.info("Overlay connected: %d client(s)", len(self._clients))
            window_start = time.monotonic()
            messages = 0
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                data = frame.get("text")
                if (
                    not isinstance(data, str)
                    or len(data.encode("utf-8")) > _MAX_WS_MESSAGE
                ):
                    await websocket.close(code=1009)
                    break
                now = time.monotonic()
                if now - window_start >= 1:
                    window_start, messages = now, 0
                messages += 1
                if messages > 128:
                    await websocket.close(code=1008)
                    break
                try:
                    message = json.loads(data)
                except json.JSONDecodeError, RecursionError:
                    continue
                if isinstance(message, dict):
                    self._delete_played_audio(message.get(DONE_FIELD), websocket)
        except WebSocketDisconnect:
            pass
        finally:
            self._drop_client(websocket)
            LOGGER.info("Overlay disconnected: %d client(s)", len(self._clients))

    def _unlink_audio(self, filename: str) -> None:
        path = resolve_audio_file(self._audio_dir, filename)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("Audio cleanup deferred to reaper", exc_info=True)

    def _delete_played_audio(self, filename: object, client: WebSocket) -> None:
        if not isinstance(filename, str):
            return
        receipt = self._outstanding.get(filename)
        if receipt is None or client not in receipt.clients:
            return
        receipt.clients.discard(client)
        self._pending.get(client, set()).discard(filename)
        if not receipt.clients:
            del self._outstanding[filename]
            self._unlink_audio(filename)

    def _drop_client(self, client: WebSocket) -> None:
        self._clients.discard(client)
        for filename in list(self._pending.pop(client, ())):
            self._delete_played_audio(filename, client)

    async def _close_quietly(self, websocket: WebSocket) -> None:
        try:
            async with asyncio.timeout(self._send_timeout):
                await websocket.close()
        except Exception:
            LOGGER.debug("Ignoring failure while closing a client", exc_info=True)

    async def broadcast(self, event: BroadcastEvent) -> int:
        self.outstanding_files()
        filename = Path(event.audio_url).name
        if (
            event.audio_url != f"{AUDIO_URL_PREFIX}/{filename}"
            or resolve_audio_file(self._audio_dir, filename) is None
        ):
            raise ValueError("Broadcast audio URL must name a local MP3")
        clients = list(self._clients)
        if not clients:
            return 0
        message = json.dumps(dataclasses.asdict(event))
        # Record ALL owners before any send yields: a fast ACK must never
        # delete the file before another recipient has even been registered.
        receipt = self._outstanding.setdefault(filename, _Receipt(time.monotonic()))
        eligible: list[WebSocket] = []
        overloaded: list[WebSocket] = []
        for client in clients:
            pending = self._pending.setdefault(client, set())
            if len(pending) >= self._max_pending:
                overloaded.append(client)
                self._drop_client(client)
            else:
                pending.add(filename)
                receipt.clients.add(client)
                eligible.append(client)
        if not receipt.clients:
            self._outstanding.pop(filename, None)

        async def deliver(client: WebSocket) -> int:
            try:
                async with asyncio.timeout(self._send_timeout):
                    await client.send_text(message)
                return 1
            except Exception:
                self._drop_client(client)
                await self._close_quietly(client)
                LOGGER.warning("Dropped stalled or disconnected overlay")
                return 0

        results = await asyncio.gather(
            *(deliver(client) for client in eligible),
            *(self._close_quietly(client) for client in overloaded),
        )
        return sum(result for result in results if isinstance(result, int))

    async def serve(self) -> None:
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            proxy_headers=bool(self._trusted_proxies),
            forwarded_allow_ips=",".join(self._trusted_proxies),
            ws_max_size=_MAX_WS_MESSAGE,
            ws_max_queue=8,
            ws_per_message_deflate=False,
        )
        try:
            await _QuietServer(config).serve()
        finally:
            clients = list(self._clients)
            for client in clients:
                self._drop_client(client)
            await asyncio.gather(*(self._close_quietly(client) for client in clients))
