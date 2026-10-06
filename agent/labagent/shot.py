"""Kompresi screenshot untuk dikirim ke server. Pillow dimuat saat dipakai (tidak ikut mode --helper/--watch-usb).

Pratinjau di dashboard paling lebar sekitar 650 px, jadi gambar diperkecil ke lebar maks 800 px dan disimpan
sebagai JPEG berkualitas rendah. Bila masih di atas batas ukuran, kualitas diturunkan bertahap. Hasil umumnya
20-60 KB, jauh di bawah layar penuh, agar banyak PC bisa mengirim tiap beberapa detik tanpa membebani jaringan.
"""
import io

MAX_SIDE = 800
QUALITIES = (45, 35, 25, 15)
MAX_BYTES = 80 * 1024


def compress(image) -> bytes:
    """`image`: PIL.Image dari ImageGrab. Mengembalikan bytes JPEG (kualitas terendah bila batas ukuran tak tercapai)."""
    from PIL import Image

    image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    data = b""
    for quality in QUALITIES:
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality, optimize=True, subsampling=2)   # subsampling 2 = 4:2:0
        data = out.getvalue()
        if len(data) <= MAX_BYTES:
            break
    return data
