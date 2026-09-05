# Project guidance

This is a Python 3.14, uv-managed Twitch TTS service using TwitchIO, Starlette,
Uvicorn, Supertonic and plain JavaScript OBS overlays. Read ARCHITECTURE.md for
component boundaries and SECURITY.md before changing authentication or deployment.

## Engineering rules

- Keep app.py as the composition root; inject network and storage dependencies.
- Keep normalization and domain values free of I/O. Avoid speculative abstractions.
- Bound queues, caches, payloads, retries and subprocess output. Preserve cancellation.
- Authenticate before work; validate Twitch application, account and scopes together.
- Browser acknowledgements must only release audio owned by that connection.
- Keep Twitch credentials on the backend and redact secrets from failure output.
- Preserve JSON compatibility; use private atomic writes and a single refresh owner.
- Keep both overlays on the shared playback runtime and serve executable assets locally.
- Add focused regressions for security boundaries, concurrency and failure recovery.
- Do not exercise live Twitch grants or send chat messages as part of offline tests.

## Checks

```sh
uv sync --locked --dev
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen pyright
uv run --frozen pytest -q
node tests/test_overlay.cjs
```

The installed async-python-patterns, python-performance-optimization,
python-testing-patterns, python-design-patterns, architecture-patterns,
modern-javascript-patterns, security-best-practices, web-design-guidelines and
webapp-testing skills are relevant when their subject matches the change.
Use the actual Starlette/plain-JavaScript stack; do not introduce a frontend
framework or migrate the HTTP framework just to match a skill.
