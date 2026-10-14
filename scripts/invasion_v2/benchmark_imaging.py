"""Write five Phase 8A samples and measure 20 real warm compositions each.

Run from repository root with both namespace roots on PYTHONPATH:
  PYTHONPATH="$(dirname "$PWD"):$PWD" python scripts/invasion_v2/benchmark_imaging.py
No database, Telegram, disk-result cache, or asset generation is involved.
"""
import json
import platform
from pathlib import Path
from statistics import mean
from time import perf_counter

from PIL import __version__ as pillow_version, features
from sib.services.invasion_v2.imaging import (
    compose_announcement_image, compose_join_image, compose_result_image,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "docs/invasion-rework/samples/phase-8a"


def main():
    card = (ROOT / "assets/characters/green_knight.jpg").read_bytes()
    cases = {
        "announcement": lambda: compose_announcement_image("soldier", "درّه ماه", "شیاسیب پیاده"),
        "join-normal": lambda: compose_join_image("shadow", card, "داریوش", 888, False, 12345),
        "join-crit": lambda: compose_join_image("shadow", card, "داریوش", 1337, True, 11008),
        "victory": lambda: compose_result_image("victory", "درّه ماه", "king", ["سیب طلایی ×۳", "سیب الماسی ×۱"]),
        "defeat": lambda: compose_result_image("defeat", "درّه ماه", "brute", []),
    }
    result = {"python": platform.python_version(), "pillow": pillow_version,
              "raqm": features.check("raqm"), "platform": platform.platform(),
              "runs": 20, "measurements": {}}
    SAMPLES.mkdir(parents=True, exist_ok=True)
    for name, build in cases.items():
        started = perf_counter()
        data = build()
        cold = (perf_counter()-started)*1000
        (SAMPLES / f"{name}.jpg").write_bytes(data)
        timings = []
        for _ in range(20):
            start = perf_counter()
            build()
            timings.append((perf_counter()-start)*1000)
        result["measurements"][name] = {"first_call_ms": round(cold, 2),
            "mean_ms": round(mean(timings), 2), "max_ms": round(max(timings), 2),
            "output_bytes": len(data), "path": str((SAMPLES / f"{name}.jpg").relative_to(ROOT))}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
