"""Regression coverage for overlay access, ownership, resource limits and cleanup."""

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voxer.models import BroadcastEvent, EmoteItem, audio_url_for
from voxer.server import (
    AudioServer,
    _QuietServer,
    reap_audio,
    resolve_audio_file,
    sweep_audio_dir,
)

TOKEN = "test-overlay-token-32-characters-long"


@pytest.fixture
def server(tmp_path: Path) -> AudioServer:
    return AudioServer(tmp_path, "127.0.0.1", 0, 0.1, allowed_hosts=("testserver",))


def _event(filename: str = "clip.mp3") -> BroadcastEvent:
    return BroadcastEvent(
        f"/audio/{filename}",
        "chatter",
        "https://example.invalid/avatar.png",
        [EmoteItem("Kappa", "https://example.invalid/kappa.png")],
    )


def _clip(directory: Path, name: str = "clip.mp3", age: float = 0) -> Path:
    path = directory / name
    path.write_bytes(b"audio")
    if age:
        then = time.time() - age
        os.utime(path, (then, then))
    return path


def _wait_deleted(path: Path) -> None:
    deadline = time.monotonic() + 2
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not path.exists()


def _publish(client: TestClient, server: AudioServer, name: str = "clip.mp3") -> int:
    assert client.portal is not None
    return client.portal.call(server.broadcast, _event(name))


@pytest.mark.parametrize(
    "name",
    [
        "../secret.txt",
        "/etc/passwd",
        "sub/clip.mp3",
        "a/../clip.mp3",
        "notes.txt",
        "",
        "x\x00.mp3",
        "x\\clip.mp3",
        None,
        42,
        [],
        {},
    ],
)
def test_invalid_audio_names_rejected(tmp_path: Path, name: object) -> None:
    assert resolve_audio_file(tmp_path, name) is None


@pytest.mark.parametrize(
    "name", ["clip.mp3", "_clip.mp3", "-clip.mp3", "a" * 128 + ".mp3"]
)
def test_generated_audio_urls_can_be_served(server, tmp_path, name) -> None:
    _clip(tmp_path, name)
    with TestClient(server._app) as client:
        assert client.get(audio_url_for(name)).content == b"audio"


@pytest.mark.parametrize(
    "name", ["clip.v2.mp3", "../clip.mp3", "a" * 129 + ".mp3", "clip.mp3\n"]
)
def test_domain_rejects_names_the_server_cannot_deliver(tmp_path, name) -> None:
    with pytest.raises(ValueError):
        audio_url_for(name)
    assert resolve_audio_file(tmp_path, name) is None


def test_plain_mp3_and_symlink_rules(tmp_path: Path) -> None:
    path = _clip(tmp_path)
    assert resolve_audio_file(tmp_path, path.name) == path.resolve()
    alias = tmp_path / "alias.mp3"
    alias.symlink_to(path)
    assert resolve_audio_file(tmp_path, alias.name) is None


