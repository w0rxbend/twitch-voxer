# Predefined soundboard clips

The catalogue contains 49 clips: 17 original effects and 32 user-added clips.
Use `!s <name>` or `!sound <name>`; see the command and alias table in the
[project README](../../README.md).

## Original effects

These 17 effects were downloaded from [BigSoundBank](https://bigsoundbank.com/)
on September 5, 2026. The source pages identify these recordings as **CC0 (public domain)**.
See the [source license statement](https://bigsoundbank.com/licenses.html) and
[CC0 1.0 dedication](https://creativecommons.org/publicdomain/zero/1.0/).

These are recordings and effects selected for the requested sound names;
`doki doki` is a heartbeat, `wan wan` is a dog bark, and `noted` is a notification.
They are available locally without runtime downloads or third-party audio services.

| Local file | Original recording | Creator |
| :--- | :--- | :--- |
| `pop.mp3` | [Bubbles Burst](https://bigsoundbank.com/bubbles-burst-s1074.html) | Joseph Sardin |
| `zap.mp3` | [Star Wars Blaster #2](https://bigsoundbank.com/star-wars-blaster-2-s1758.html) | Joseph Sardin |
| `sparkle.mp3` | [Chimes "Dream" #8](https://bigsoundbank.com/chimes-dream-8-s2086.html) | Joseph Sardin |
| `ding.mp3` | [Microwave Bell](https://bigsoundbank.com/microwave-bell-s1631.html) | Joseph Sardin |
| `crunch.mp3` | [Apple crunched #2](https://bigsoundbank.com/apple-crunched-2-s1114.html) | Joseph Sardin |
| `chirp.mp3` | [Chick 2 (Stuffings)](https://bigsoundbank.com/chick-2-stuffings-s0431.html) | Joseph Sardin |
| `choo-choo.mp3` | [Train Whistle, Foley #7](https://bigsoundbank.com/train-whistle-foley-7-s3321.html) | Joseph Sardin |
| `splash.mp3` | [Splash, Small #1](https://bigsoundbank.com/splash-small-1-s1529.html) | Joseph Sardin |
| `tweet.mp3` | [Birds Waking #3](https://bigsoundbank.com/wake-birds-3-s0999.html) | Joseph Sardin |
| `boing.mp3` | [Boing Cartoon #2](https://bigsoundbank.com/boing-cartoon-2-s2278.html) | Joseph Sardin |
| `hush.mp3` | [Hush, long man](https://bigsoundbank.com/hush-long-man-s1076.html) | Joseph Sardin |
| `ribbit.mp3` | [One frog](https://bigsoundbank.com/one-frog-s0819.html) | Joseph Sardin |
| `doki-doki.mp3` | [Heart Beat](https://bigsoundbank.com/heart-beat-s0218.html) | Joseph Sardin |
| `wan-wan.mp3` | [Barking Dog #2](https://bigsoundbank.com/barking-dog-2-s2954.html) | Joseph Sardin |
| `noted.mp3` | [Message #1](https://bigsoundbank.com/message-1-s1111.html) | Joseph Sardin |
| `bang.mp3` | [Rifle: Shot #2](https://bigsoundbank.com/rifle-shot-2-s2854.html) | Joseph Sardin |
| `beep.mp3` | [Answering Machine Beep](https://bigsoundbank.com/answering-machine-beep-s1616.html) | Joseph Sardin |

These 17 excerpts have leading silence removed, are capped at three seconds,
and are encoded as mono 44.1 kHz, 128 kbps MP3. Peaks were adjusted to -6 dB
with short fades. These edits do not change the CC0 dedication.

## User-added clips

These files were supplied locally and renamed where needed. Their audio contents
and durations are unchanged. Two stray non-audio bytes (`{}`) were removed from
the end of `rizz.mp3` to fix a decoder error; its audio frames and decoded samples
are unchanged. Original source URLs and licenses were not supplied;
the CC0 attribution above applies only to the original 17 effects.

Each command name below is also its filename (for example, `!s wow` plays `wow.mp3`).

| Command name | Original filename |
| :--- | :--- |
| `wow` | `anime-wow.mp3` |
| `gong` | `asian-gong-music.mp3` |
| `aww` | `aww.mp3` |
| `bruh` | `bruh.mp3` |
| `buzzer` | `buzzer.mp3` |
| `chime` | `ding-sound-effect.mp3` |
| `call` | `discord-call.mp3` |
| `leave` | `discord-leave-noise.mp3` |
| `discord` | `discord-notification.mp3` |
| `join` | `discordjoin.mp3` |
| `wrong` | `extremely-loud-incorrect-buzzer.mp3` |
| `fart` | `fart-button.mp3` |
| `gunshot` | `gunshottttt.mp3` |
| `iphone` | `iphone-notification.mp3` |
| `meow2` | `m-e-o-w.mp3` |
| `quack` | `mac-quack.mp3` |
| `meow` | `meow-1.mp3` |
| `evil` | `muhehehe.mp3` |
| `nana` | `na-na-na.mp3` |
| `nope` | `nope.mp3` |
| `hellnah` | `oh-my-god-bro-oh-hell-nah-man.mp3` |
| `omg` | `oh-my-god-meme.mp3` |
| `ohno` | `oh-no-no-no-laugh.mp3` |
| `punch` | `punch-sound.mp3` |
| `rizz` | `rizz-sound-effect.mp3` |
| `shocked` | `shocked-sound.mp3` |
| `thunder` | `thunder.mp3` |
| `wait` | `wait-wait-wait-what-the-hell-legend-sound.mp3` |
| `champions` | `we-are-the-champions.mp3` |
| `wetfart` | `wet-fart.mp3` |
| `whip` | `whip.mp3` |
| `womp` | `womp-womp-womp.mp3` |

[sources.json](sources.json) records available source information, original filenames,
modifications, and SHA-256 hashes. User-added clips have unknown source URLs and
licenses recorded as `null`.

## Replacing clips

To replace an effect, set `VOXER_SOUNDS_DIR` to a directory containing MP3s with
the filenames above and restart Voxer. The names and aliases in `voxer/soundboard.py`
remain the same. The default assets are packaged with the application and Docker image.
