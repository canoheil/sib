"""Verified static assets and the existing Persian text shaping pipeline."""
from pathlib import Path
from functools import lru_cache
import json
from PIL import Image
from PIL import ImageFont, features
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError as exc:
    raise ImportError("Persian invasion imaging needs arabic-reshaper and python-bidi") from exc

ROOT = Path(__file__).resolve().parents[3]
FONT_CANDIDATES = (
    ROOT / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Bold.ttf",
    ROOT / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Regular.ttf",
)
FONT_PATH = next((path for path in FONT_CANDIDATES if path.is_file()), None)
if FONT_PATH is None:
    raise FileNotFoundError("Persian font missing; searched: " + ", ".join(map(str, FONT_CANDIDATES)))

SIZE = (1280, 720)

@lru_cache(maxsize=32)
def font(size: int):
    return ImageFont.truetype(str(FONT_PATH), size)


def label(draw, xy, text: str, size: int, *, fill="#f3eee1", anchor="ra",
          stroke_width=1, stroke_fill="#090b12", max_width=None):
    """Shape with libraqm where available, otherwise local reshaper + bidi."""
    # Keep the established raqm / reshaper+bidi selection and shaping unchanged.
    raqm = features.check("raqm")
    visual = str(text) if raqm else get_display(arabic_reshaper.reshape(str(text)))
    kwargs = {"direction": "rtl"} if raqm else {}
    if max_width:
        while size > 1 and draw.textlength(visual, font=font(size), **kwargs) > max_width:
            size -= 1
    draw.text(xy, visual, font=font(size), fill=fill, anchor=anchor,
              stroke_width=stroke_width, stroke_fill=stroke_fill, **kwargs)


# Static source assets: copied on access so composition never mutates the cache.
ASSETS_ROOT = Path(__file__).resolve().parents[3] / "assets" / "invasion_v2"
ENEMY_TYPES = ["soldier", "shadow", "commander", "brute", "king"]
LAYOUT_NAMES = ("announcement", "join", "victory", "defeat")
# IV3 random-event art. Optional by design: a missing image degrades the
# event message to text instead of failing a battle, so these are NOT part of
# ``verify_assets`` (which fails the import when required art is absent).
EVENT_ASSETS = ASSETS_ROOT / "events"


def verify_assets() -> None:
    paths = [ASSETS_ROOT / "manifest.json"]
    paths += [ASSETS_ROOT / "backgrounds" / f"{name}.jpg" for name in ENEMY_TYPES]
    paths += [ASSETS_ROOT / "silhouettes" / f"{name}.png" for name in ENEMY_TYPES]
    paths += [ASSETS_ROOT / "layouts" / f"{name}.jpg" for name in LAYOUT_NAMES]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Invasion v2 assets missing: " + ", ".join(missing))


def asset_version() -> str:
    """Read on demand: a manifest bump invalidates disk and decoded caches."""
    with (ASSETS_ROOT / "manifest.json").open(encoding="utf-8") as source:
        return str(json.load(source)["version"])


@lru_cache(maxsize=32)
def _decoded(path: Path, version: str, mtime_ns: int, size: int) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA" if path.suffix == ".png" else "RGB")


def _load(folder: str, name: str, suffix: str, allowed) -> Image.Image:
    if name not in allowed:
        raise ValueError(f"Unknown invasion {folder} asset: {name}")
    path = ASSETS_ROOT / folder / f"{name}{suffix}"
    # Stat even on a cache hit: removing a deployed asset must not be hidden.
    stat = path.stat()
    return _decoded(path, asset_version(), stat.st_mtime_ns, stat.st_size).copy()


def load_background(enemy_type: str) -> Image.Image:
    return _load("backgrounds", enemy_type, ".jpg", ENEMY_TYPES)


def load_silhouette(enemy_type: str) -> Image.Image:
    return _load("silhouettes", enemy_type, ".png", ENEMY_TYPES)


def load_layout(name: str) -> Image.Image:
    return _load("layouts", name, ".jpg", LAYOUT_NAMES)


def event_image_path(code: str) -> Path | None:
    """Path to one event's art, or ``None`` when it has not been generated.

    Deliberately speculative rather than fatal: the presentation layer falls
    back to a text message for that event and every other event keeps its
    image. ``code`` is always a catalogue key, never user input.
    """
    if not code or "/" in code or "\\" in code or not code.replace("_", "").isalnum():
        return None
    path = EVENT_ASSETS / f"{code}.jpg"
    return path if path.is_file() and path.stat().st_size else None


verify_assets()
