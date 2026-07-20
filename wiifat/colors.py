"""Deterministic, readable colors for named users."""

from __future__ import annotations

import colorsys
import hashlib
import unicodedata


WHITE = "#ffffff"
UNASSIGNED_COLOR = "#8a8f98"
_SATURATION_RANGE = (0.45, 0.65)
_LIGHTNESS_RANGE = (0.42, 0.55)


def user_color(name: str) -> str:
    """Return a stable midtone color derived from the NFC-normalized name.

    ``hashlib`` is deliberately used instead of Python's process-randomized
    ``hash()`` so a user's color remains unchanged across service restarts.
    """

    return color_from_key(unicodedata.normalize("NFC", name))


def color_from_key(key: str) -> str:
    """Return a pleasant deterministic color derived from an arbitrary key."""

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    hue = int.from_bytes(digest[0:4], "big") / 2**32
    saturation = (
        _SATURATION_RANGE[0]
        + int.from_bytes(digest[4:6], "big") / 65535
        * (_SATURATION_RANGE[1] - _SATURATION_RANGE[0])
    )
    lightness = (
        _LIGHTNESS_RANGE[0]
        + int.from_bytes(digest[6:8], "big") / 65535
        * (_LIGHTNESS_RANGE[1] - _LIGHTNESS_RANGE[0])
    )
    return _quantized_hsl(hue, saturation, lightness)


def text_color(name: str) -> str:
    """Return the readable foreground color for a user's badge."""

    del name
    return WHITE


def _to_byte(component: float) -> int:
    return max(0, min(255, int(component * 255 + 0.5)))


def _quantized_hsl(hue: float, saturation: float, lightness: float) -> str:
    """Encode HSL while keeping the decoded 8-bit color inside the bands."""

    saturation_midpoint = sum(_SATURATION_RANGE) / 2.0
    lightness_midpoint = sum(_LIGHTNESS_RANGE) / 2.0
    for _ in range(8):
        red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
        components = (_to_byte(red), _to_byte(green), _to_byte(blue))
        _decoded_hue, decoded_lightness, decoded_saturation = colorsys.rgb_to_hls(
            *(component / 255 for component in components)
        )
        if (
            _SATURATION_RANGE[0] <= decoded_saturation <= _SATURATION_RANGE[1]
            and _LIGHTNESS_RANGE[0] <= decoded_lightness <= _LIGHTNESS_RANGE[1]
        ):
            return "#{:02x}{:02x}{:02x}".format(*components)
        saturation = (saturation + saturation_midpoint) / 2.0
        lightness = (lightness + lightness_midpoint) / 2.0
    raise RuntimeError("could not quantize an in-band user color")
