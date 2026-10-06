# Lab Control

Perangkat lunak manajemen lab komputer milik sekolah: server Linux (`server/`, Flask + SQLite) dan agent Windows (`agent/`, Python + Tkinter) yang membatasi pemakaian PC di luar jadwal sesi. Pemasangan lengkap ada di `README.md`.

## Perintah
- Uji server: `cd server && python -m pytest`
- Uji agent: `cd agent && python -m pytest` (hanya logika; `winapi.py` dan `overlay.py` khusus Windows)
- Jalankan server: `cd server && python run.py` (membuat `config.toml` bila belum ada)

## Struktur
- `server/labserver/policy.py`: keputusan kunci (per-PC > semua PC > jadwal), fungsi murni
- `server/labserver/app.py`: API agent (`/api/agent/poll`, token), API admin, dan halaman dashboard. Login: form `/login` + cookie sesi `labsession` (12 jam, HttpOnly, SameSite=Lax, Secure bila HTTPS; kunci sesi di tabel settings `session_secret`; mengganti `admin_password` mengakhiri semua sesi). Skrip/curl tetap bisa memakai Basic auth. Semua permintaan non-GET, termasuk yang bersesi, wajib header `X-Lab-Admin: 1`. Tidak ada tantangan `WWW-Authenticate`, jadi browser tidak memunculkan prompt
- `server/labserver/templates/`: satu halaman per menu sidebar (`dashboard`, `pc`, `jadwal`, `log`, `pembaruan`, `pengaturan`) di atas `base.html`; JS/CSS bersama di `static/lab.js`, `static/lab.css`
- `server/labserver/static/vendor/`: aset template admin (salinan dari `WEBSITE/fog/admin/html`, sama dengan admin fog.tf); jangan diedit selain dua tambalan kecil (`?.` pada `.navbar-logo`, impor highlight dibuang). `/static` terbuka tanpa login
- `agent/labagent/core.py`: logika agent tanpa ketergantungan Windows
- `agent/labagent/winapi.py`, `overlay.py`: bagian Windows
- `agent/labagent/updater.py`, `helper.py`: pembaruan otomatis dan pembantu SYSTEM (lihat bawah); logika murni, efek samping disuntikkan lewat `helper.Ports`
- `server/labserver/release.py`: penyimpanan rilis (satu exe aktif, `<folder db>/releases/`); batas versi/ukuran harus sama dengan `agent/labagent/updater.py` (dicek tes)
- `agent/labagent/killswitch.py`: pemulihan darurat admin (lihat bawah); logikanya murni, bagian Windows ada di `winapi.py`

## Pemulihan darurat (pencopotan lewat drive)
Untuk saat server tidak bisa dipakai dan PC lab tidak bisa dibuka. `labagent.exe --watch-usb` (tugas `LabAgentKillSwitch`, akun SYSTEM) membaca sekali tiap drive yang terpasang, hanya nama file tingkat pertama. Jika ada file bernama sesuai `kill_file_sha256` di `config.json`, agent dicopot: tugas terjadwal, proses, kebijakan registry, dan foldernya dihapus.
- Diaktifkan lewat `install.ps1 -KillFileName <nama-file>`; yang disimpan hanya SHA-256 nama itu. Kosong = fitur mati.
- Nama file dipilih dan dipegang admin; jangan dicetak atau di-commit.

