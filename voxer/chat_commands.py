"""Split inline audio commands into ordered sections of a Twitch message."""

import re
from dataclasses import dataclass

# Commands start after whitespace (or at message start) and end at a separator.
# The lookarounds reject embedded words (!soundtrack, email!tts) and escaped
# markers (\!tts). Scan once, then slice sections so speech keeps its case,
# punctuation and original fragment offsets. No greedy "capture everything"
# regex can swallow the next command.
_COMMAND = re.compile(
    r"""
    (?<!\S) ! [^\S\r\n]* (?P<command>tts|sound|end|s) (?=\s|[:=]|\Z)
    (?: \s* [:=] \s* | \s+ )?
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class ChatCommand:
    name: str
    argument: str
    start: int  # inclusive argument offset in the original Twitch fragments
    end: int  # exclusive argument offset


def parse_commands(text: str) -> list[ChatCommand]:
    """Return commands in order, including !end markers that terminate sections.

    Speech and sound arguments end at the next supported command or message
    end. !end explicitly terminates a section; its following prose is ignored.
    A caller can distinguish ordinary chat (no markers) from an empty command.
    """
    markers = list(_COMMAND.finditer(text))
    commands = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        commands.append(
            ChatCommand(
                name=marker["command"].casefold(),
                argument=text[marker.end() : end].strip(),
                start=marker.end(),
                end=end,
            )
        )
    return commands
