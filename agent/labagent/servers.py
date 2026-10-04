"""Alamat server agent: ditanam saat build (`_build.py`, dibuat build.ps1 dari deploy\\build.conf).

Agent memakai alamat yang terakhir berhasil, dan mencoba alamat lain bila gagal, sehingga PC tetap
terhubung baik lewat LAN maupun lewat internet (Cloudflare Tunnel).
"""
try:
    from ._build import SERVER_URLS as BUILT_IN
except ImportError:          # dijalankan dari sumber (tes/pengembangan) tanpa build
    BUILT_IN = ()


class Servers:
    def __init__(self, urls):
        self.urls = [str(u).rstrip("/") for u in urls]
        if not self.urls:
            raise ValueError("tidak ada alamat server")
        self._active = 0

    @property
    def active(self) -> str:
        return self.urls[self._active]

    def call(self, request):
        """`request(base_url)` dicoba pada alamat aktif dulu, lalu alamat lain; galat terakhir dilempar."""
        order = [self._active] + [i for i in range(len(self.urls)) if i != self._active]
        error = None
        for index in order:
            try:
                result = request(self.urls[index])
            except Exception as exc:
                error = exc
                continue
            self._active = index
            return result
        raise error