## Pembaruan otomatis dan pembantu SYSTEM
- `labagent.exe --helper` (tugas `LabAgentHelper`, SYSTEM, AtStartup) menulis 4 nilai registry Ctrl+Alt+Del ke `HKEY_USERS\<sid>` dari `state_dir\policy.json` (divalidasi ketat, folder itu bisa ditulis user) dan memasang rilis baru. Berkas yang menentukan exe (unduhan, `apply-update.cmd`, `updater.json`, `helper.ok`) hanya di folder pemasangan (admin).
- Rilis = exe yang diunggah admin di dashboard (`POST /api/release`, hanya exe + versi opsional); **tanpa tanda tangan, kunci, atau JSON** (dihapus 2026-10-06 atas permintaan pemilik). Agent hanya memeriksa ukuran, SHA-256, dan awalan `MZ`. Konsekuensinya: akses admin atau `agent_token` di jalur HTTP LAN cukup untuk menyebarkan kode SYSTEM ke semua PC.
- Keputusan pembaruan = SHA-256 exe terpasang vs rilis, bukan nomor versi. Pergantian gagal (tidak ada `helper.ok` baru dalam 30 dtk) otomatis dipulihkan.
- Ganti nama Windows: server menyimpan tabel `renames` (pending -> applied|failed), menawarkannya lewat jawaban `/api/agent/release` (`"rename"`), helper memanggil `winapi.rename_computer` dan melapor ke `/api/agent/rename-result`. Saat PC muncul dengan nama baru, `Store.migrate_renamed` memindahkan alias, override, event, dan notice dari entri lama.
- Catatan helper (unduh/pasang rilis gagal, ganti nama) ditulis ke `helper.json` (`notes`) dan dilaporkan agent sebagai peristiwa "Pembantu: ..." di log PC (`core._report_helper_notes`); galat yang sama diredam 10 menit.
- Unggahan dashboard (rilis dan folder bersama) memakai sesi per potongan `/api/uploads*` (`labserver/uploads.py`, potongan di `<folder db>/uploads/`, kedaluwarsa 24 jam; klien `chunkedUpload` di `static/lab.js` melanjutkan dari `received` server setelah putus). `POST /api/release` dan `POST /api/shared/file` satu permintaan tetap ada untuk skrip.
- Unggah rilis dari dashboard lewat jalur internet (`lab.fog.tf`) bisa gagal bila uplink server lambat (terukur 2026-10-06: 1 MB > 40 dtk); unggah lewat alamat LAN. Menu "Semua PC" di dashboard: waktu tertentu lewat prompt, `POST /api/all/command` (restart/shutdown, hanya PC online).
- Agent tanpa helper (mis. belum dipasang ulang) mencoba menulis registry langsung; pada PC dengan kunci Policies milik SYSTEM itu gagal.

## Screenshot dan agent lewat tunnel
- Screenshot: interval di settings `screenshot_interval` (bawaan 10, 0 = mati; pilihan 5 dtk sampai 5 mnt), dikirim ke agent lewat jawaban poll. Agent (`core.Agent.screenshot_once`, utas sendiri) hanya mengirim saat tidak terkunci dan terhubung; `winapi.WindowsPlatform.screenshot` memakai Pillow. Server menyimpan satu JPEG per PC di `<folder db>/screenshots/` dan `pcs.shot_ts`; `pc_view["shot"]` kosong saat terkunci/offline.
- Agent lewat Cloudflare: `block_agent_via_tunnel = false` di `config.toml` server ini (atas permintaan pemilik, 2026-10-01); `lab.fog.tf` -> 8080 ada di `WEBSITE/fog/tunnel/config.yaml`. Semua permintaan agent wajib memakai `core.agent_headers` (User-Agent `LabAgent/x`), karena Cloudflare menolak `Python-urllib` dengan error 1010.
- IP di dashboard = `lan_ip` yang dilaporkan agent; tanpa itu, alamat koneksi (`Cf-Connecting-Ip` bila lewat tunnel).
- Uji tampilan: Chromium Playwright di `~/.cache/ms-playwright` butuh `LD_LIBRARY_PATH=~/.local/opt/drawio/libs/usr/lib/x86_64-linux-gnu`.

## Folder bersama
- Server: `labserver/shared.py` (validasi nama, `SharedFiles` = file di `<folder db>/shared/files/` + tabel `shared_files`; **tanpa batas ukuran/jumlah file** atas permintaan pemilik, hanya penolakan bila disk sisa < `MIN_FREE_BYTES` 2 GB; `request.max_content_length = NO_LIMIT` karena `None` di Flask berarti memakai `MAX_CONTENT_LENGTH`; semua data server termasuk `shared/` ada di HDD `/dev/sda1`, bukan NVMe), endpoint agent `/api/agent/shared/{index,file}` (token, **selalu 404 bila lewat Cloudflare**, izin per aturan PC: index = receive/send/both, unduh = receive/both, unggah/hapus = send/both), admin `/api/shared*` dan halaman `/pembagian`. Aturan per PC di kolom `pcs.shared_mode` (NULL = bawaan di settings `shared`); poll membawa `shared: {mode, path, interval}`; status sinkron agent ada di memori (`shared_status`).
- Agent: `labagent/sharedsync.py` (`decide` = tabel keputusan dari SHA lokal/server/base, `FolderSync`, `HttpSharedClient`, `is_local_url`), dipanggil `core.Agent.shared_once` di utas `run_shared`. `base` (SHA terakhir tersinkron) disimpan di `state_dir\shared.json`; hapus = file ada di base tetapi hilang di satu sisi. Aturan nama di agent dan server harus sama (dicek `test_path_rules_match_agent`).
- Folder bersama dihapus utuh di satu PC = tidak punya folder: `FolderSync.run` mengosongkan `base`, folder dibuat ulang dan file diunduh lagi, tanpa hapus apa pun (diuji untuk receive/both/send). Hapus subfolder/file tetap hapus biasa.
- Hapus di mode kirim/dua arah menghapus di server; mode terima tidak. Tidak ada tempat sampah (sengaja, sesuai permintaan pemilik).
- Belum dicoba di Windows: `WindowsPlatform.profile_dir` (`USERPROFILE`), penggantian file yang sedang dibuka aplikasi lain, dan junction. Uji di dua PC: aturan berbeda, ubah/hapus file, matikan jaringan lalu nyalakan.

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
- Jangan meng-commit `config.toml`, password dashboard, `agent_token`, atau nama file pemulihan. Menampilkannya di chat boleh bila pemilik meminta.
- Jangan mengubah `agent_token` atau `block_agent_via_tunnel` tanpa persetujuan; `/api/agent` sekarang terbuka lewat tunnel dan hanya dilindungi token.
- Kode Windows tidak bisa diuji di Linux; jangan menyatakan berhasil sebelum dicoba di satu PC Windows.

