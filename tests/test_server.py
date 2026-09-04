"""Unit tests for voxer.server: the overlay routes, the WebSocket protocol and
the broadcast fan-out.

Three groups of tests, each driving the code the way production does:

  - `resolve_audio_file` is a pure function, so it is called directly.
  - The HTTP routes and the WebSocket endpoint are exercised through the real
    Starlette application with starlette's `TestClient`, which speaks ASGI to
    the app in a background thread instead of opening a real network socket.
    That means these tests go through the same routing, the same JSON parsing
    and the same file deletion as a live OBS browser source does, so they keep
    holding after the endpoint's internals are rearranged.
  - `broadcast()` is called directly with hand-written fake sockets, because
    the behaviours worth pinning (every live client is reached; a client that
    raises is dropped without stopping the others; a client that accepts
    nothing is dropped rather than allowed to hold the pipeline up; and the
    count of clients actually reached, which is what tells the handler whether
    an MP3 will ever be played) cannot be provoked through a real connection on
    demand.

A note on timing.  `TestClient` hands a WebSocket message to the application
asynchronously: `send_text` returns as soon as the message is queued, not once
the server has acted on it.  So instead of asserting straight after the send,
these tests wait (with a short deadline) for the observable effect — the MP3
disappearing from disk.  Where a test needs to prove that something did *not*
happen, it sends a second, valid message afterwards and waits for *that* one to
take effect: messages on a single socket are processed in order, so once the
later effect is visible the earlier message has definitely been handled too.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from voxer.models import BroadcastEvent, EmoteItem
from voxer.server import AudioServer, resolve_audio_file

# How long to wait for the server thread to act on a WebSocket message before
# declaring the test failed.  Generous, because it is only ever paid in full
# when something is genuinely broken; the happy path returns in milliseconds.
_DELETION_TIMEOUT_SECONDS = 5.0

# The per-client send deadline every server built here runs with.  Production
# uses seconds (VOXER_WS_SEND_TIMEOUT, default 5); a quarter of a second is
# still thousands of times longer than an in-memory fake needs to accept a
# string, and it is what keeps the stalled-client test quick.
_TEST_SEND_TIMEOUT = 0.25


def _wait_until_deleted(path: Path) -> bool:
    """Poll until `path` is gone, returning False if it outlives the deadline."""
    deadline = time.monotonic() + _DELETION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def server(tmp_path: Path) -> AudioServer:
    """An AudioServer whose audio directory is a fresh temporary directory.

    Host and port are never used: `serve()` is what binds the socket and no
    test calls it, so nothing here listens on the network.  The send deadline
    is deliberately tiny so a test can stall a client without stalling itself.
    """
    return AudioServer(tmp_path, "127.0.0.1", 0, _TEST_SEND_TIMEOUT)


# --------------------------------------------------------------------------
# resolve_audio_file — the path-traversal guard
# --------------------------------------------------------------------------


def test_plain_filename_allowed(tmp_path: Path) -> None:
    resolved = resolve_audio_file(tmp_path, "abc.mp3")
    assert resolved == tmp_path.resolve() / "abc.mp3"


def test_parent_traversal_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "../secret.txt") is None


def test_absolute_path_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "/etc/passwd") is None


def test_subdirectory_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "sub/dir.mp3") is None


def test_deep_traversal_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "../../../../etc/passwd") is None


# --------------------------------------------------------------------------
# The WebSocket endpoint — steps 4 and 5 of the audio-file lifecycle
# --------------------------------------------------------------------------


def test_done_message_deletes_the_audio_file(
    server: AudioServer, tmp_path: Path
) -> None:
    """The confirmation the browser sends after playback removes the MP3.

    This is the last step of the lifecycle described in the module docstring
    of voxer/server.py, and the reason the audio directory does not fill up.
    """
    clip = tmp_path / "a.mp3"
    clip.write_bytes(b"fake mp3 bytes")

    with TestClient(server._app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"done": "a.mp3"}))
            assert _wait_until_deleted(clip)


def test_done_message_with_traversal_leaves_outside_file_alone(
    server: AudioServer, tmp_path: Path
) -> None:
    """A filename pointing outside the audio directory deletes nothing.

    The filename comes from the browser, so a compromised or hostile overlay
    could ask for any path on disk.  The connection deliberately stays open
    after the rejection — a bad name is a bad message, not a bad client — which
    is what the follow-up `done` proves.
    """
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("do not delete me")
    clip = tmp_path / "b.mp3"
    clip.write_bytes(b"fake mp3 bytes")

    try:
        with TestClient(server._app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"done": "../secret.txt"}))
                # Messages on one socket are handled in order, so once this
                # second deletion has happened the first message is long done.
                ws.send_text(json.dumps({"done": "b.mp3"}))
                assert _wait_until_deleted(clip)
                assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.parametrize("garbage", ["not json", "42", "null", "[1]"])
def test_garbage_message_does_not_kill_the_connection(
    server: AudioServer, tmp_path: Path, garbage: str
) -> None:
    """Unparseable or non-object messages are logged and skipped, not fatal.

    "not json" fails to parse at all; "42", "null" and "[1]" parse into a
    number, None and a list respectively — all valid JSON, none of them
    something `.get("done")` can be called on.  Before either guard existed
    such a message raised inside the endpoint and tore down that client's
    connection, so the overlay went silent until the page was reloaded.
    """
    clip = tmp_path / "c.mp3"
    clip.write_bytes(b"fake mp3 bytes")

    with TestClient(server._app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(garbage)
            ws.send_text(json.dumps({"done": "c.mp3"}))
            assert _wait_until_deleted(clip)


# --------------------------------------------------------------------------
# The HTTP routes the OBS browser source loads
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/simple"])
def test_overlay_pages_are_served_uncacheable(server: AudioServer, path: str) -> None:
    """Both overlay pages load, and neither may be cached.

    OBS keeps its own HTTP cache, and a cached page keeps running the old
    overlay JavaScript after an update, so `Cache-Control: no-store` is part
    of the contract rather than an incidental header.
    """
    with TestClient(server._app) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_favicon_is_served(server: AudioServer) -> None:
    """/favicon.ico answers so the browser does not log a 404 on every load."""
    with TestClient(server._app) as client:
        response = client.get("/favicon.ico")

    assert response.status_code == 200


# --------------------------------------------------------------------------
# broadcast() — the fan-out to every connected overlay
# --------------------------------------------------------------------------


class FakeSocket:
    """A stand-in for a connected WebSocket, recording what was sent to it.

    `broadcast()` only ever calls `send_text` on a client, so that single
    method is the whole surface a fake needs.  With `raises=True` the socket
    behaves like one whose browser vanished between two messages: the send
    blows up, which is the case broadcast() has to survive.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.sent: list[str] = []
        self._raises = raises

    async def send_text(self, message: str) -> None:
        if self._raises:
            raise RuntimeError("socket is closed")
        self.sent.append(message)


