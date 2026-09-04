"""Unit tests for voxer.server: the overlay routes, the WebSocket protocol,
the broadcast fan-out, the orphaned-audio reaper and the uvicorn signal seam.

Five groups of tests, each driving the code the way production does:

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
  - `sweep_audio_dir` works on real files in a real temporary directory, with
    modification times pushed into the past by `os.utime` so a file can be
    "old" without the test waiting for it to age.  `reap_audio` is started as a
    real task with a tiny interval and cancelled at the end, the way the
    composition root's task group cancels it at shutdown.
  - `_QuietServer.capture_signals()` is called directly and the process's
    registered signal handlers are read back around it, because the thing being
    checked is a side effect on the operating system rather than a value.

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
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.testclient import TestClient

from voxer.models import BroadcastEvent, EmoteItem
from voxer.server import (
    AudioServer,
    _QuietServer,
    reap_audio,
    resolve_audio_file,
    sweep_audio_dir,
)

# How long to wait for the server thread to act on a WebSocket message before
# declaring the test failed.  Generous, because it is only ever paid in full
# when something is genuinely broken; the happy path returns in milliseconds.
_DELETION_TIMEOUT_SECONDS = 5.0

# The per-client send deadline every server built here runs with.  Production
# uses seconds (VOXER_WS_SEND_TIMEOUT, default 5); a quarter of a second is
# still thousands of times longer than an in-memory fake needs to accept a
# string, and it is what keeps the stalled-client test quick.
_TEST_SEND_TIMEOUT = 0.25

# How long the reaper waits between sweeps in the tests below.  Production uses
# five minutes (VOXER_AUDIO_SWEEP_INTERVAL_SECS); a hundredth of a second keeps
# these tests instant while exercising exactly the same loop.
_TEST_REAP_INTERVAL = 0.01


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


def test_last_client_leaving_releases_the_outstanding_clips(
    server: AudioServer,
) -> None:
    """When nobody is connected, an outstanding clip is an abandoned one.

    Outstanding names are held so the reaper does not delete a clip a browser
    is still going to play.  Once the last overlay disconnects, no "done"
    message can ever arrive for those names, so holding them would protect
    those files from the reaper for the rest of the process's life -- the exact
    leak the reaper exists to close.
    """
    with TestClient(server._app) as client:
        with client.websocket_connect("/ws"):
            server._outstanding.add("queued.mp3")
        # Leaving the `with` block closes the socket; the endpoint's cleanup
        # runs on the server side, so give it a moment to be observed.
        deadline = time.time() + 5
        while server._outstanding and time.time() < deadline:
            time.sleep(0.01)

    assert server.outstanding_files() == frozenset()


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


# --------------------------------------------------------------------------
# sweep_audio_dir / reap_audio — clearing up clips nobody came back for
# --------------------------------------------------------------------------


def _mp3(directory: Path, name: str, *, age_secs: float = 0.0) -> Path:
    """Create an MP3-named file and, optionally, backdate it.

    The reaper decides what to delete from a file's modification time, so a
    test needs files of a given age.  Waiting for one to get old would make the
    suite slow, so `os.utime` rewrites the timestamp instead: it is the same
    number `Path.stat().st_mtime` reports either way.
    """
    path = directory / name
    path.write_bytes(b"not really audio")
    if age_secs:
        past = time.time() - age_secs
        os.utime(path, (past, past))
    return path


def test_sweep_with_no_minimum_age_takes_every_file(tmp_path: Path) -> None:
    """This is the sweep at startup: nothing is playing, so nothing is spared.

    A brand-new file would survive any real age cutoff, which is exactly wrong
    here — the process that could have been playing it died with the previous
    run, so it is rubbish however recently it was written.
    """
    just_written = _mp3(tmp_path, "fresh.mp3")
    long_dead = _mp3(tmp_path, "old.mp3", age_secs=3600)

    removed = sweep_audio_dir(tmp_path)

    assert removed == 2
    assert not just_written.exists()
    assert not long_dead.exists()


def test_sweep_deletes_only_the_files_past_the_cutoff(tmp_path: Path) -> None:
    """With a minimum age, an abandoned clip goes and a current one stays.

    The sparing half is the one that matters most.  A file written seconds ago
    may still be queued for synthesis, on its way to a browser, or playing
    right now, and deleting it would cut the clip off mid-word — which is why
    the shipped cutoff (five minutes) is far longer than any clip can live.
    """
    abandoned = _mp3(tmp_path, "abandoned.mp3", age_secs=600)
    playing_now = _mp3(tmp_path, "playing.mp3")

    removed = sweep_audio_dir(tmp_path, min_age_secs=300)

    assert removed == 1
    assert not abandoned.exists()
    assert playing_now.exists()


def test_sweep_spares_a_clip_the_overlay_still_owes_a_done_message(
    tmp_path: Path,
) -> None:
    """An outstanding clip survives however old it looks.

    Age is only a guess at "nobody is coming back for this", and it is a bad
    guess while a browser is holding the clip: the overlay plays one at a time
    and keeps a queue, and a tab that has not been clicked yet is not allowed
    to play audio at all, so a legitimately outstanding clip can easily be
    older than the five-minute cutoff.  Deleting it would break the overlay's
    promise that every broadcast clip stays fetchable until it is played.
    """
    outstanding = _mp3(tmp_path, "queued.mp3", age_secs=600)
    abandoned = _mp3(tmp_path, "forgotten.mp3", age_secs=600)

    removed = sweep_audio_dir(tmp_path, min_age_secs=300, spare={"queued.mp3"})

    assert removed == 1
    assert outstanding.exists()
    assert not abandoned.exists()


async def test_broadcast_marks_a_delivered_clip_outstanding(
    server: AudioServer,
) -> None:
    """A clip a browser accepted is protected from the reaper until it is played."""
    server._clients = {FakeSocket()}

    await server.broadcast(_event())

    assert server.outstanding_files() == frozenset({"clip.mp3"})

    # The "done" message is the browser saying it finished, so the protection
    # ends there — the file is deleted anyway, and must not be spared forever.
    server._delete_played_audio("clip.mp3")
    assert server.outstanding_files() == frozenset()


async def test_broadcast_that_reached_nobody_marks_nothing_outstanding(
    server: AudioServer,
) -> None:
    """With no client there is no "done" message to wait for.

    MessageHandler deletes the file itself in this case (broadcast returns 0),
    so sparing it would leave the reaper guarding a name that no longer exists.
    """
    assert await server.broadcast(_event()) == 0
    assert server.outstanding_files() == frozenset()


def test_sweep_leaves_files_that_are_not_mp3s_alone(tmp_path: Path) -> None:
    """Only *.mp3 is swept, so a directory shared with anything else is safe.

    audio_dir holds nothing but generated clips today, but it is a configurable
    path (VOXER_AUDIO_DIR) that somebody could point at a directory of their
    own, and a sweep that deleted whatever it found there would be a nasty
    surprise.
    """
    keep = tmp_path / "notes.txt"
    keep.write_text("not mine to delete")
    # Backdated as far as the clip beside it, so the only thing that can save
    # it is its name.  Left at its real age it would survive on age alone, and
    # this test would keep passing even if the sweep grabbed every file.
    past = time.time() - 600
    os.utime(keep, (past, past))
    _mp3(tmp_path, "clip.mp3", age_secs=600)

    removed = sweep_audio_dir(tmp_path, min_age_secs=300)

    assert removed == 1
    assert keep.exists()


def test_sweep_of_a_directory_that_does_not_exist_removes_nothing(
    tmp_path: Path,
) -> None:
    """A missing directory is reported as an empty sweep, not an error.

    The reaper runs inside the application's task group, where an exception
    would cancel every sibling task and stop the bot.  Nothing should be able
    to reach that from a directory somebody deleted underneath us.
    """
    assert sweep_audio_dir(tmp_path / "gone", min_age_secs=300) == 0


async def test_reaper_keeps_deleting_orphans_and_reports_what_it_took(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The timer loop actually sweeps, and says so when it finds something.

    This is the whole point of the reaper: a browser that crashes or is
    refreshed in the middle of a clip never sends the "done" message that
    deletes it, so those files used to sit in audio_dir until the next restart.
    The task is cancelled at the end, which is how the composition root's task
    group stops it during shutdown.
    """
    orphan = _mp3(tmp_path, "orphan.mp3", age_secs=600)
    caplog.set_level(logging.INFO, logger="voxer.server")

    task = asyncio.create_task(reap_audio(tmp_path, _TEST_REAP_INTERVAL, 300))
    try:
        async with asyncio.timeout(_DELETION_TIMEOUT_SECONDS):
            while orphan.exists():
                await asyncio.sleep(_TEST_REAP_INTERVAL)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert not orphan.exists()
    assert "Reaped 1 orphaned audio file(s)" in caplog.text


