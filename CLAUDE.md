# Lab Control

Perangkat lunak manajemen lab komputer milik sekolah: server Linux (`server/`, Flask + SQLite) dan agent Windows (`agent/`, Python + Tkinter) yang membatasi pemakaian PC di luar jadwal sesi. Pemasangan lengkap ada di `README.md`.

## Perintah
- Uji server: `cd server && python -m pytest`
- Uji agent: `cd agent && python -m pytest` (hanya logika; `winapi.py` dan `overlay.py` khusus Windows)
- Jalankan server: `cd server && python run.py` (membuat `config.toml` bila belum ada)

## Struktur
- `server/labserver/policy.py`: keputusan kunci (per-PC > semua PC > jadwal), fungsi murni
- `server/labserver/app.py`: API agent (`/api/agent/poll`, token) dan API admin (Basic auth + header `X-Lab-Admin: 1`)
- `agent/labagent/core.py`: logika agent tanpa ketergantungan Windows
- `agent/labagent/winapi.py`, `overlay.py`: bagian Windows
- `agent/labagent/killswitch.py`: pemulihan darurat admin (lihat bawah); logikanya murni, bagian Windows ada di `winapi.py`

## Pemulihan darurat (pencopotan lewat drive)
Untuk saat server tidak bisa dipakai dan PC lab tidak bisa dibuka. `labagent.exe --watch-usb` (tugas `LabAgentKillSwitch`, akun SYSTEM) membaca sekali tiap drive yang terpasang, hanya nama file tingkat pertama. Jika ada file bernama sesuai `kill_file_sha256` di `config.json`, agent dicopot: tugas terjadwal, proses, kebijakan registry, dan foldernya dihapus.
- Diaktifkan lewat `install.ps1 -KillFileName <nama-file>`; yang disimpan hanya SHA-256 nama itu. Kosong = fitur mati.
- Nama file dipilih dan dipegang admin; jangan dicetak atau di-commit.

## Mengoperasikan lab lewat API
Kredensial lewat variabel lingkungan, jangan ditulis ke file:
```bash
export LAB_URL=http://192.168.1.10:8080   # atau https://lab.contoh.com
export LAB_USER=admin LAB_PASS='...'      # dari server/config.toml
api() { curl -sS -u "$LAB_USER:$LAB_PASS" -H 'X-Lab-Admin: 1' -H 'Content-Type: application/json' "$@"; }

api $LAB_URL/api/pcs                                        # status semua PC
api "$LAB_URL/api/events?pc=PC-01&limit=50"                 # log
api -X POST $LAB_URL/api/pcs/PC-01/lock   -d '{"minutes":30}'   # tanpa "minutes" = tanpa batas
api -X POST $LAB_URL/api/pcs/PC-01/unlock -d '{"minutes":60}'
api -X POST $LAB_URL/api/pcs/PC-01/clear                    # kembali ke jadwal
api -X POST $LAB_URL/api/all/lock -d '{}'                   # juga: unlock, clear
api -X POST $LAB_URL/api/pcs/PC-01/command -d '{"action":"restart"}'   # atau shutdown
api -X PUT  $LAB_URL/api/schedule -d '{"enabled":true,"rules":[{"days":[0,1,2,3,4],"start":"08:00","end":"16:00"}]}'
```
Hari: 0 = Senin ... 6 = Minggu. Bila dashboard di belakang Cloudflare Access, tambahkan header `CF-Access-Client-Id` dan `CF-Access-Client-Secret`.

## Aturan kerja
- Minta konfirmasi sebelum: mengunci/membuka semua PC, shutdown/restart, mengubah jadwal, menghapus PC.
- Jangan menampilkan atau meng-commit `config.toml`, password dashboard, `agent_token`, atau nama file pemulihan.
- Jangan mengubah `agent_token` atau `block_agent_via_tunnel` tanpa persetujuan; `/api/agent` memang tidak dibuka ke internet.
- Kode Windows tidak bisa diuji di Linux; jangan menyatakan berhasil sebelum dicoba di satu PC Windows.

## Belum selesai
- Pemulihan darurat: logika dan tes selesai (`tests/test_killswitch.py`), tetapi `winapi.list_drives`, `list_names`, `remove_agent`, mode `--watch-usb`, dan tugas SYSTEM di `install.ps1` **belum dicoba di Windows**. Uji di satu PC: pasang dengan `-KillFileName`, colok drive berisi file itu, pastikan agent hilang. Cek juga pemasangan ulang oleh watchdog tidak terjadi.
- `labagent.exe` perlu dibangun ulang (`build.ps1`) dan dipasang ulang agar mode `--watch-usb` ada.
- Profil user yang **tidak sedang login** tidak dibersihkan dari kebijakan registry saat pencopotan (sama dengan `uninstall.ps1`).
- Uji Windows umum yang belum tercatat: overlay di semua monitor, keyboard terblokir, layar Ctrl+Alt+Del, tombol shutdown.
