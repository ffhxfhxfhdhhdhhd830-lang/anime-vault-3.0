from anime_bot.extra_commands import FONT_STYLES, _font_text


def test_font_converter_preserves_spaces_and_punctuation():
    assert _font_text("Hello, darling!", "bold") == "𝐇𝐞𝐥𝐥𝐨, 𝐝𝐚𝐫𝐥𝐢𝐧𝐠!"


def test_font_converter_small_caps():
    assert _font_text("Hello World", "smallcaps") == "ʜᴇʟʟᴏ ᴡᴏʀʟᴅ"


def test_font_style_menu_has_multiple_styles():
    assert {"bold", "script", "double", "smallcaps"}.issubset(FONT_STYLES)
