"""Static castle banners; walk backwards if a level is not deployed."""

from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "castle"


def castle_image(level: int, directory: Path = ASSET_DIR):
    for candidate in range(min(10, level), 0, -1):
        path = directory / f"level_{candidate}.jpg"
        if path.is_file():
            return path
    return None
