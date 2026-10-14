"""Atomic, content-addressed local JPEG cache; no network or DB I/O."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable
from PIL import Image
from .templates import asset_version

CACHE_DIR = Path.home() / ".cache" / "inv2"
MAX_AGE = 30 * 24 * 60 * 60


def cache_key(*parts: object) -> str:
    return hashlib.sha256(json.dumps((asset_version(), *parts), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def cached_image(parts: tuple[object, ...], build: Callable[[], Image.Image | bytes], *, directory: Path | None = None) -> Path:
    directory = Path(directory or CACHE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (cache_key(*parts) + ".jpg")
    if path.is_file() and path.stat().st_size:
        return path
    fd, temp = tempfile.mkstemp(dir=directory, suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as output:
            image = build()
            if isinstance(image, bytes):
                output.write(image)
            else:
                image.convert("RGB").save(output, format="JPEG", quality=88)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path


def prune(*, directory: Path | None = None, now: float | None = None) -> int:
    directory = Path(directory or CACHE_DIR)
    if not directory.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - MAX_AGE
    removed = 0
    for path in directory.glob("*.jpg"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass  # best-effort cleanup, not a startup blocker
    return removed
