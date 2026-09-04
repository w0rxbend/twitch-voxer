"""Pure text-processing helpers for chat messages.

Everything in this module is side-effect-free (no I/O, no state): bot-account
filtering, Unicode-emoji extraction, and the text normalisation pipeline that
turns a raw chat message into something a TTS voice can speak naturally
(URL replacement, abbreviation expansion, laugh tags).

Everything that differs between languages — the spoken stand-in for a URL, the
abbreviation table and its regex, the "username says:" template — lives in one
LanguageRules entry per language in the RULES table, so the set of supported
languages is a single explicit fact (SUPPORTED_LANGS) instead of something
inferred from whichever dictionary a caller happened to look in.

Extracted from handler.py so the rules can be unit-tested without importing
the TTS engine, twitchio, or any other heavyweight dependency.
"""

import logging
import re
from typing import Final, NamedTuple

import emoji as emoji_lib

from .models import EmoteItem

LOGGER: logging.Logger = logging.getLogger(__name__)

# Default language when detection fails or returns an unsupported code.
DEFAULT_LANG: str = "uk"

# ── Bot filtering ─────────────────────────────────────────────────────────────

# Well-known Twitch bot accounts that should never be read aloud.
# Any username that *contains* "bot" (case-insensitive) is also silently skipped.
KNOWN_BOTS: frozenset[str] = frozenset(
    {
        "streamelements",
        "nightbot",
        "moobot",
        "streamlabs",
        "wizebot",
        "fossabot",
        "botisimo",
        "phantombot",
        "cloudbot",
        "sery_bot",
        "soundalerts",
        "dixperstats",
    }
)

# ── Regular expressions ───────────────────────────────────────────────────────

# Matches http:// and https:// URLs so they can be replaced with spoken text.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# The expression tag the TTS engine understands as "laugh here", and the burst of
# three that a laugh token is actually replaced with.  One laugh is too short to
# be audible, so the tag is repeated; naming the burst keeps the replacement text
# and the prepended copy below from drifting apart, which is what a twice-typed
# string literal invites.
_LAUGH_TAG: Final[str] = "<laugh>"
_LAUGH_BURST: Final[str] = ",".join([_LAUGH_TAG] * 3)

