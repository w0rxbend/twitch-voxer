import json
import logging
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

LOGGER: logging.Logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


class AudioServer:
    def __init__(self, audio_dir: Path, host: str, port: int) -> None:
        self._audio_dir = audio_dir
        self._host = host
        self._port = port
        self._clients: set[WebSocket] = set()
        self._app = self._build_app()

    def _build_app(self) -> Starlette:
        async def index(request):
            return FileResponse(_STATIC_DIR / "index.html")

        async def favicon(request):
            return Response(content=b"", media_type="image/x-icon")

        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            self._clients.add(websocket)
            LOGGER.info("WebSocket client connected — %d client(s) active", len(self._clients))
            try:
                while True:
                    data = await websocket.receive_text()
                    try:
                        message = json.loads(data)
                    except json.JSONDecodeError:
                        LOGGER.warning("Received malformed WS message: %r", data)
                        continue
                    if filename := message.get("done"):
                        path = (self._audio_dir / filename).resolve()
                        if path.parent == self._audio_dir.resolve():
                            path.unlink(missing_ok=True)
                            LOGGER.debug("Cleaned up audio file: %s", filename)
                        else:
                            LOGGER.warning("Rejected suspicious filename: %r", filename)
            except WebSocketDisconnect:
                LOGGER.info("WebSocket client disconnected — %d client(s) remaining", len(self._clients) - 1)
            finally:
                self._clients.discard(websocket)

        return Starlette(routes=[
            Route("/", index),
            Route("/favicon.ico", favicon),
            WebSocketRoute("/ws", ws_endpoint),
            Mount("/audio", StaticFiles(directory=self._audio_dir)),
        ])

    async def broadcast(self, url: str, username: str) -> None:
        if not self._clients:
            LOGGER.debug("No WS clients connected, skipping broadcast")
            return
        LOGGER.info("Broadcasting to %d client(s): %s", len(self._clients), url)
        message = json.dumps({"url": url, "username": username})
        dead: set[WebSocket] = set()
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        if dead:
            LOGGER.warning("Dropped %d stale client(s)", len(dead))
        self._clients -= dead

    async def serve(self) -> None:
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()
