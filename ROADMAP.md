# Feature roadmap

These are proposals, not implemented controls. Exercise the current security and
resource bounds with real Twitch traffic and OBS before expanding the service.

| Priority | Feature | Value and architecture |
| --- | --- | --- |
| 1 | Moderator pause, resume, skip and clear | Stop disruptive speech immediately. Use separate operator authorization and cancellation events acknowledged by all overlays. |
| 1 | Readiness and stream diagnostics | Show Twitch authorization, overlays, synthesis time, queue age and drop reasons using bounded counters, without exposing chat or secrets. |
| 1 | Speech eligibility and moderation | Subscriber/moderator rules, blocked phrases, quiet users and duplicate-text suppression before inference. Keep policy separate from transport. |
| 2 | Channel-point TTS and priority alerts | Redeem points for approved speech. Use separate bounded queues with fair scheduling and reserved alert capacity. |
| 2 | Voice preferences and previews | Let chatters choose permitted voices with cooldowns; give operators a rate-limited preview through the existing synthesis limits. |
| 2 | Isolated inference process | Enforce hard deadlines and recover from native crashes through bounded IPC while retaining the TTS interface. |
| 2 | Emote ID resolution | Carry Twitch emote IDs through the queue and migrate the cache to eliminate display-name collisions. |
| 3 | Audio normalization and ducking | Normalize perceived volume and optionally duck OBS audio; measure encoding cost and latency first. |
| 3 | Multiple channel profiles | Separate credentials, rules, queues and overlay tokens per broadcaster. Design tenancy first; the current service deliberately authorizes one account. |
| 3 | Replay and synthesis cache | Optional short replay buffer and bounded repeated-announcement cache. Include voice/model/normalization version in keys and define retention explicitly. |

Before adding workers or replacing the engine, measure message-to-playback
latency, inference time, memory, backlog age and dropped messages on the OBS
machine. Compare the full and simple overlays and optimize the measured bottleneck.