## Belum selesai
- Pemulihan darurat: logika dan tes selesai (`tests/test_killswitch.py`), tetapi `winapi.list_drives`, `list_names`, `remove_agent`, mode `--watch-usb`, dan tugas SYSTEM di `install.ps1` **belum dicoba di Windows**. Uji di satu PC: pasang dengan `-KillFileName`, colok drive berisi file itu, pastikan agent hilang. Cek juga pemasangan ulang oleh watchdog tidak terjadi.
- `labagent.exe` perlu dibangun ulang (`build.ps1`) dan dipasang ulang agar mode `--watch-usb` ada.
- Profil user yang **tidak sedang login** tidak dibersihkan dari kebijakan registry saat pencopotan (sama dengan `uninstall.ps1`).
- Pembaruan otomatis + helper: logika dan endpoint server sudah diuji (`tests/test_helper.py`, `test_updater.py`, `server/tests/test_release.py`, plus uji ujung-ke-ujung helper terhadap server), tetapi `winapi.apply_user_policies`, `launch_update` (tugas `LabAgentUpdate`), `current_sid`, `own_exe_sha256`, `apply-update.cmd`, dan tugas `LabAgentHelper` **belum dicoba di Windows**. Uji di satu PC: build, `install.cmd`, cek overlay bisa mengunci tanpa "Kebijakan registry gagal", lalu unggah rilis dengan versi berbeda dan pastikan exe berganti, `helper.ok` berisi SHA baru, dan rollback berjalan bila exe rusak. Curigai: apakah `schtasks /TR "\"path\""` menerima path dengan spasi, dan apakah skrip `.cmd` dari tugas SYSTEM sekali-jalan tetap hidup setelah `taskkill labagent.exe`.
- Ganti nama Windows: logika server dan helper sudah diuji, tetapi `winapi.rename_computer` (`SetComputerNameExW` sebagai SYSTEM) dan perpindahan entri setelah restart **belum dicoba di Windows**. Uji di satu PC: ganti nama, restart, pastikan `hostname` berubah dan entri lama hilang dengan alias/log pindah.
- Screenshot: logika agent dan server sudah diuji (termasuk ujung-ke-ujung `core.Agent` -> server dan lewat `lab.fog.tf`), tetapi `winapi.WindowsPlatform.screenshot` (Pillow `ImageGrab`) dan build PyInstaller dengan Pillow **belum dicoba di Windows**. Uji di satu PC: build, pasang, pastikan pratinjau muncul di dashboard dalam sekitar 10 detik dan hilang saat PC dikunci.
- Agent lewat tunnel belum dicoba dari PC Windows sungguhan (HTTPS dari exe PyInstaller): pasang satu PC dengan `-ServerUrl https://lab.fog.tf`.
- Belum ada agent terpasang (versi saat ini 1.2.0; semua PC lama dihapus dari server 2026-10-04). Pasang tiap PC sekali dengan `build.cmd` lalu `install.cmd`; baru setelah itu pembaruan otomatis.
- Uji Windows umum yang belum tercatat: overlay di semua monitor, keyboard terblokir, layar Ctrl+Alt+Del, tombol shutdown.
