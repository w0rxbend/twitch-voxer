"""Offline Chromium smoke test; run with uv --with playwright (see README).

Uses the real AudioServer, WebSocket receipts and MP3 playback. Only the publish
fixture and generated silence replace Twitch and model inference.
"""

import asyncio
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Route

from voxer.models import BroadcastEvent, EmoteItem, audio_url_for
from voxer.server import AudioServer


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    artifacts = Path(tempfile.mkdtemp(prefix="voxer-overlay-browser-"))
    with tempfile.TemporaryDirectory(prefix="voxer-browser-audio-") as directory:
        audio_dir = Path(directory)
        sample = audio_dir / "sample.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=22050:cl=mono",
                "-t",
                "4",
                str(sample),
            ],
            check=True,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = AudioServer(audio_dir, "127.0.0.1", port, 1)

            async def publish(request):
                filename = f"{uuid.uuid4()}.mp3"
                await asyncio.to_thread(shutil.copyfile, sample, audio_dir / filename)
                delivered = await server.broadcast(
                    BroadcastEvent(
                        audio_url_for(filename),
                        request.query_params.get("username", "W" * 64),
                        emotes=[
                            EmoteItem(f"emote-{i}", f"/static/speaker.svg?emote={i}")
                            for i in range(8)
                        ],
                    )
                )
                return JSONResponse(
                    {"audio_url": audio_url_for(filename), "delivered": delivered}
                )

            # This route exists only in this ephemeral test fixture.
            server._app.router.routes.append(
                Route("/__fixture__/publish", publish, methods=["POST"])
            )
            http = uvicorn.Server(
                uvicorn.Config(server._app, log_level="error", access_log=False)
            )
            worker = threading.Thread(
                target=http.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            try:
                deadline = time.monotonic() + 10
                while not http.started:
                    if not worker.is_alive() or time.monotonic() > deadline:
                        raise RuntimeError("Offline browser server did not start")
                    time.sleep(0.01)
                origin = f"http://127.0.0.1:{port}"
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            "--autoplay-policy=no-user-gesture-required",
                            "--use-gl=angle",
                            "--use-angle=swiftshader",
                            "--enable-unsafe-swiftshader",
                        ],
                    )
                    try:
                        for route in ("/", "/simple"):
                            for reduced in (False, True):
                                context = browser.new_context(
                                    viewport={"width": 360, "height": 640},
                                    reduced_motion="reduce"
                                    if reduced
                                    else "no-preference",
                                )
                                errors = []
                                external = []
                                page = context.new_page()
                                page.on(
                                    "pageerror", lambda error: errors.append(str(error))
                                )
                                page.on(
                                    "console",
                                    lambda msg: (
                                        errors.append(msg.text)
                                        if msg.type == "error"
                                        else None
                                    ),
                                )
                                page.on(
                                    "request",
                                    lambda req: (
                                        external.append(req.url)
                                        if not req.url.startswith(origin + "/")
                                        else None
                                    ),
                                )
                                page.add_init_script("""(() => {
                                  const play = HTMLMediaElement.prototype.play;
                                  let block = true;
                                  HTMLMediaElement.prototype.play = function () {
                                    if (block) {
                                      block = false;
                                      return Promise.reject(new DOMException('Test autoplay recovery', 'NotAllowedError'));
                                    }
                                    return play.call(this);
                                  };
                                })();""")
                                try:
                                    page.goto(origin + route)
                                    page.wait_for_load_state("networkidle")
                                    expect(
                                        page.locator("#status-pill .txt")
                                    ).to_have_text("connected")
                                    assert page.title().startswith("Voxer")
                                    result = page.request.post(
                                        origin + "/__fixture__/publish"
                                    ).json()
                                    assert result["delivered"] == 1
                                    button = page.get_by_role(
                                        "button", name="Enable Audio"
                                    )
                                    expect(button).to_be_visible()
                                    expect(button).to_be_focused()
                                    # Tab navigation must not accidentally start playback.
                                    page.keyboard.press("Tab")
                                    expect(button).to_be_visible()
                                    button.focus()
                                    page.keyboard.press("Enter")
                                    expect(button).to_be_hidden()
                                    card = page.locator("#np-card")
                                    expect(card).to_have_attribute(
                                        "aria-hidden", "false"
                                    )
                                    expect(page.locator(".np-username")).to_have_text(
                                        "@" + "W" * 64
                                    )
                                    box = card.bounding_box()
                                    assert (
                                        box
                                        and box["x"] >= 0
                                        and box["x"] + box["width"] <= 360
                                    )
                                    assert page.locator(".np-emotes img").count() == 8
                                    if reduced:
                                        assert (
                                            page.locator(
                                                ".fp, .emote-particle, canvas"
                                            ).count()
                                            == 0
                                        )
                                        assert (
                                            page.evaluate(
                                                "document.getAnimations().length"
                                            )
                                            == 0
                                        )
                                    elif route == "/":
                                        expect(page.locator("canvas")).to_have_count(2)
                                    label = f"{'full' if route == '/' else 'simple'}-{'reduced' if reduced else 'motion'}"
                                    page.screenshot(
                                        path=str(artifacts / f"{label}.png")
                                    )
                                    # A changed OS preference stops effects during playback.
                                    page.emulate_media(reduced_motion="reduce")
                                    expect(
                                        page.locator(".fp, .emote-particle")
                                    ).to_have_count(0)
                                    expect(card).to_have_attribute(
                                        "aria-hidden", "true", timeout=10000
                                    )
                                    assert (
                                        page.request.get(
                                            origin + result["audio_url"]
                                        ).status
                                        == 404
                                    )
                                    assert not external, external
                                    assert not errors, errors
                                    print(
                                        f"PASS {label}: keyboard recovery, layout, motion, MP3 and owned ACK"
                                    )
                                except Exception:
                                    print(
                                        {
                                            "route": route,
                                            "reduced": reduced,
                                            "errors": errors,
                                            "motion": page.evaluate(
                                                "matchMedia('(prefers-reduced-motion: reduce)').matches"
                                            ),
                                        }
                                    )
                                    raise
                                finally:
                                    context.close()
                    finally:
                        browser.close()
            finally:
                http.should_exit = True
                worker.join(timeout=10)
                if worker.is_alive():
                    raise RuntimeError("Offline browser server did not stop")
    print(f"Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
