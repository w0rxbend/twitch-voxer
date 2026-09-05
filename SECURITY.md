# Security and deployment

## Local OBS and overlay credentials

The default bind is `127.0.0.1:8080`. Exact Host checks and same-origin browser
WebSocket checks apply even locally. A separate overlay token is optional for a
local listener and required for any non-loopback listener, including Docker's
internal bind.

Generate a token:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set the result as `VOXER_OVERLAY_TOKEN` in `.env`. It must contain 32-256 URL-safe
letters, digits, underscores or hyphens. Use
`http://localhost:8080/?token=YOUR_TOKEN` or
`http://localhost:8080/simple?token=YOUR_TOKEN` as the OBS source URL. The server
exchanges it for an HttpOnly, SameSite=Strict cookie and redirects without the
query token. Keep the token in the configured OBS URL for fresh browser profiles.
Never reuse Twitch credentials for this purpose.

Changing the token invalidates existing cookies; reload each overlay with the new
token. Keep browser history and OBS configuration private. Audio-server access
logs are disabled, and responses use no-referrer and no-store headers. A reverse
proxy must also redact or omit bootstrap query strings from its logs.

## Docker

Set the three Twitch settings and `VOXER_OVERLAY_TOKEN` before
`docker compose up --build`. Both 8080 and OAuth port 4343 are published on host
loopback only. The process runs as an unprivileged user, with a read-only root
filesystem, dropped capabilities and no privilege escalation. Data, model-cache
and temporary directories remain writable.

`/healthz` is a minimal unauthenticated liveness probe. It does not establish
Twitch readiness, OBS connectivity or successful inference.

## Network access and TLS reverse proxies

Keep direct backend ports private and use HTTPS/WSS on untrusted networks.
Set a strong overlay token and add the exact address used by OBS to
`VOXER_ALLOWED_HOSTS`. Entries have no scheme, path, port or wildcard.

Forwarded headers are disabled by default. For TLS termination, configure
`VOXER_TRUSTED_PROXIES` with the proxy's exact IP or a narrow CIDR. The proxy must
overwrite `X-Forwarded-For` and `X-Forwarded-Proto`, preserve Host, and support
WebSocket upgrades. Do not trust every address.

Example for a reverse proxy on the same host:

```dotenv
VOXER_SERVER_HOST=127.0.0.1
VOXER_OVERLAY_TOKEN=REPLACE_WITH_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS
VOXER_ALLOWED_HOSTS=tts.example.org
VOXER_TRUSTED_PROXIES=127.0.0.1,::1
```

Container proxies need their actual network address. To intentionally publish a
LAN port, change the Compose host bind and allow the LAN address. Keep OAuth port
4343 private unless its reverse proxy and registered redirect are configured.

## OAuth and credentials

Only the configured bot account may authorize this single-channel service.
The adapter fixes the registered redirect URI and requested scopes. Callback
state is short-lived, single-use and browser-bound. A local HTTP callback port
must match `VOXER_OAUTH_PORT`.

Tokens use unique private temporary files, fsync and atomic replacement. The
final file is mode 0600 on POSIX. Protect and back up the data directory. Never
paste tokens into reports or logs.

The bot owns refresh while running. `voxer-fetch-emotes` can reuse a valid scoped
token, but the bot must stop if the downloader needs to rotate it. By default the
downloader scans the configured channel and followed channels; explicitly pass
`--include-followers` for the larger follower scan.

## Resource limits

| Setting | Default | Purpose |
| --- | --- | --- |
| `VOXER_MESSAGE_QUEUE_MAXSIZE` | 20 | Waiting messages |
| `VOXER_MAX_MESSAGE_AGE_SECS` | 60 | Stale-work cutoff |
| `VOXER_USER_COOLDOWN_SECS` | 2 | Per-chatter admission interval; 0 disables it |
| `VOXER_MAX_MESSAGE_CHARS` | 500 | Incoming text limit |
| `VOXER_MAX_SPEECH_CHARS` | 1000 | Expanded speech limit; maximum 2000 |
| `VOXER_MAX_WS_CLIENTS` | 8 | Concurrent overlays |
| `VOXER_MAX_PENDING_PER_CLIENT` | 64 | Outstanding clips per overlay |
| `VOXER_AUDIO_MAX_AGE_SECS` | 300 | Retained clip age |
| `VOXER_AUDIO_SWEEP_INTERVAL_SECS` | 300 | Disk cleanup cadence |
| `VOXER_WS_SEND_TIMEOUT` | 5 | Slow-client send deadline |

No connected overlay means no synthesis. Overflow, stale and excessive chat may
be dropped deliberately. Native inference can finish after cancellation because
it runs in a thread; its files are cleaned afterward. Hard CPU deadlines require
a separate inference process. Performance claims need measurements on the actual
streaming machine.

## Maintenance

```sh
uv sync --locked --dev
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen pyright
uv run --frozen pytest -q
node tests/test_overlay.cjs
```

Commit the lockfile and audit dependencies when updating it. Vendored browser
libraries retain versions and licenses under `voxer/static/vendor/`; review
upstream advisories when upgrading them.

References: [Twitch OAuth](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/),
[Twitch refresh ownership](https://dev.twitch.tv/docs/authentication/refresh-tokens/),
and [uv locking](https://docs.astral.sh/uv/concepts/projects/sync/).
