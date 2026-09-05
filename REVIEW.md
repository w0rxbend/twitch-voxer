# Security and performance review

Reviewed and remediated on 2026-09-05. Scope: Twitch API/OAuth, emote metadata
fetcher, TTS processing, persistence, HTTP/WebSocket server, both OBS overlays,
deployment, dependencies and regression tests.

## Findings addressed

| Priority | Finding | Result |
| --- | --- | --- |
| High | Unauthenticated network audio/WS access and cross-origin connections | Localhost defaults, separate overlay credential, exact Host and Origin policy, explicit proxy trust |
| High | Client could acknowledge arbitrary files or delete another overlay's queued clip | Typed MP3 names and per-recipient ownership; disconnect/expiry cleanup |
| High | OAuth callback lacked effective browser-bound state validation | Fixed redirect/scopes, expiring single-use state cookie, account/application/scope validation |
| High | Competing token writers, unsafe permissions and refresh-token disclosure on failure | Private atomic files, refresh ownership, POST-body secrets and redacted failures |
| High | Vulnerable installed HTTP dependencies | aiohttp upgraded to 3.14.3; Starlette upgraded to 1.6.0; patched lower bounds and updated lockfile |
| Medium | Unbounded pending playback, cache growth and expensive work before admission | Queue/client/cache/text limits, per-user cooldowns, deduplication and early rejection |
| Medium | Slow clients blocked fanout and stale/offline work consumed inference | Concurrent bounded sends, queue age limits and no synthesis without an overlay |
| Medium | Cancellation left inference outputs or ffmpeg work behind | Serialized model access, eventual file cleanup, bounded encoder duration/output and process reaping |
| Medium | Repeated whole-state timestamp writes and transient persistence loss | Batched validated timestamps, atomic snapshots, dirty-write retries and shutdown flushes |
| Medium | NaN timestamps poisoned all future writes; tiny rates produced infinite sleeps | Validation of all loaded timestamp entries and finite scheduling intervals |
| Medium | Rejected seed grants could replace an existing working app/user token | Serialized admission and restoration of previous SDK token state on rejection |
| Medium | Downloader ignored transient failures, identity and rate/size limits | Correct account binding, bounded requests/pagination/retries and explicit refresh ownership |
| Medium | Stale emotes survived refresh and source precedence varied | Complete atomic cache replacement and deterministic source ordering |
| Medium | Browser executed remote CDN scripts and retained stale media work | Pinned local assets/licenses, content policy and bounded playback/reconnect lifecycle |
| Medium | Container exposed both ports on every interface | Loopback publishing, reduced container privileges, read-only root, dedicated liveness probe |
| Low | Configuration accepted unusable redirect or speech limits | Redirect port validation, retained public ports, engine-aligned speech bounds |

The implementation retains the existing framework and extracts OAuth/token
persistence into focused modules. Runtime PickleDB was removed while preserving
the existing JSON state format. See ARCHITECTURE.md for the final responsibilities.

## Verification

- Full Python suite: 365 tests passed; only upstream deprecation warnings.
- Browser runtime: 7 Node tests passed.
- Ruff lint and formatting checks passed; Pyright reported zero errors/warnings.
- pip-audit reported no known vulnerabilities for installed public Python packages.
- The local application package is not published on PyPI and was reviewed as source.
- Wheel build succeeded; packaged files include OAuth/token modules, overlay runtime,
  graphics libraries and their licenses.
- Independent native agents cross-reviewed the server, backend and authentication
  changes. Their additional findings were fixed with regressions.
- Ultracode's initial authentication finder completed. Its subprocess verifier
  timed out without a result; a native agent replaced that verification stage.

This is not a live Twitch/OBS or Docker runtime sign-off. Real grants, native
synthesis quality, rendered WebGL/GPU load and a deployed TLS proxy were not
exercised. Native inference remains an uninterruptible thread, timestamp batching
can repeat an announcement after an abrupt crash, and the emote cache still uses
display names. These limits and future improvements are documented in ROADMAP.md.

## Installed agent skills

Installed into the user's Codex skills directory and available on the next turn.
Popularity is approximate, as reported by skills.sh at selection time. Source
instructions were inspected; downloaded skill scripts were not executed.

| Skill | Source | Approximate installs |
| --- | --- | --- |
| async-python-patterns | [wshobson/agents](https://skills.sh/wshobson/agents/async-python-patterns) | 15.6K |
| python-performance-optimization | [wshobson/agents](https://skills.sh/wshobson/agents/python-performance-optimization) | 32.4K |
| python-testing-patterns | [wshobson/agents](https://skills.sh/wshobson/agents/python-testing-patterns) | 31.5K |
| python-design-patterns | [wshobson/agents](https://skills.sh/wshobson/agents/python-design-patterns) | 19.7K |
| architecture-patterns | [wshobson/agents](https://skills.sh/wshobson/agents/architecture-patterns) | 21.6K |
| modern-javascript-patterns | [wshobson/agents](https://skills.sh/wshobson/agents/modern-javascript-patterns) | 18.5K |
| security-best-practices | [OpenAI](https://skills.sh/openai/skills/security-best-practices) | 7.9K |
| web-design-guidelines | [Vercel](https://skills.sh/vercel-labs/agent-skills/web-design-guidelines) | 608.6K |
| webapp-testing | [Anthropic](https://skills.sh/anthropics/skills/webapp-testing) | 149.5K |