async def test_reaper_says_nothing_when_there_is_nothing_to_reap(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A sweep that deletes nothing is silent, which is nearly every sweep.

    The loop runs for as long as the bot does, every few minutes.  Logging each
    pass would fill the log with lines that carry no information and bury the
    ones an operator is actually reading.
    """
    playing_now = _mp3(tmp_path, "playing.mp3")
    caplog.set_level(logging.INFO, logger="voxer.server")

    task = asyncio.create_task(reap_audio(tmp_path, _TEST_REAP_INTERVAL, 300))
    # Long enough for several sweeps to have come and gone.
    await asyncio.sleep(_TEST_REAP_INTERVAL * 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert playing_now.exists()
    assert caplog.records == []


# --------------------------------------------------------------------------
# _QuietServer — keeping uvicorn's hands off the process's signal handlers
# --------------------------------------------------------------------------

# The two signals uvicorn takes over by default, and the two the composition
# root (voxer/app.py) installs its own handlers for: SIGINT is Ctrl-C, SIGTERM
# is what `docker stop` and most process supervisors send.
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


async def _null_app(scope: Any, receive: Any, send: Any) -> None:
    """An ASGI application that does nothing, to satisfy uvicorn.Config.

    uvicorn.Config insists on being given an application, but the tests below
    never start the server — they only call capture_signals() — so the
    application is never invoked.  `log_config=None` stops Config's constructor
    from reconfiguring Python's logging for the rest of the test session.
    """


def _config() -> uvicorn.Config:
    return uvicorn.Config(_null_app, log_config=None)


def test_quiet_server_leaves_the_process_signal_handlers_alone() -> None:
    """_QuietServer.capture_signals() must install nothing and restore nothing.

    This is the seam that lets voxer/app.py own shutdown.  Stock uvicorn
    replaces the SIGINT/SIGTERM handlers for as long as its server runs and
    re-sends the signal it caught afterwards, which used to kill this process
    from inside one task before the others could shut down (see _QuietServer's
    own docstring).  Overriding the method is a small change that is easy to
    lose in a later edit, so this test pins it: the handlers registered with
    the operating system must be exactly the same before, during and after.
    """
    before = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}

    with _QuietServer(_config()).capture_signals():
        during = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}

    after = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}

    assert during == before
    assert after == before


def test_stock_uvicorn_still_installs_its_own_handlers() -> None:
    """The behaviour _QuietServer exists to suppress is still uvicorn's.

    Without this, the test above would keep passing if a future uvicorn
    release stopped capturing signals — and nobody would know that the
    override, and the comments explaining why it is there, had become
    obsolete.  So this asserts the opposite of the test above against the
    unmodified class: inside capture_signals() the handler for each signal is
    uvicorn's, and afterwards the original one is back.
    """
    before = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}
    stock = uvicorn.Server(_config())

    with stock.capture_signals():
        during = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}

    after = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}

    assert during == {sig: stock.handle_exit for sig in _SHUTDOWN_SIGNALS}
    assert after == before


async def test_serve_starts_a_server_that_captures_no_signals(
    server: AudioServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AudioServer.serve() must start the quiet server, not a stock one.

    The test above proves _QuietServer behaves; this one proves it is the class
    actually used, which is the half a careless edit would undo.  uvicorn's own
    `Server.serve` is replaced with a stand-in that records the server object
    and returns immediately, so nothing binds a port and nothing runs forever;
    then that recorded object's capture_signals() is exercised.  Reverting to
    `uvicorn.Server(config)` would make the recorded object install uvicorn's
    handlers here and fail the comparison.
    """
    started: list[uvicorn.Server] = []

    async def _fake_serve(self: uvicorn.Server, sockets: Any = None) -> None:
        started.append(self)

    monkeypatch.setattr(uvicorn.Server, "serve", _fake_serve)

    await server.serve()

    assert len(started) == 1
    before = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}
    with started[0].capture_signals():
        during = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}
    assert during == before