class StalledSocket:
    """A client that is still connected but has stopped reading its socket.

    This is what a paused OBS browser source or a suspended laptop looks like
    from the server: nothing raises and nothing closes, the send just never
    finishes.  `asyncio.Event().wait()` reproduces that exactly — it blocks
    forever and, unlike `time.sleep`, yields to the event loop so the rest of
    the broadcast can be observed while this one client hangs.
    """

    def __init__(self) -> None:
        self.closed = False

    async def send_text(self, message: str) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


def _event() -> BroadcastEvent:
    return BroadcastEvent(
        audio_url="/audio/clip.mp3",
        username="chatter",
        avatar_url="https://example.invalid/avatar.png",
        emotes=[EmoteItem(name="Kappa", url="https://example.invalid/kappa.png")],
    )


async def test_broadcast_reaches_every_connected_client(server: AudioServer) -> None:
    """Every live client receives the same JSON payload, nested emotes included."""
    first, second = FakeSocket(), FakeSocket()
    # The client set is populated by the WebSocket endpoint in production;
    # here it is filled by hand because broadcast() only duck-types its
    # members, calling send_text and nothing else.
    server._clients = {first, second}

    delivered = await server.broadcast(_event())

    assert delivered == 2

    expected: dict[str, Any] = {
        "audio_url": "/audio/clip.mp3",
        "username": "chatter",
        "avatar_url": "https://example.invalid/avatar.png",
        "emotes": [{"name": "Kappa", "url": "https://example.invalid/kappa.png"}],
    }
    assert [json.loads(payload) for payload in first.sent] == [expected]
    assert [json.loads(payload) for payload in second.sent] == [expected]


async def test_broadcast_drops_a_raising_client_and_still_serves_the_rest(
    server: AudioServer,
) -> None:
    """One dead socket neither hides the message from the others nor lingers.

    Two things matter here.  The healthy client must still get its message,
    which is why the removal is collected during the loop rather than raising
    out of it; and the dead client must be gone from the set afterwards, so
    the next broadcast does not pay for it again.
    """
    dead, alive = FakeSocket(raises=True), FakeSocket()
    server._clients = {dead, alive}

    delivered = await server.broadcast(_event())

    assert delivered == 1
    assert len(alive.sent) == 1
    assert server._clients == {alive}


async def test_broadcast_with_no_clients_reports_zero_deliveries(
    server: AudioServer,
) -> None:
    """With nothing connected, broadcasting is harmless and reports 0 reached.

    This is the common case while the stream is offline but the bot is running,
    and the zero is load-bearing rather than cosmetic: it is what tells
    MessageHandler that no browser will ever send back the "done" message for
    this clip, so the MP3 has to be deleted at the source instead of being left
    in the audio directory for nobody.
    """
    delivered = await server.broadcast(_event())

    assert delivered == 0
    assert server._clients == set()


async def test_broadcast_drops_a_stalled_client_and_still_serves_the_rest(
    server: AudioServer,
) -> None:
    """A client that accepts nothing is given up on, not waited for.

    Clients are served one after another, and broadcast() is awaited from the
    single task that drains the message queue, so a browser that stops reading
    its socket used to hold up the entire bot: no exception was raised and no
    connection was closed, the send simply never completed.  Chat kept arriving,
    the bounded queue filled, and messages were dropped with nothing logged to
    say why.  Now the send has a deadline, and missing it costs that one client
    its connection and nothing else.

    The whole call is wrapped in a deadline of its own.  Without it, a
    regression here would hang the test suite instead of failing it — the exact
    failure mode this test exists to prevent.
    """
    stalled, alive = StalledSocket(), FakeSocket()
    server._clients = {stalled, alive}

    async with asyncio.timeout(_TEST_SEND_TIMEOUT * 20):
        delivered = await server.broadcast(_event())

    # The healthy client is served whichever order the set happens to iterate in
    assert delivered == 1
    assert len(alive.sent) == 1
    assert server._clients == {alive}
    # Closed as well as forgotten, so the endpoint holding that connection
    # finishes instead of sitting in receive_text() until uvicorn's keepalive
    # eventually notices.
    assert stalled.closed
