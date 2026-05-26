# twitch-voxer — Architecture

## Overview

`twitch-voxer` is a self-hosted Twitch chat Text-to-Speech bot.  
It listens to EventSub events from Twitch, synthesises speech for every incoming chat message, and streams the resulting audio to an OBS browser-source overlay via WebSocket.

---

## High-Level Component Map

```mermaid
graph TD
    subgraph Twitch Platform
        TW[Twitch EventSub / Chat]
    end

    subgraph twitch-voxer process
        BOT[VoxBot<br/>bot.py<br/>twitchio AutoBot]
        MQ[(asyncio.Queue<br/>QueuedMessage)]
        MH[MessageHandler<br/>handler.py<br/>lang detect · voice assign · normalise]
        TTS[TTSService<br/>tts.py<br/>Supertonic WAV → ffmpeg MP3]
        SRV[AudioServer<br/>server.py<br/>Starlette HTTP + WebSocket]
        SCH[Scheduler<br/>scheduler.py<br/>rotating chat messages]
        CFG[config.py<br/>env vars]
        LOG[log.py<br/>colorlog]
    end

    subgraph Persistent Storage
        VDB[(voices.json<br/>username → voice)]
        TDB[(timestamps.json<br/>username → last-seen)]
        EDB[(emotes.db<br/>emote name → image URLs)]
        MSGS[(data/messages.json<br/>scheduler texts)]
        AUDIO[(audio/<br/>ephemeral MP3 files)]
    end

    subgraph OBS / Browser
        OBS[Browser Source<br/>static/index.html]
    end

    TW -- EventSub events --> BOT
    BOT -- QueuedMessage --> MQ
    MQ -- dequeued by --> MH
    MH -- synthesise --> TTS
    TTS -- WAV --> TTS
    TTS -- MP3 --> AUDIO
    MH -- BroadcastEvent --> SRV
    SRV -- /audio/*.mp3 --> OBS
    SRV -- WebSocket push --> OBS
    OBS -- done:filename --> SRV
    SRV -- unlink --> AUDIO
    SCH -- send_chat() --> BOT
    BOT -- chat message --> TW
    MH --- VDB
    MH --- TDB
    MH --- EDB
    SCH --- MSGS
    CFG -.-> BOT
    CFG -.-> MH
    CFG -.-> SRV
    CFG -.-> SCH
    LOG -.-> BOT
    LOG -.-> MH
    LOG -.-> SRV
    LOG -.-> SCH
```

---

## Startup / Wiring Sequence

`voxer/__init__.py` is the **composition root** — it instantiates every component and wires their dependencies before handing control to `asyncio.gather`.

```mermaid
sequenceDiagram
    participant main as main.py
    participant init as voxer/__init__.py
    participant tts as TTSService
    participant srv as AudioServer
    participant hdl as MessageHandler
    participant bot as VoxBot
    participant sch as Scheduler

    main->>init: main() → asyncio.run(run())
    init->>tts: TTSService(voices_dir)
    init->>srv: AudioServer(audio_dir, host, port)
    init->>hdl: MessageHandler(tts, db_path, audio_dir, server.broadcast, queue, ...)
    init->>hdl: await handler.preload_resources()   ← loads emotes DB async
    init->>bot: get_user_id(BOT_USERNAME)  ← one-shot Twitch API call
    init->>bot: VoxBot(bot_id, subs, message_queue)
    init->>bot: await bot.add_token(ACCESS_TOKEN, REFRESH_TOKEN)
    init->>sch: Scheduler(bot.send_chat, messages_path, interval)
    init->>init: asyncio.gather(bot.start, server.serve, scheduler.run, handler.process_queue)
    Note over init: All four coroutines run concurrently forever
```

---

## Message Lifecycle — Chat Message → Audio

```mermaid
sequenceDiagram
    participant TW as Twitch EventSub
    participant BOT as VoxBot
    participant MQ as asyncio.Queue
    participant MH as MessageHandler
    participant TTS as TTSService
    participant SRV as AudioServer
    participant OBS as Browser Source

    TW->>BOT: event_message(ChatMessage)
    BOT->>BOT: split fragments → text + emote names
    BOT->>MQ: put(QueuedMessage(username, text, emotes))
    MQ->>MH: process_queue() dequeues
    MH->>MH: _is_bot()  ← skip known bots
    MH->>MH: _extract_emojis()  ← strip emojis, build EmoteItem list
    alt emote-only message
        MH->>MH: copy random emote sound MP3
        MH->>SRV: broadcast(BroadcastEvent)
    else has text
        MH->>MH: _detect_lang()  ← langdetect (in thread)
        MH->>MH: _get_or_assign_voice()  ← pickledb, locked
        MH->>MH: _normalize()  ← expand abbrevs, replace URLs, laugh tags
        MH->>MH: _should_announce()  ← check timestamp window
        MH->>MH: prepend "username says:" if outside window
        MH->>MH: _record_message()  ← save current timestamp
        MH->>TTS: save_wav(text, voice, lang)  ← in thread
        MH->>TTS: to_mp3(wav, mp3)  ← ffmpeg subprocess
        MH->>SRV: broadcast(BroadcastEvent(audio_url, username, emotes))
    end
    SRV->>OBS: WebSocket push {audio_url, username, emotes}
    OBS->>OBS: queue audio, play sequentially
    OBS->>SRV: WS message {done: "filename.mp3"}
    SRV->>SRV: unlink audio file  ← cleanup
```