@pytest.mark.parametrize(
    "path", ["/", "/simple", "/static/overlay.js", "/audio/clip.mp3"]
)
def test_auth_protects_every_overlay_resource(tmp_path: Path, path: str) -> None:
    _clip(tmp_path)
    protected = AudioServer(tmp_path, "127.0.0.1", 0, 0.1, overlay_token=TOKEN)
    with TestClient(protected._app, base_url="http://127.0.0.1") as client:
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code
            == 200
        )
        assert (
            client.get(path, headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )


def test_bootstrap_cookie_redirect_and_minimal_health(tmp_path: Path) -> None:
    protected = AudioServer(tmp_path, "127.0.0.1", 0, 0.1, overlay_token=TOKEN)
    with TestClient(protected._app, base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").text == "ok"
        assert client.get("/?token=wrong").status_code == 401
        response = client.get(
            f"/simple?volume=0.4&token={TOKEN}&debug=1", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/simple?volume=0.4&debug=1"
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "Secure" not in response.headers["set-cookie"]
        assert response.headers["referrer-policy"] == "no-referrer"
        assert client.get("/simple").status_code == 200
        with client.websocket_connect(
            "ws://127.0.0.1/ws", headers={"Origin": "http://127.0.0.1"}
        ):
            assert protected.has_clients


def test_https_bootstrap_cookie_is_secure(tmp_path: Path) -> None:
    protected = AudioServer(tmp_path, "127.0.0.1", 0, 0.1, overlay_token=TOKEN)
    with TestClient(protected._app, base_url="https://127.0.0.1") as client:
        response = client.get(f"/?token={TOKEN}", follow_redirects=False)
        assert "Secure" in response.headers["set-cookie"]
        with client.websocket_connect(
            "wss://127.0.0.1/ws", headers={"Origin": "https://127.0.0.1"}
        ):
            assert protected.has_clients


def test_host_and_origin_reject_rebinding_and_cross_site_requests(
    server: AudioServer,
) -> None:
    with TestClient(server._app) as client:
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
        assert (
            client.get("/", headers={"Host": "testserver:invalid"}).status_code == 400
        )
        assert (
            client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400
        )
        assert (
            client.get("/", headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws", headers={"Origin": "https://evil.example"}
            ):
                pytest.fail("cross-site socket accepted")


def test_ws_requires_cookie_or_bearer_not_query_token(tmp_path: Path) -> None:
    protected = AudioServer(
        tmp_path,
        "127.0.0.1",
        0,
        0.1,
        overlay_token=TOKEN,
        allowed_hosts=("testserver",),
    )
    with TestClient(protected._app) as client:
        for path in ("/ws", f"/ws?token={TOKEN}"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(path):
                    pytest.fail("unauthenticated socket accepted")
        with client.websocket_connect(
            "/ws", headers={"Authorization": f"Bearer {TOKEN}"}
        ):
            assert protected.has_clients


@pytest.mark.parametrize("path", ["/", "/simple"])
def test_overlay_pages_are_uncacheable_with_local_script_policy(
    server: AudioServer, path: str
) -> None:
    with TestClient(server._app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'self';" in response.headers["content-security-policy"]
    assert "https://esm.sh" not in response.text
    assert "https://cdn.jsdelivr.net/npm/pixi" not in response.text
    assert "'unsafe-eval'" not in response.headers["content-security-policy"]


@pytest.mark.parametrize("path", ["/", "/simple"])
def test_overlay_executable_code_is_local_and_external(server, path):
    from html.parser import HTMLParser

    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            assert not any(name.startswith("on") for name, _ in attrs)
            if tag == "script":
                src = dict(attrs).get("src")
                assert src and src.startswith("/static/")
                self.scripts.append(src)

    parser = Assets()
    with TestClient(server._app) as client:
        parser.feed(client.get(path).text)
        assert "/static/overlay.js" in parser.scripts
        for src in parser.scripts:
            assert client.get(src).status_code == 200


def test_audio_route_excludes_non_audio_files_and_links(
    server: AudioServer, tmp_path: Path
) -> None:
    (tmp_path / "secret.txt").write_text("private")
    (tmp_path / "alias.mp3").symlink_to(tmp_path / "secret.txt")
    _clip(tmp_path)
    with TestClient(server._app) as client:
        assert client.get("/audio/secret.txt").status_code == 404
        assert client.get("/audio/alias.mp3").status_code == 404
        assert client.get("/audio/clip.mp3").content == b"audio"
        assert client.get("/favicon.ico").status_code == 200


@pytest.mark.parametrize(
    "garbage",
    [
        "not json",
        "42",
        "null",
        "[1]",
        '{"done":42}',
        '{"done":[]}',
        '{"done":{}}',
        '{"done":"../secret.txt"}',
    ],
)
def test_invalid_ack_cannot_delete_unowned_file_or_kill_socket(
    server: AudioServer, tmp_path: Path, garbage: str
) -> None:
    owned = _clip(tmp_path)
    unrelated = _clip(tmp_path, "unrelated.mp3")
    with TestClient(server._app) as client:
        with client.websocket_connect("/ws") as ws:
            assert _publish(client, server) == 1
            ws.receive_json()
            ws.send_text(garbage)
            ws.send_json({"done": unrelated.name})
            ws.send_json({"done": owned.name})
            _wait_deleted(owned)
            assert unrelated.exists()


def test_two_clients_must_both_acknowledge(server: AudioServer, tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    with TestClient(server._app) as client:
        with (
            client.websocket_connect("/ws") as first,
            client.websocket_connect("/ws") as second,
        ):
            assert _publish(client, server) == 2
            first.receive_json()
            second.receive_json()
            first.send_json({"done": clip.name})
            # A subsequent broadcast is observed after the first ACK is sent;
            # the fake-socket race regression below pins exact registration order.
            time.sleep(0.02)
            assert clip.exists()
            second.send_json({"done": clip.name})
            _wait_deleted(clip)


def test_disconnect_releases_only_that_clients_receipts(
    server: AudioServer, tmp_path: Path
) -> None:
    clip = _clip(tmp_path)
    with TestClient(server._app) as client:
        with client.websocket_connect("/ws") as first:
            with client.websocket_connect("/ws") as second:
                assert _publish(client, server) == 2
                first.receive_json()
                second.receive_json()
            assert clip.exists()
        _wait_deleted(clip)
    assert not server.has_clients
    assert server.outstanding_files() == frozenset()


def test_ws_frame_and_rate_bounds(server: AudioServer) -> None:
    with TestClient(server._app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text("x" * 1025)
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_text()
            assert error.value.code == 1009
        with client.websocket_connect("/ws") as ws:
            for _ in range(129):
                ws.send_text("{}")
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_text()
            assert error.value.code == 1008


def test_ws_connection_limit(tmp_path: Path) -> None:
    limited = AudioServer(
        tmp_path, "127.0.0.1", 0, 0.1, allowed_hosts=("testserver",), max_clients=1
    )
    with TestClient(limited._app) as client:
        with client.websocket_connect("/ws"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws"):
                    pytest.fail("connection limit exceeded")


class FakeSocket:
    def __init__(self, *, fail: bool = False, stall: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.stall = stall
        self.closed = False
        self.received = asyncio.Event()
        self.on_send: Any = None

    async def send_text(self, message: str) -> None:
        if self.fail:
            raise RuntimeError("disconnected")
        if self.stall:
            await asyncio.Event().wait()
        self.sent.append(message)
        self.received.set()
        if self.on_send:
            self.on_send()

    async def close(self) -> None:
        self.closed = True


async def test_fanout_serialization_and_zero_clients(server: AudioServer) -> None:
    assert await server.broadcast(_event()) == 0
    first, second = FakeSocket(), FakeSocket()
    server._clients = {first, second}
    assert await server.broadcast(_event()) == 2
    assert first.sent == second.sent
    assert json.loads(first.sent[0])["emotes"] == [
        {"name": "Kappa", "url": "https://example.invalid/kappa.png"}
    ]


async def test_stalled_clients_are_concurrent_and_healthy_client_is_immediate(
    server: AudioServer,
) -> None:
    alive = FakeSocket()
    stalled = [FakeSocket(stall=True) for _ in range(4)]
    failed = FakeSocket(fail=True)
    server._clients = {alive, failed, *stalled}
    task = asyncio.create_task(server.broadcast(_event()))
    async with asyncio.timeout(0.07):
        await alive.received.wait()
    async with asyncio.timeout(0.25):
        assert await task == 1
    assert server._clients == {alive}
    assert all(socket.closed for socket in [failed, *stalled])


async def test_fast_ack_cannot_delete_before_other_delivery(
    server: AudioServer, tmp_path: Path
) -> None:
    clip = _clip(tmp_path)
    fast, other = FakeSocket(), FakeSocket()
    fast.on_send = lambda: server._delete_played_audio(clip.name, fast)
    server._clients = {fast, other}
    assert await server.broadcast(_event()) == 2
    assert clip.exists()
    server._delete_played_audio(clip.name, other)
    assert not clip.exists()


async def test_nonrecipient_cannot_acknowledge_clip(
    server: AudioServer, tmp_path: Path
) -> None:
    clip = _clip(tmp_path)
    owner, stranger = FakeSocket(), FakeSocket()
    server._clients = {owner}
    await server.broadcast(_event())
    server._delete_played_audio(clip.name, stranger)
    assert clip.exists()
    server._delete_played_audio(clip.name, owner)
    assert not clip.exists()


async def test_pending_capacity_drops_nonacknowledging_client(tmp_path: Path) -> None:
    limited = AudioServer(tmp_path, "127.0.0.1", 0, 0.1, max_pending_per_client=1)
    client = FakeSocket()
    limited._clients = {client}
    clip = _clip(tmp_path)
    assert await limited.broadcast(_event()) == 1
    assert await limited.broadcast(_event("next.mp3")) == 0
    assert client.closed
    assert not clip.exists()
    assert not limited.has_clients
    assert limited.outstanding_files() == frozenset()


async def test_hard_ttl_expires_acked_never_clips_with_connected_client(
    tmp_path: Path,
) -> None:
    limited = AudioServer(tmp_path, "127.0.0.1", 0, 0.1, audio_max_age=5)
    limited._clients = {FakeSocket()}
    clip = _clip(tmp_path)
    await limited.broadcast(_event())
    limited._outstanding[clip.name].created_at -= 10
    assert limited.outstanding_files() == frozenset()
    assert not clip.exists()
    assert limited.has_clients


def test_sweep_age_ownership_and_other_files(tmp_path: Path) -> None:
    fresh = _clip(tmp_path, "fresh.mp3")
    old = _clip(tmp_path, "old.mp3", age=600)
    pending = _clip(tmp_path, "pending.mp3", age=600)
    other = tmp_path / "notes.txt"
    other.write_text("keep")
    assert sweep_audio_dir(tmp_path, 300, {pending.name}) == 1
    assert not old.exists()
    assert fresh.exists() and pending.exists() and other.exists()
    assert sweep_audio_dir(tmp_path) == 2
    assert other.exists()
    assert sweep_audio_dir(tmp_path / "missing") == 0


async def test_reaper_removes_orphans_without_blocking_event_loop(
    tmp_path: Path,
) -> None:
    orphan = _clip(tmp_path, age=600)
    task = asyncio.create_task(reap_audio(tmp_path, 0.005, 300))
    try:
        async with asyncio.timeout(2):
            while orphan.exists():
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not orphan.exists()


def test_quiet_server_keeps_application_signal_handlers() -> None:
    signals = (signal.SIGINT, signal.SIGTERM)
    before = {sig: signal.getsignal(sig) for sig in signals}
    config = uvicorn.Config(lambda *args: None, log_config=None)
    with _QuietServer(config).capture_signals():
        assert {sig: signal.getsignal(sig) for sig in signals} == before
    assert {sig: signal.getsignal(sig) for sig in signals} == before


async def test_serve_uses_bounded_protocol_and_closes_clients(
    server: AudioServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[uvicorn.Server] = []
    client = FakeSocket()
    server._clients = {client}

    async def fake_serve(self: uvicorn.Server, sockets: Any = None) -> None:
        started.append(self)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    await server.serve()
    assert isinstance(started[0], _QuietServer)
    assert started[0].config.ws_max_size == 1024
    assert started[0].config.ws_max_queue == 8
    assert not started[0].config.access_log
    assert not started[0].config.proxy_headers
    assert client.closed and not server.has_clients


async def test_serve_trusts_only_explicit_proxy_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[uvicorn.Server] = []
    server = AudioServer(
        tmp_path, "127.0.0.1", 0, 0.1, trusted_proxies=("127.0.0.1", "10.1.0.0/24")
    )

    async def fake_serve(self: uvicorn.Server, sockets: Any = None) -> None:
        started.append(self)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    await server.serve()
    assert started[0].config.proxy_headers
    assert started[0].config.forwarded_allow_ips == "127.0.0.1,10.1.0.0/24"
