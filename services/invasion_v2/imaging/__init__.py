"""Offline Pillow composition for Invasion v2. Missing dependencies are fatal."""
try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
except ImportError as exc:
    raise ImportError("Invasion v2 imaging requires Pillow (pip install Pillow)") from exc

from .templates import font, FONT_PATH

__all__ = ["font", "FONT_PATH"]