# Matches common laugh expressions in English and Ukrainian/Cyrillic.
# Each match is replaced with the TTS-native <laugh> expression tag.
# The tag is also *prepended* to the normalised text so the voice starts
# laughing before it reads the rest of the message (adds comedic timing).
_LAUGH_RE = re.compile(
    r"\b(?:"
    # English laugh variants: lol, lmao, rofl, kek, xD, ww, haha, hehe …
    r"lo+l|lmf?ao|rofl|lel|kek+w?|x+d|w+w+|"
    r"a*ha+ha+(?:ha)*|he+he+(?:he)*|hi+hi+(?:hi)*|"
    # Ukrainian / Cyrillic transliteration: хаха, азаз, хіхі, лол, кек …
    r"а*ха+ха+(?:ха)*|а+хах+|аза+з+|"
    r"хі-хі|хіхі+|ха-ха|хах(?:а+)?|"
    r"кек+|лол+|гаха+|ахах+|їхіхі+"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# ── Abbreviation expansion tables ─────────────────────────────────────────────
# Longer keys must appear first in the alternation so they are tried before their prefixes.
# Without this, "gg" would match inside "ggwp" and leave "wp" un-expanded.
# The ordering is enforced by _build_abbrev_re() which sorts by key length descending.

_ABBREVS_EN: dict[str, str] = {
    "ggwp": "good game well played",
    "glhf": "good luck have fun",
    "omfg": "oh my freaking god",
    "icymi": "in case you missed it",
    "afaik": "as far as I know",
    "fwiw": "for what it's worth",
    "yolo": "you only live once",
    "tldr": "too long didn't read",
    "goat": "greatest of all time",
    "imho": "in my honest opinion",
    "iirc": "if I recall correctly",
    "asap": "as soon as possible",
    "wtf": "what the f",
    "wth": "what the heck",
    "omg": "oh my god",
    "brb": "be right back",
    "afk": "away from keyboard",
    "imo": "in my opinion",
    "fyi": "for your information",
    "tbh": "to be honest",
    "tbf": "to be fair",
    "irl": "in real life",
    "ngl": "not gonna lie",
    "idk": "I don't know",
    "idc": "I don't care",
    "nvm": "never mind",
    "ofc": "of course",
    "smh": "shaking my head",
    "ikr": "I know right",
    "lmk": "let me know",
    "btw": "by the way",
    "ftw": "for the win",
    "jk": "just kidding",
    "rn": "right now",
    "gn": "good night",
    "gm": "good morning",
    "gg": "good game",
    "gj": "good job",
    "gl": "good luck",
    "hf": "have fun",
    "wp": "well played",
    "op": "overpowered",
    "npc": "non-player character",
    "pvp": "player versus player",
    "pve": "player versus environment",
    "fps": "first person shooter",
    "pov": "point of view",
    "eta": "estimated time of arrival",
    "dm": "direct message",
}

_ABBREVS_UK: dict[str, str] = {
    # Latin abbreviations expanded into Ukrainian
    "ggwp": "гарна гра, добре зіграно",
    "glhf": "удачі та гарної гри",
    "omfg": "о боже мій",
    "icymi": "якщо пропустили",
    "afaik": "наскільки я знаю",
    "fwiw": "якщо вам цікаво",
    "yolo": "живемо один раз",
    "tldr": "занадто довго, не читав",
    "goat": "найкращий всіх часів",
    "imho": "на мою скромну думку",
    "iirc": "якщо не помиляюсь",
    "asap": "якнайшвидше",
    "wtf": "що за чорт",
    "wth": "що за таке",
    "omg": "о боже",
    "brb": "зараз повернусь",
    "afk": "від клавіатури",
    "imo": "на мою думку",
    "fyi": "до відома",
    "tbh": "чесно кажучи",
    "tbf": "якщо чесно",
    "irl": "у реальному житті",
    "ngl": "чесно кажучи",
    "idk": "не знаю",
    "idc": "мені все одно",
    "nvm": "нічого",
    "ofc": "звичайно",
    "smh": "хитаю головою",
    "ikr": "та я розумію",
    "lmk": "дай знати",
    "btw": "до речі",
    "ftw": "для перемоги",
    "jk": "жартую",
    "rn": "прямо зараз",
    "gn": "на добраніч",
    "gm": "доброго ранку",
    "gg": "гарна гра",
    "gj": "гарна робота",
    "gl": "удачі",
    "hf": "гарної гри",
    "wp": "добре зіграно",
    "op": "перекачаний",
    "npc": "не ігровий персонаж",
    "pvp": "гравець проти гравця",
    "pve": "гравець проти середовища",
    "fps": "шутер від першої особи",
    "pov": "точка зору",
    "eta": "приблизний час прибуття",
    "dm": "особисте повідомлення",
    # Native Cyrillic abbreviations
    "імхо": "на мою скромну думку",
    "афк": "від клавіатури",
    "гг": "гарна гра",
    "нз": "не знаю",
    "хз": "хто зна",
    "дк": "до речі",
}


def _build_abbrev_re(abbrevs: dict[str, str]) -> re.Pattern:
    """Compile a word-boundary regex that matches all keys in the abbreviation dict.

    Keys are sorted longest-first so the alternation tries longer patterns before
    their shorter prefixes (e.g. "ggwp" before "gg").  Without this ordering,
    the engine would greedily match the shorter key, leaving the suffix un-expanded.
    """
    keys = sorted(abbrevs, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b",
        re.IGNORECASE | re.UNICODE,
    )


# ── Per-language rules ────────────────────────────────────────────────────────


class LanguageRules(NamedTuple):
    """Every language-dependent value the message pipeline needs, in one place.

    Grouping these means adding a third language is a single new entry in RULES
    rather than a new key in each of several parallel dictionaries.  It also
    makes a partially-supported language impossible: previously the link phrase
    was looked up with a graceful `.get(lang, default)` while the abbreviation
    table was chosen with `if lang == "uk"`, so an unexpected code such as "de"
    would have been given Ukrainian link phrasing together with English
    abbreviations.  One lookup cannot produce that mixture.

    Attributes:
        link_replacement: Spoken phrase substituted for any URL in the message.
        abbrevs: Lower-cased abbreviation → expansion lookup for this language.
        abbrev_re: Pattern matching exactly the keys of `abbrevs`, longest first.
        announcement: "username says:" template, formatted with {username}/{text}.
    """

    link_replacement: str
    abbrevs: dict[str, str]
    abbrev_re: re.Pattern
    announcement: str


# The regexes are compiled once here, at module load time, rather than on every
# message.  RULES is the single source of truth for "which languages does this
# bot support?" — see SUPPORTED_LANGS below, which handler.py consults when
# language detection returns something unexpected.
RULES: dict[str, LanguageRules] = {
    "en": LanguageRules(
        link_replacement="... see link in the chat ...",
        abbrevs=_ABBREVS_EN,
        abbrev_re=_build_abbrev_re(_ABBREVS_EN),
        announcement="The user {username} says: {text}",
    ),
    "uk": LanguageRules(
        link_replacement="... дивіться посилання в чаті ...",
        abbrevs=_ABBREVS_UK,
        abbrev_re=_build_abbrev_re(_ABBREVS_UK),
        announcement="Користувач {username} каже: {text}",
    ),
}

# The set of language codes this module can speak, derived from the table rather
# than typed out a second time.
SUPPORTED_LANGS: frozenset[str] = frozenset(RULES)


def rules_for(lang: str) -> LanguageRules:
    """Return the rules for a language code, falling back to DEFAULT_LANG.

    The fallback happens exactly once, for the whole set of rules at a time, so
    an unsupported code always gets one language's rules in full and never a
    mixture of two.
    """
    return RULES.get(lang, RULES[DEFAULT_LANG])


# ── Twemoji URL helpers ───────────────────────────────────────────────────────

# Base URL for Twemoji PNG assets (72×72 px).  Used to build image URLs for
# Unicode emoji so the browser overlay can display them alongside Twitch emotes.
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"

# Two Unicode characters that render as nothing at all.  Written as escapes and
# given names because a literal U+200D is invisible in a diff, in a code review
# and in most editors — and is silently deleted by any tool that strips
# zero-width characters, which would break emoji URLs with no visible cause.
#
# Zero-Width Joiner: glues several emoji into one compound emoji, e.g. man +
# ZWJ + woman + ZWJ + girl renders as a single family emoji.
_ZWJ: Final[str] = "\u200d"
# Variation Selector-16: asks for the colourful emoji rendering of a character
# that also has a plain-text form, e.g. U+2764 ("❤") + VS16 gives the red heart.
_VARIATION_SELECTOR_16: Final[int] = 0xFE0F


def emoji_url(char: str) -> str:
    """Return the Twemoji PNG URL for a single emoji character.

    Twemoji's filename convention: Variation Selector-16 (U+FE0F) is omitted
    from simple emoji ("❤️" → "2764.png"), but KEPT inside Zero-Width-Joiner
    sequences ("❤️‍🔥" → "2764-fe0f-200d-1f525.png") — matching how the assets
    are actually named in the Twemoji repository.
    """
    has_zwj = _ZWJ in char
    codepoints = "-".join(
        f"{ord(c):x}" for c in char if has_zwj or ord(c) != _VARIATION_SELECTOR_16
    )
    return f"{_TWEMOJI_BASE}/{codepoints}.png"


def extract_emojis(text: str) -> tuple[str, list[EmoteItem]]:
    """Strip Unicode emoji from text and return (clean_text, emoji_items).

    The emoji library identifies all emoji positions, builds EmoteItems with
    Twemoji image URLs, then returns a cleaned string with emoji removed.
    The items are later merged with Twitch emote items to form the overlay list.
    """
    found = emoji_lib.emoji_list(text)
    items = [EmoteItem(name=e["emoji"], url=emoji_url(e["emoji"])) for e in found]
    clean = emoji_lib.replace_emoji(text, replace="").strip()
    return clean, items


def is_bot(username: str) -> bool:
    """Return True if the username belongs to a known bot or contains 'bot'."""
    lower = username.lower()
    return lower in KNOWN_BOTS or "bot" in lower


def normalize(text: str, lang: str) -> str:
    """Apply text transformations to make a chat message more speakable.

    Transformations (applied in order):
      1. Replace URLs with a language-appropriate spoken phrase.
      2. Expand abbreviations using the language-specific lookup table.
      3. Replace laugh tokens with the TTS <laugh> tag and also prepend it
         so the voice opens with laughter before reading the rest.

    Every language-dependent value comes from one LanguageRules entry, so an
    unrecognised code falls back to DEFAULT_LANG for all of them together.

    Args:
        text: Cleaned message text (emoji already stripped).
        lang: Detected language code; anything outside SUPPORTED_LANGS is
            treated as DEFAULT_LANG.

    Returns:
        Normalised text ready for TTS synthesis.
    """
    rules = rules_for(lang)
    text, link_count = _URL_RE.subn(rules.link_replacement, text)

    # subn with a lambda performs a case-insensitive lookup in the dict
    text, abbrev_count = rules.abbrev_re.subn(
        lambda m: rules.abbrevs[m.group(0).lower()], text
    )

    text, laugh_count = _LAUGH_RE.subn(_LAUGH_BURST, text)
    if link_count:
        LOGGER.debug("Replaced %d link(s)", link_count)
    if abbrev_count:
        LOGGER.debug("Expanded %d abbreviation(s)", abbrev_count)
    if laugh_count:
        LOGGER.debug("Applied %d <laugh> tag(s): %s", laugh_count, text)
        # Prepend laugh so the voice starts laughing *before* reading the message
        text = _LAUGH_BURST + text
    return text
