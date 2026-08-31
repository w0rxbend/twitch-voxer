"""Unit tests for the pure text-processing rules in voxer.textnorm."""

from voxer.textnorm import extract_emojis, is_bot, normalize


class TestIsBot:
    def test_known_bot(self) -> None:
        assert is_bot("Nightbot")
        assert is_bot("streamelements")

    def test_contains_bot(self) -> None:
        assert is_bot("MyCoolBot")
        assert is_bot("robotic_voice")

    def test_regular_user(self) -> None:
        assert not is_bot("worxbend")
        assert not is_bot("viewer123")


class TestNormalize:
    def test_url_replaced_en(self) -> None:
        out = normalize("check https://example.com now", "en")
        assert "https://" not in out
        assert "see link in the chat" in out

    def test_url_replaced_uk(self) -> None:
        out = normalize("дивись https://example.com", "uk")
        assert "https://" not in out
        assert "посилання" in out

    def test_longest_abbreviation_wins(self) -> None:
        # "ggwp" must expand as one unit, not as "gg" + "wp" leftovers
        assert normalize("ggwp", "en") == "good game well played"

    def test_short_abbreviation(self) -> None:
        assert normalize("gg", "en") == "good game"

    def test_abbreviation_case_insensitive(self) -> None:
        assert normalize("GG", "en") == "good game"

    def test_cyrillic_abbreviation(self) -> None:
        assert normalize("хз", "uk") == "хто зна"

    def test_laugh_tag_prepended(self) -> None:
        out = normalize("lol that was great", "en")
        assert out.startswith("<laugh>")
        assert "lol" not in out

    def test_plain_text_unchanged(self) -> None:
        assert normalize("hello there friend", "en") == "hello there friend"


class TestExtractEmojis:
    def test_no_emoji(self) -> None:
        clean, items = extract_emojis("hello world")
        assert clean == "hello world"
        assert items == []

    def test_emoji_stripped_and_collected(self) -> None:
        clean, items = extract_emojis("nice 🔥 play")
        assert "🔥" not in clean
        assert len(items) == 1
        assert items[0].name == "🔥"
        assert items[0].url.endswith(".png")

    def test_variation_selector_dropped_from_url(self) -> None:
        # "❤️" is U+2764 + U+FE0F; Twemoji filenames omit the FE0F selector
        _, items = extract_emojis("❤️")
        assert items[0].url.endswith("/2764.png")

    def test_zwj_sequence_kept_whole(self) -> None:
        # Family emoji is one ZWJ sequence — one item, all codepoints in the URL
        _, items = extract_emojis("👨‍👩‍👧")
        assert len(items) == 1
        assert items[0].url.endswith("/1f468-200d-1f469-200d-1f467.png")

    def test_zwj_sequence_keeps_variation_selector(self) -> None:
        # Twemoji keeps U+FE0F inside ZWJ sequences (unlike simple emoji)
        _, items = extract_emojis("❤️‍🔥")
        assert items[0].url.endswith("/2764-fe0f-200d-1f525.png")

    def test_emoji_only_message(self) -> None:
        clean, items = extract_emojis("😀😀")
        assert clean == ""
        assert len(items) == 2
