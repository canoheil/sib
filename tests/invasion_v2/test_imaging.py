"""Static asset integrity and real (not disk-cache-hit) composition checks."""
import hashlib
import json
import logging
import runpy
import shutil
from io import BytesIO
from pathlib import Path
from statistics import mean
from time import perf_counter

import pytest
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageFilter

from sib.services.invasion_v2.imaging import (
    compose_announcement_image, compose_join_image, compose_result_image,
)
from sib.services.invasion_v2.imaging import cache, compose, templates

ROOT = templates.ASSETS_ROOT
CARD = ROOT.parent / "characters" / "green_knight.jpg"
EXPECTED = (
    [f"backgrounds/{name}.jpg" for name in templates.ENEMY_TYPES]
    + [f"silhouettes/{name}.png" for name in templates.ENEMY_TYPES]
    + [f"layouts/{name}.jpg" for name in templates.LAYOUT_NAMES]
)


def test_manifest_matches_files():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["version"] == templates.asset_version()
    assert manifest["generator"] == "manual-ai"
    assert manifest["generated_at"].endswith("Z")
    assert len(manifest["assets"]) == 14
    assert {entry["path"] for entry in manifest["assets"]} == set(EXPECTED)
    for entry in manifest["assets"]:
        path = ROOT / entry["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        with Image.open(path) as image:
            assert image.size == (entry["width"], entry["height"])


def test_all_assets_exist():
    templates.verify_assets()
    assert all((ROOT / name).is_file() for name in EXPECTED)
    assert {p.relative_to(ROOT).as_posix() for p in ROOT.glob("*/*")} == set(EXPECTED)


@pytest.mark.parametrize("path", EXPECTED)
def test_dimensions(path):
    with Image.open(ROOT / path) as image:
        if path.startswith("silhouettes/"):
            assert image.size == (400, 560)
            assert image.mode == "RGBA" and image.format == "PNG"
            assert image.getchannel("A").getextrema() == (0, 255)
            assert image.crop((0, 500, 400, 560)).getchannel("A").getbbox() is None
        else:
            assert image.size == (1280, 720)
            assert image.mode == "RGB" and image.format == "JPEG"
        profile = ImageCms.ImageCmsProfile(BytesIO(image.info["icc_profile"]))
        assert "srgb" in ImageCms.getProfileDescription(profile).lower()


def _assert_jpeg(data):
    assert isinstance(data, bytes)
    with Image.open(BytesIO(data)) as image:
        assert image.format == "JPEG" and image.size == (1280, 720)
        image.load()


@pytest.mark.parametrize("enemy", templates.ENEMY_TYPES)
def test_compose_announcement(enemy):
    _assert_jpeg(compose_announcement_image(enemy, "درّه ماه", "شیاسیب پیاده"))


def test_compose_join_normal_vs_crit():
    args = ("shadow", CARD.read_bytes(), "داریوش", 888)
    normal = compose_join_image(*args, False, 12345)
    critical = compose_join_image(*args, True, 12345)
    _assert_jpeg(normal)
    _assert_jpeg(critical)
    assert normal != critical
    assert normal != compose_join_image(*args, False, 12344)


def test_compose_result_victory_vs_defeat():
    victory = compose_result_image("victory", "درّه ماه", "king", ["سیب طلایی ×۳"])
    defeat = compose_result_image("defeat", "درّه ماه", "king", ["سیب طلایی ×۳"])
    _assert_jpeg(victory)
    _assert_jpeg(defeat)
    assert victory != defeat
    assert victory != compose_result_image("victory", "درّه ماه", "king", [])
    assert defeat == compose_result_image("defeat", "درّه ماه", "king", [])


def test_cache_key_changes_with_manifest_version(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads((ROOT / "manifest.json").read_text())
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(templates, "ASSETS_ROOT", tmp_path)
    original = cache.cache_key("announcement", 123, "soldier")
    manifest["version"] = f"{manifest['version']}-test-bump"
    manifest_path.write_text(json.dumps(manifest))
    assert cache.cache_key("announcement", 123, "soldier") != original


def test_missing_asset_raises(tmp_path, monkeypatch):
    # Rename an isolated deployment copy, never a shared checked-in file.
    shutil.copytree(ROOT, tmp_path / "assets")
    monkeypatch.setattr(templates, "ASSETS_ROOT", tmp_path / "assets")
    templates.load_background("soldier")  # Warm decoded cache must not hide removal.
    source = tmp_path / "assets/backgrounds/soldier.jpg"
    source.rename(source.with_suffix(".missing"))
    with pytest.raises(FileNotFoundError, match="backgrounds/soldier.jpg"):
        templates.verify_assets()
    with pytest.raises(FileNotFoundError):
        templates.load_background("soldier")


def test_import_lists_all_missing_assets(tmp_path):
    # Exercise module-level verification, without reloading the live test module.
    module = tmp_path / "services/invasion_v2/imaging/templates.py"
    module.parent.mkdir(parents=True)
    shutil.copyfile(templates.__file__, module)
    font = tmp_path / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Bold.ttf"
    font.parent.mkdir(parents=True)
    shutil.copyfile(templates.FONT_PATH, font)
    with pytest.raises(FileNotFoundError) as error:
        runpy.run_path(str(module))
    for path in ["manifest.json", *EXPECTED]:
        assert path in str(error.value)


@pytest.mark.parametrize("critical", [False, True])
def test_compose_join_fast(critical):
    card = CARD.read_bytes()
    def build():
        return compose_join_image("soldier", card, "داریوش", 888, critical, 12345)
    build()  # Warm decoded plates, font and card-effect masks; no final-image cache.
    durations = []
    for _ in range(20):
        start = perf_counter()
        build()
        durations.append(perf_counter()-start)
    assert mean(durations) < .200, durations
    assert max(durations) < .300, durations


def test_decoded_assets_are_not_mutated():
    before = templates.load_layout("join").tobytes()
    altered = templates.load_layout("join")
    altered.paste("red", (0, 0, 1280, 720))
    assert templates.load_layout("join").tobytes() == before
    enemy = templates.load_silhouette("soldier").tobytes()
    compose_announcement_image("soldier", "درّه ماه", "شیاسیب پیاده")
    assert templates.load_silhouette("soldier").tobytes() == enemy


@pytest.mark.parametrize("loader,value", [
    (templates.load_background, "../soldier"),
    (templates.load_silhouette, "unknown"),
    (templates.load_layout, "unknown"),
])
def test_invalid_asset_name(loader, value):
    with pytest.raises(ValueError):
        loader(value)


def test_invalid_result_status():
    with pytest.raises(ValueError):
        compose_result_image("expired", "درّه ماه", "king", [])


def test_announcement_panel_contrast():
    def linear(value):
        v = value / 255
        return v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4
    lut = [linear(v) for v in range(256)]
    image = templates.load_layout("announcement")
    # Maximum luminance among every decoded JPEG pixel in the right third.
    panel = image.crop((854, 0, 1280, 720))
    pixels = panel.load()
    luminance = max(.2126*lut[r]+.7152*lut[g]+.0722*lut[b]
                    for y in range(panel.height) for x in range(panel.width)
                    for r, g, b in [pixels[x, y]])
    assert 1.05/(luminance+.05) >= 4.5


def test_cache_accepts_jpeg_bytes_and_prunes(tmp_path):
    calls = []
    data = compose_announcement_image("soldier", "درّه ماه", "شیاسیب پیاده")
    def build():
        calls.append(1)
        return data
    path = cache.cached_image(("bytes",), build, directory=tmp_path)
    assert path.read_bytes() == data
    assert cache.cached_image(("bytes",), build, directory=tmp_path) == path
    assert calls == [1]
    assert cache.prune(directory=tmp_path, now=path.stat().st_mtime+cache.MAX_AGE+1) == 1


def test_cache_failed_build_is_atomic(tmp_path):
    def build():
        raise RuntimeError("render failed")
    with pytest.raises(RuntimeError, match="render failed"):
        cache.cached_image(("broken",), build, directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_card_aspect_ratio_and_legacy_placeholder():
    # Wide and tall cards fit without cropping or distortion.
    for size in [(1000, 200), (200, 1000)]:
        card = Image.new("RGB", size, "red")
        data = BytesIO()
        card.save(data, "PNG")
        _assert_jpeg(compose_join_image("soldier", data.getvalue(), "داریوش", 8, False, 90))
        canvas = Image.new("RGBA", (1280, 720))
        compose._paste_card(canvas, data.getvalue())
        red, green, _, _ = canvas.split()
        box = ImageChops.subtract(red, green).point(lambda v: 255 if v > 200 else 0).getbbox()
        width, height = box[2]-box[0], box[3]-box[1]
        assert width <= 420 and height <= 580
        assert abs(width/height - size[0]/size[1]) < .01
        assert (box[0]+box[2])//2 == 640
    assert compose.join_image("soldier", None, "داریوش", 0, False, placeholder=True).size == (1280, 720)
    with pytest.raises(FileNotFoundError):
        compose.join_image("soldier", Path("nonexistent.jpg"), "داریوش", 1, False)


# --- Phase 8E: phone-legible typography, shadows, glow, dramatic status words ---


def _spy_font(monkeypatch):
    """Record every font size requested through the shared templates.font."""
    sizes = []
    real_font = templates.font

    def spy(size):
        sizes.append(size)
        return real_font(size)

    monkeypatch.setattr(templates, "font", spy)
    return sizes


def _spy_draw(monkeypatch, record=None):
    """Wrap ImageDraw.Draw inside compose; record (text, kwargs) and/or pills."""
    events = []
    real_draw = ImageDraw.Draw

    class _Spy:
        @staticmethod
        def Draw(image):
            draw = real_draw(image)
            original_text = draw.text

            def record_text(xy, text, **kwargs):
                events.append(("text", text, kwargs))
                return original_text(xy, text, **kwargs)

            draw.text = record_text
            if record is not None:
                original_pill = draw.rounded_rectangle

                def record_pill(box, **kwargs):
                    events.append(("pill", box, kwargs))
                    return original_pill(box, **kwargs)

                draw.rounded_rectangle = record_pill
            return draw

    monkeypatch.setattr(compose, "ImageDraw", _Spy)
    return events


def _spy_blur_radii(monkeypatch):
    radii = []
    real_blur = ImageFilter.GaussianBlur

    def spy(radius):
        radii.append(radius)
        return real_blur(radius)

    monkeypatch.setattr(compose.ImageFilter, "GaussianBlur", spy)
    return radii


def test_announcement_region_font_size(monkeypatch):
    sizes = _spy_font(monkeypatch)
    compose.compose_announcement_image("soldier", "درّه ماه", "شیاسیب پیاده")
    assert 88 in sizes and max(sizes) >= 88


def test_join_damage_font_size(monkeypatch):
    sizes = _spy_font(monkeypatch)
    compose.compose_join_image("soldier", CARD.read_bytes(), "داریوش", 88, False, 123)
    assert 160 in sizes


def test_result_status_font_size(monkeypatch):
    sizes = _spy_font(monkeypatch)
    compose.compose_result_image("victory", "درّه ماه", "king", [])
    assert 200 in sizes


def test_region_name_font_size(monkeypatch):
    sizes = _spy_font(monkeypatch)
    compose.compose_result_image("defeat", "درّه ماه", "king", [])
    assert 88 in sizes


def test_defeat_fill_is_deep_red(monkeypatch):
    events = _spy_draw(monkeypatch)
    compose.compose_result_image("defeat", "درّه ماه", "king", [])
    word = compose._shaped("شکست")[0]
    fills = {kw["fill"] for kind, text, kw in events if kind == "text" and text == word}
    # deep blood red fill, its #8B0000 glow, and the black shadow silhouette
    assert fills == {"#7A0E0E", "#8B0000", (0, 0, 0, 255)}


def test_victory_fill_is_deep_gold(monkeypatch):
    events = _spy_draw(monkeypatch)
    compose.compose_result_image("victory", "درّه ماه", "king", [])
    word = compose._shaped("پیوروزی")[0]
    fills = {kw["fill"] for kind, text, kw in events if kind == "text" and text == word}
    assert fills == {"#C8A02E", "#E8C766", (0, 0, 0, 255)}


def test_text_helper_adds_shadow(monkeypatch):
    events = []
    real_blur = ImageFilter.GaussianBlur

    def spy_blur(radius):
        events.append("blur")
        return real_blur(radius)

    monkeypatch.setattr(compose.ImageFilter, "GaussianBlur", spy_blur)
    real_draw = ImageDraw.Draw

    class _Spy:
        @staticmethod
        def Draw(image):
            draw = real_draw(image)
            original = draw.text

            def record(xy, text, **kwargs):
                events.append(f"text:{kwargs.get('fill')}")
                return original(xy, text, **kwargs)

            draw.text = record
            return draw

    monkeypatch.setattr(compose, "ImageDraw", _Spy)
    img = Image.new("RGBA", (320, 160), (255, 255, 255, 255))
    compose._draw_text_with_shadow(img, (160, 80), "سلام", templates.font(48),
                                   fill=(200, 30, 30), anchor="mm")
    assert "blur" in events
    colored = f"text:{(200, 30, 30)}"
    assert colored in events
    assert events.index("blur") < events.index(colored)  # shadow before text
    # The blurred shadow must actually darken the pixels below-right of the ink.
    probe = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(probe).text((160, 80), compose._shaped("سلام")[0],
                               font=templates.font(48), fill=(0, 0, 0, 255), anchor="mm")
    box = probe.getchannel("A").getbbox()
    pixels = img.load()
    # the (3, 3)-offset blurred shadow must darken the band right/below the ink
    band = [sum(pixels[x, y][:3])
            for y in range(box[3] + 1, box[3] + 20)
            for x in range(box[0], box[2] + 20)]
    assert min(band) < sum(pixels[2, 2][:3])


def test_damage_glow_present_for_crit_and_kill(monkeypatch):
    card = CARD.read_bytes()
    radii = _spy_blur_radii(monkeypatch)
    compose.compose_join_image("soldier", card, "داریوش", 88, False, 123)
    assert 12 in radii  # normal hit glow
    radii.clear()
    compose.compose_join_image("soldier", card, "داریوش", 88, True, 123)
    assert 16 in radii  # crit glow
    radii.clear()
    compose.compose_join_image("soldier", card, "داریوش", 88, True, 0)
    assert 16 in radii  # killing-blow glow
    events = _spy_draw(monkeypatch)
    compose.compose_join_image("soldier", card, "داریوش", 88, True, 0)
    digits = compose._digits(88)
    fills = {kw["fill"] for kind, text, kw in events if kind == "text" and text == digits}
    assert "#B8E986" in fills  # kill color wins over crit gold


def test_vignette_produces_alpha_gradient():
    img = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    compose._draw_radial_vignette(img, (200, 150), 150, 100, 0.8, color=(0, 0, 0))
    alpha = img.getchannel("A")
    center = alpha.getpixel((200, 150))
    mid = alpha.getpixel((275, 150))
    corner = alpha.getpixel((5, 5))
    assert center >= int(0.8 * 255) - 3
    assert 0 < mid < center  # smooth fade toward the ellipse edge
    assert corner <= 2


def test_region_name_backdrop_present(monkeypatch):
    events = _spy_draw(monkeypatch, record=True)
    compose.compose_result_image("victory", "درّه ماه", "king", [])
    pills = [e for e in events if e[0] == "pill"]
    assert pills, "no rounded pill backdrop was drawn"
    first_pill = next(i for i, e in enumerate(events) if e[0] == "pill")
    assert pills[0][2]["fill"] == (0, 0, 0, 140) and pills[0][2]["radius"] == 20
    region_text = compose._shaped("درّه ماه")[0]
    region_draws = [i for i, e in enumerate(events)
                    if e[0] == "text" and e[1] == region_text]
    assert any(i > first_pill for i in region_draws)  # pill before the visible text


def test_long_region_name_auto_shrinks(monkeypatch, caplog):
    sizes = _spy_font(monkeypatch)
    long_name = "محوطهٔ بزرگ نگهبانان درّهٔ ماه" * 2
    with caplog.at_level(logging.WARNING, logger="sib"):
        compose.compose_result_image("victory", long_name, "king", [])
    region_run = []
    for size in sizes:
        if size == 200:  # status word: the region shrink loop is over
            break
        region_run.append(size)
    assert region_run[0] == 88
    final = region_run[-1]
    assert 56 <= final < 88
    assert any("inv2 result region name too long" in record.message
               for record in caplog.records)
