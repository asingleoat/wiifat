import colorsys
import re

from wiifat.colors import (
    UNASSIGNED_COLOR,
    WHITE,
    color_from_key,
    text_color,
    user_color,
)


def test_user_colors_are_deterministic_snapshots():
    assert user_color("Alice") == user_color("Alice")
    assert user_color("Alice") == "#81b632"
    assert user_color("Mohammed") == "#40aa4b"
    assert user_color("Jos\u00e9") == user_color("Jose\u0301")


def test_user_color_format_and_foreground():
    assert re.fullmatch(r"#[0-9a-f]{6}", user_color("Taylor"))
    assert re.fullmatch(r"#[0-9a-f]{6}", UNASSIGNED_COLOR)
    assert text_color("Taylor") == WHITE == "#ffffff"


def test_user_colors_stay_inside_hsl_bands():
    for index in range(48):
        color = user_color(f"Person {index}")
        red, green, blue = (
            int(color[offset : offset + 2], 16) / 255
            for offset in (1, 3, 5)
        )
        _hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
        assert 0.45 <= saturation <= 0.65
        assert 0.42 <= lightness <= 0.55


def test_common_names_are_usually_distinct():
    names = [
        "Olivia",
        "Liam",
        "Emma",
        "Noah",
        "Amelia",
        "Oliver",
        "Sophia",
        "Elijah",
        "Mia",
        "Mateo",
    ]
    assert len({user_color(name) for name in names}) >= 8


def test_timestamp_key_colors_stay_inside_hsl_bands():
    for index in range(48):
        key = str(1_700_000_000.0 + index * 0.1234567)
        color = color_from_key(key)
        red, green, blue = (
            int(color[offset : offset + 2], 16) / 255
            for offset in (1, 3, 5)
        )
        _hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
        assert 0.45 <= saturation <= 0.65
        assert 0.42 <= lightness <= 0.55