---

## Channel Events (Follow / Sub / Raid / Cheer)

Channel events bypass the full user pipeline and go straight to TTS with a random voice in Ukrainian.

```mermaid
sequenceDiagram
    participant TW as Twitch EventSub
    participant BOT as VoxBot
    participant EVT as events.py
    participant MQ as asyncio.Queue
    participant MH as MessageHandler
    participant TTS as TTSService
    participant SRV as AudioServer

    TW->>BOT: event_follow / event_subscription / event_cheer / event_raid
    BOT->>EVT: follow_message(username) / sub_message / cheer_message / raid_message
    EVT-->>BOT: random funny announcement string (Ukrainian)
    BOT->>MQ: put(QueuedMessage(kind=SYSTEM, text=announcement))
    MQ->>MH: process_queue() dequeues
    MH->>MH: _handle_system()  ← random voice, no lang detect, no announce window
    MH->>TTS: save_wav + to_mp3
    MH->>SRV: broadcast(BroadcastEvent)
```

---

## Text Normalisation Pipeline (handler.py)

Applied to every user message before synthesis.

```mermaid
flowchart LR
    RAW[raw text] --> EM[strip emojis\n_extract_emojis]
    EM --> LANG[detect language\n_detect_lang\nuk / en]
    LANG --> URL[replace URLs\n_URL_RE\n→ 'see link in chat']
    URL --> ABB[expand abbreviations\n_ABBREV_RE_UK / EN\ne.g. wtf→what the f\nhz→хто зна]
    ABB --> LAUGH[convert laugh tokens\n_LAUGH_RE\n→ TTS <laugh> tag]
    LAUGH --> ANN{announce\nwindow?}
    ANN -- outside window --> PREFIX[prepend\nusername says:]
    ANN -- within window --> FINAL[final text]
    PREFIX --> FINAL
```

---

## Voice Assignment

Each chatter gets a voice on first message and keeps it forever.

```mermaid
flowchart TD
    MSG[incoming message] --> LOOKUP{voices.json\nhas username?}
    LOOKUP -- yes --> USE[use stored voice]
    LOOKUP -- no --> PICK[random.choice\nfrom voice pool]
    PICK --> SAVE[save to voices.json]
    SAVE --> USE
    USE --> TTS[synthesise with chosen voice]

    subgraph Voice Pool
        B[M1 M2 M3 M4 M5\nF1 F2 F3 F4 F5\nbuilt-in Supertonic]
        C[custom *.json\nfrom voices/ dir]
        B --> POOL
        C --> POOL[combined pool]
    end
```

---

## AudioServer — HTTP + WebSocket Endpoints

```mermaid
graph LR
    subgraph Starlette Routes
        R1[GET /] --> IDX[index.html\nOBS overlay]
        R2[GET /simple] --> SIMP[simple.html\nalternate overlay]
        R3[GET /favicon.ico] --> FAV[empty response]
        R4[WS /ws] --> WSH[ws_endpoint\nclient set management\n+ audio file cleanup]
        R5[GET /static/**] --> STDIR[voxer/static/]
        R6[GET /audio/**] --> ADIR[audio/ dir\nephemeral MP3s]
    end
```

---

## Scheduler

Posts rotating messages to Twitch chat on a fixed interval.  
The DB is re-read every cycle so messages can be updated without a restart.

```mermaid
sequenceDiagram
    participant SCH as Scheduler
    participant DB as data/messages.json
    participant BOT as VoxBot.send_chat

    SCH->>SCH: sleep(initial_delay)
    loop every interval seconds
        SCH->>DB: load() + get("messages")
        DB-->>SCH: list[str]
        SCH->>SCH: text = messages[index % len]
        SCH->>SCH: index += 1
        SCH->>BOT: send_chat(text)
        SCH->>SCH: sleep(interval)
    end
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| `asyncio.Queue` between bot and handler | Decouples Twitch event arrival from potentially slow TTS synthesis; prevents event backpressure in the bot. |
| `asyncio.to_thread` for lang detect + WAV synthesis | Both `langdetect` and Supertonic are synchronous CPU-bound calls; offloading them keeps the event loop responsive. |
| `asyncio.Lock` around `_db` and `_ts_db` | pickledb is not async-safe; the lock prevents concurrent load/save races on the same file. |
| Separate `preload_resources()` method on `MessageHandler` | `async def __init__` is not valid Python; `preload_resources()` handles the async emotes-DB load that must happen before messages are processed. |
| Audio file deleted by the browser client | The server cannot know when the browser finishes playing; the client sends a `{done: filename}` WS message after the `<audio>` element fires `ended`, then the server unlinks the file. |
| Path traversal check before unlink | `path.parent == self._audio_dir.resolve()` prevents a malicious WS message from deleting arbitrary files on the server. |
| data/messages.json reloaded every scheduler cycle | Allows live edits without restarting the bot; the DB read is cheap. |
| Longest abbreviation first in regex alternation | Without longest-first ordering, shorter prefixes (`gg`) would match before longer keys (`ggwp`), producing wrong expansions. |
