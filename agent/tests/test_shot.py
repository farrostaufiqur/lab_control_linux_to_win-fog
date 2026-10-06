import io
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

Image = pytest.importorskip("PIL.Image")

from labagent import shot  # noqa: E402


def desktop(width=1920, height=1080):
    """Mirip layar: blok warna rata, teks (garis tipis), dan sedikit derau."""
    from PIL import ImageDraw
    image = Image.new("RGB", (width, height), (240, 240, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height - 48, width, height), fill=(20, 24, 40))
    for y in range(80, height - 80, 22):
        draw.line((120, y, 120 + random.Random(y).randint(300, 1200), y), fill=(30, 30, 30), width=2)
    return image


def test_compress_is_small_jpeg_within_max_side():
    data = shot.compress(desktop())
    assert data.startswith(b"\xff\xd8\xff")
    out = Image.open(io.BytesIO(data))
    assert out.format == "JPEG" and max(out.size) <= shot.MAX_SIDE and out.size == (800, 450)
    assert len(data) <= shot.MAX_BYTES


def test_compress_is_much_smaller_than_full_resolution():
    image = desktop()
    full = io.BytesIO()
    image.save(full, "JPEG", quality=85)
    assert len(shot.compress(image)) < len(full.getvalue()) / 5


def test_quality_drops_until_under_limit(monkeypatch):
    noisy = Image.effect_noise((1600, 900), 90).convert("RGB")        # sulit dikompres
    monkeypatch.setattr(shot, "MAX_BYTES", 10_000_000)
    first = shot.compress(noisy)
    monkeypatch.setattr(shot, "MAX_BYTES", len(first) // 2)
    second = shot.compress(noisy)
    assert len(second) < len(first)


def test_small_and_non_rgb_images_are_handled():
    assert Image.open(io.BytesIO(shot.compress(Image.new("RGBA", (200, 100), (1, 2, 3, 4))))).size == (200, 100)
    assert Image.open(io.BytesIO(shot.compress(Image.new("L", (64, 64), 128)))).mode == "RGB"
