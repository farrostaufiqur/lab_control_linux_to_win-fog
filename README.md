# Lab Control

Server Linux memantau PC Windows di lab, mengunci layar di luar jadwal sesi, dan bisa mengunci PC tertentu dari dashboard web.

```
 PC Windows (labagent.exe)  ──HTTP tiap 5 dtk──►  Server Linux (Flask + SQLite)
   - lapor status, log, screenshot                   - dashboard web
   - overlay kunci layar                             - jadwal + kunci manual
   - jalankan shutdown/restart                       - log aktivitas
```

Agent yang menghubungi server (bukan sebaliknya), jadi Windows Firewall tidak perlu diubah.

## Yang dipantau
Online/offline, user login, aplikasi dan judul jendela aktif, idle, uptime, pemakaian RAM, status kunci, **screenshot berkala** (lihat bawah), serta log: agent mulai, ganti aplikasi, kunci/buka, tombol shutdown/restart, perintah admin, koneksi terputus. Log dihapus otomatis setelah 30 hari. Judul jendela bisa berisi info pribadi. Matikan lewat `"log_titles": false`, dan beri tahu pengguna lab bahwa aktivitas dan layar dicatat.

## Dashboard
Halaman terpisah dengan sidebar (tampilan dari template admin yang sama dengan admin fog.tf; mode terang/gelap):
- **Dashboard**: dua kelompok, Online dan Offline. Tiap PC satu kartu berisi nama, alamat IP, pratinjau screenshot (bila tidak terkunci), tombol pesan, dan tombol gembok (gembok tertutup = kunci, gembok terbuka = buka kunci; pilih durasinya). Klik kartu untuk membuka detail. Tombol **Semua PC** untuk mengunci/membuka/mengirim pesan ke semua.
- **Detail PC** (`/pc/<nama>`): screenshot besar, info lengkap, semua tombol (kunci, buka, ikut jadwal, pesan, nama tampilan, nama Windows, restart, matikan, hapus), dan log PC itu.
- **Jadwal sesi**, **Log aktivitas**, **Pembaruan agent**, **Pengaturan** (interval screenshot, restart server).

## Screenshot berkala
Selama PC **tidak terkunci**, agent (v1.2.0 ke atas) mengirim gambar monitor utama tiap 10 detik, sama dengan penyegaran log aktivitas (JPEG, lebar maks 1280). Interval diatur di halaman Pengaturan: mati, 5 dtk, 10 dtk, 30 dtk, 1 mnt, 5 mnt. Server hanya menyimpan **gambar terakhir** tiap PC (`screenshots/` di samping database); tidak ada riwayat. Mematikan fitur menghapus semua gambar. Gambar tidak ditampilkan saat PC terkunci atau offline. Build agent kini butuh Pillow (`build.ps1` memasangnya).

## Cara kunci bekerja
- Overlay layar penuh (semua monitor), selalu di atas, aplikasi di belakangnya tetap jalan.
- Semua tombol keyboard diblokir selama terkunci (Alt+Tab, tombol Windows, Alt+F4, dst.). Mouse hanya bisa mengakses overlay.
- Ctrl+Alt+Del **tidak bisa** diblokir program biasa. Sebagai gantinya agent mematikan isinya lewat kebijakan Windows: Task Manager, Lock, Sign out, dan Change password dinonaktifkan. Layar itu hanya bisa dibatalkan.
- Overlay menampilkan hitung mundur sampai kunci dibuka, plus tombol **Restart** dan **Matikan** (klik dua kali untuk konfirmasi).
- 5 menit sebelum sesi berakhir muncul peringatan merah di pojok layar.
- Timer ada di agent: kalau server mati, PC tetap terkunci/terbuka sesuai waktu yang sudah diketahui, lalu berpindah status tepat waktu.

Prioritas: **kunci per-PC** > **kunci semua PC** > **jadwal**. Kunci manual bisa diberi durasi (15 mnt ... tanpa batas), lalu kembali ke lapisan di bawahnya.

## Pasang server (Linux)
```bash
sudo mkdir -p /opt/lab-control && sudo cp -r server /opt/lab-control/
cd /opt/lab-control/server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # pertama kali: membuat config.toml + menampilkan password & token
```
Catat **agent_token** dan password dashboard (tersimpan di `config.toml`). Ubah `timezone` dan `lab_name` di sana bila perlu. Dashboard: `http://IP-SERVER:8080` (user `admin`).
Agar jalan otomatis: salin `labcontrol.service` ke `/etc/systemd/system/`, buat user `labcontrol`, lalu `systemctl enable --now labcontrol`.
Beri IP tetap pada server (atau reservasi DHCP).

Dashboard memakai halaman login (form username + password, cookie sesi 12 jam, tombol keluar di pojok kanan atas) dan tidak terenkripsi bila diakses lewat HTTP LAN. Cukup untuk LAN tertutup. Untuk yang lebih aman, taruh di belakang nginx/Caddy dengan HTTPS.

## Bagaimana Linux dan Windows terhubung (jaringan lokal)
Server **tidak** menghubungi Windows. Agent di tiap PC yang menghubungi server tiap 5 detik lewat HTTP ke `http://IP-SERVER:8080`. Jadi syaratnya:
1. PC Windows dan server Linux ada di jaringan yang sama (atau saling bisa dijangkau).
2. Server punya IP tetap (atau reservasi DHCP), misalnya `192.168.1.10`.
3. Port 8080 di server terbuka untuk LAN. Contoh dengan ufw: `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`.
4. Windows tidak perlu diubah (koneksi keluar diizinkan secara default).

Cek dari PC Windows sebelum memasang: `curl http://192.168.1.10:8080/healthz` harus menjawab `{"ok":true}`. `install.ps1` juga melakukan cek ini.

Setelah server berjalan, `labagent.exe` dibuat sekali, lalu `install.ps1 -ServerUrl ... -Token ...` dijalankan di tiap PC, dan PC langsung muncul di dashboard. Tidak ada langkah lain, kecuali akun siswa standar dan cmd diblokir (lihat di bawah).

## Dashboard online lewat Cloudflare Tunnel
Tunnel membuat dashboard bisa dibuka dari mana saja tanpa membuka port di router. `cloudflared` berjalan di server Linux dan membuat koneksi keluar ke Cloudflare (HTTPS dari Cloudflare).

**Agent lewat tunnel atau LAN.** Secara bawaan server menolak `/api/agent/*` (404) untuk permintaan yang datang lewat Cloudflare (`block_agent_via_tunnel = true`). Supaya PC tidak harus satu LAN dengan server, setel `block_agent_via_tunnel = false` di `config.toml`, hapus aturan `path: ^/api/agent` dari `config.yml` cloudflared, lalu pasang agent dengan `-ServerUrl https://lab.contoh.com`. Syaratnya:
- Agent **v1.2.0 ke atas**. Agent lama ditolak Cloudflare (error 1010) karena memakai User-Agent bawaan Python.
- Hostname itu **tidak** boleh di belakang Cloudflare Access, atau jalur `/api/agent` harus dikecualikan; agent tidak bisa login Access.
- Satu-satunya pengaman jalur agent adalah `agent_token`; biarkan panjang dan acak (bawaan 32 karakter). Siapa pun yang memegang token bisa mendaftarkan PC palsu dan mengirim screenshot palsu, tetapi tidak bisa mengendalikan PC lain.
- PC yang sudah terpasang dengan alamat LAN tetap memakai alamat itu sampai dipasang ulang dengan `-ServerUrl` baru. Kedua jalur bisa dipakai bersamaan.

Langkah (butuh akun Cloudflare gratis dan domain di Cloudflare):
```bash
# 1. pasang cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel login
cloudflared tunnel create lab-control                      # mencatat UUID dan file credentials
cloudflared tunnel route dns lab-control lab.contoh.com
sudo mkdir -p /etc/cloudflared && sudo cp server/cloudflared/config.example.yml /etc/cloudflared/config.yml
# edit UUID dan hostname di config.yml, salin file credentials .json ke /etc/cloudflared/
sudo cloudflared service install
```
Buka `https://lab.contoh.com` dan login dengan password dashboard.

Tanpa domain, untuk uji sementara: `cloudflared tunnel --url http://localhost:8080` menghasilkan alamat acak `*.trycloudflare.com` yang berubah tiap dijalankan. Jalur agent di mode ini **tidak** diblokir oleh Cloudflare, hanya oleh server (pengaturan di atas).

**Keamanan dashboard online:** siapa pun yang tahu URL bisa mencoba login. Server memblokir sebuah IP selama 5 menit setelah 5 kali salah password. Untuk yang lebih kuat, pakai **Cloudflare Access** (Zero Trust, gratis sampai 50 user) di depan hostname itu supaya harus login lewat email/Google dulu, dan gunakan password dashboard yang panjang. Dashboard bisa mengunci dan mematikan semua PC, jadi perlakukan sebagai akses admin.

## Pasang agent (Windows)
1. Di **satu** PC Windows dengan Python 3.11+: `agent\deploy\build.ps1` → menghasilkan `deploy\labagent.exe`.
2. Di **setiap** PC lab, PowerShell sebagai Administrator:
   ```powershell
   .\install.ps1 -ServerUrl http://192.168.1.10:8080 -Token <agent_token>
   ```
3. Logout/login (atau restart). PC muncul di dashboard.

Hapus dengan `uninstall.ps1`.

**Sekali klik:** di folder `agent\deploy` ada `build.cmd`, `install.cmd`, dan `uninstall.cmd`. Klik dua kali; `install.cmd` dan `uninstall.cmd` meminta hak Administrator sendiri lewat UAC (tidak perlu "Run as administrator"), `build.cmd` tidak butuh hak itu. `install.cmd` membaca pengaturan dari `install.conf` di folder yang sama:
```
ServerUrl=http://192.168.1.10:8080
Token=<agent_token>
KillFileName=
OfflineBootPolicy=lock
```
Bila berkas itu belum ada, pemasangan pertama menanyakan isinya lalu menyimpannya, sehingga PC berikutnya (folder yang sama di flashdisk) tinggal klik. `install.conf` berisi token: jangan dibagikan atau di-commit.

### Syarat agar kunci sulit dilewati
- Akun siswa harus **akun standar**, bukan Administrator.
- Buat akun admin lab dengan nama di `exempt_users` (default: `administrator`, `admin`, `labadmin`). Agent tidak mengunci akun itu. Untuk masuk sebagai admin di PC yang terkunci, tekan **Restart** di overlay lalu login sebagai admin.
- Blokir `cmd`, PowerShell, dan `regedit` untuk akun siswa (Group Policy atau AppLocker). Tanpa itu, siswa yang membuka cmd bisa menghentikan agent.
- Agent berjalan di sesi user, bukan sebagai service. Watchdog memulai ulang agent tiap menit dan status kunci disimpan di disk, tetapi ini pengamanan tingkat lab, bukan pengaman terhadap pengguna yang punya hak admin.

### Kalau server tidak terjangkau
- Saat PC baru menyala dan server mati: default **terkunci** (`offline_boot_policy: "lock"`), supaya mencabut kabel LAN tidak membuka PC. Pilih `"unlock"` kalau lebih penting lab tetap bisa dipakai. Terbuka setelah `boot_grace_seconds`.
- Perintah shutdown/restart dari dashboard hanya berlaku 2 menit dan hanya dikirim sekali, jadi PC offline tidak akan tiba-tiba mati saat menyala lagi.
- Shutdown/restart memakai `/f` (paksa tutup aplikasi, data belum tersimpan hilang). Ubah dengan `"force_shutdown": false`.

## Pembaruan otomatis agent
Cukup unggah `labagent.exe` hasil build lewat dashboard, semua PC memperbarui agent sendiri; PC yang sedang mati memperbarui saat menyala. Tidak ada kunci, tanda tangan, atau berkas JSON: yang dibutuhkan hanya akun admin.

Tiap rilis:
1. Klik dua kali `agent\deploy\build.cmd` (hasil: `agent\deploy\labagent.exe`).
2. Di dashboard buka **Pembaruan agent**, pilih `labagent.exe`, isi versi bila mau (kosong = `build-<8 huruf SHA>`), klik **Unggah rilis**.
3. Pantau tabel "Versi agent per PC": status berubah dari "menunggu pembaruan" menjadi "terbaru".

Lewat skrip: `curl -u admin:... -H 'X-Lab-Admin: 1' -H 'Content-Type: application/octet-stream' --data-binary @labagent.exe "$LAB_URL/api/release?version=1.2.1"`.

Keamanan: siapa pun yang bisa login admin di server atau memegang `agent_token` di jaringan yang sama (LAN memakai HTTP) dapat menyebarkan program yang berjalan sebagai SYSTEM di semua PC lab. Jaga password admin dan token, dan pakai alamat `https://` (tunnel) bila memungkinkan. Agent hanya memeriksa unduhan utuh (ukuran, SHA-256, awalan `MZ`), bukan keasliannya.

Cara kerja: pembantu bertanya ke server tiap 60 detik; bila SHA-256 exe rilis berbeda dari exe terpasang, ia mengunduh (jeda acak 0-120 dtk agar PC tidak mengunduh serentak), memeriksa ukuran, SHA-256, tanda tangan, dan awalan `MZ`, lalu menjalankan skrip pergantian lewat tugas sekali-jalan `LabAgentUpdate`. Skrip menghentikan agent, menukar exe (yang lama jadi `labagent.bak`), menyalakan ulang, dan menunggu 30 detik agar pembantu baru menulis `helper.ok` berisi SHA-nya. Jika tidak muncul, exe lama dipulihkan dan rilis itu dicatat di `updater.json` supaya tidak dicoba terus. Keputusan pakai SHA-256, bukan nomor versi, jadi tidak ada loop karena salah label. Menghentikan distribusi tidak mengembalikan agent ke versi lama; unggah rilis yang lebih lama bila perlu.

Pembaruan berjalan lewat jalur yang sama dengan agent (LAN, atau tunnel bila diizinkan). PC yang masih memakai agent lama (sebelum v1.2.0) harus dipasang ulang sekali dengan `install.cmd`; setelah itu pembaruan otomatis.

## Folder bersama
Satu folder pusat di server disinkronkan ke folder di tiap komputer (halaman **Folder bersama** di dashboard). Dicek tiap 10 detik, **hanya lewat LAN**: server menolak (404) permintaan lewat Cloudflare, dan agent tidak menyinkronkan bila alamat servernya bukan LAN.
- **Folder tujuan** diisi di dashboard, relatif terhadap profil pengguna Windows (mis. `Desktop/shared` menjadi `C:\Users\<pengguna>\Desktop\shared`). Dibuat otomatis bila belum ada. Kosong = fitur mati. Agent tidak pernah menulis di luar profil pengguna.
- **Aturan tiap komputer** (atau satu aturan bawaan untuk semua):

| Aturan | Perilaku |
|---|---|
| Mati | tidak ada sinkron |
| Terima saja | mengikuti server. Mengubah atau menghapus file di komputer itu tidak berpengaruh ke server; file dikembalikan. File yang dihapus admin di server ikut terhapus di komputer. |
| Kirim saja | file baru/ubah dikirim ke server, dan file yang dihapus di komputer itu dihapus di server. Perubahan lain di server tidak diterima. |
| Dua arah | perubahan dan hapus di mana pun saling menyebar. Bila dua sisi mengubah file yang sama, waktu ubah terbaru menang; file yang diubah menang atas file yang dihapus. |

Contoh: komputer A dua arah, B terima saja, C kirim saja. File dari C muncul di server, lalu di A dan B. B menghapus file: tidak berefek. C atau A menghapus file: terhapus di server dan di komputer yang menerima.
- **Admin** mengunggah, mengunduh, dan menghapus file pusat dari halaman yang sama. Batas: 100 MB per file, 2 GB total, 2000 file, nama aman untuk Windows (tanpa `<>:"|?*\`, nama sistem seperti `con`, atau titik/spasi di akhir). `desktop.ini`, `Thumbs.db`, `~$*`, dan `*.labtmp` tidak disinkronkan.
- **Pengaman:** file yang baru diubah (kurang dari 2 detik) ditunda sampai selesai ditulis; folder yang hilang dipulihkan, bukan dianggap "semua file dihapus"; penghapusan massal ke server (lebih dari 5 file dan separuh isi sekaligus) ditahan dan dilaporkan; unduhan diverifikasi SHA-256; tautan/junction tidak diikuti. Tidak ada tempat sampah: file yang dihapus di server hilang.
- Status sinkron tiap komputer (OK / gagal) tampil di halaman itu, dan ringkasan perubahan masuk ke log PC.

## Nama PC
- Tombol **Nama**: nama tampilan di dashboard saja (tidak mengubah Windows).
- Tombol **Nama Windows**: mengganti nama komputer Windows sungguhan. Server menawarkan permintaan itu ke pembantu SYSTEM di PC (butuh agent v1.2.0 ke atas dengan pembantu berjalan), yang memanggil `SetComputerNameExW`. Nama baru berlaku **setelah PC restart** (bisa dipilih restart otomatis). Aturan nama: maks 15 huruf/angka/tanda `-`, tidak boleh angka semua, disimpan huruf besar.
- Setelah restart PC melapor dengan nama baru dan tampil sebagai entri baru; nama tampilan, override kunci, dan log entri lama dipindahkan otomatis. Bila gagal, dashboard menampilkan galatnya dan permintaan tidak diulang otomatis (kirim ulang dari tombol yang sama).
- PC yang tergabung domain (AD) tidak dianjurkan: nama lokal berbeda dari nama di domain.

## Pemulihan darurat (server rusak, PC terkunci)
Bila server tidak bisa dihidupkan dan PC lab tetap terkunci, agent bisa dicopot dengan mencolok drive (flashdisk, HDD eksternal) yang berisi **file dengan nama rahasia** di folder paling atas (tingkat pertama).

- Aktifkan saat pemasangan: `.\install.ps1 -ServerUrl ... -Token ... -KillFileName "234hv9fd2f2d23hsdfgd6.txt"`. Pakai nama panjang dan acak; isi file bebas (boleh kosong). Di PC hanya SHA-256 nama itu yang disimpan (`kill_file_sha256` di `config.json`).
- Proses `labagent.exe --watch-usb` (tugas `LabAgentKillSwitch`, SYSTEM) membaca tiap drive **sekali** saat terpasang, hanya daftar nama tingkat pertama, tanpa membuka isi file. Drive yang dicabut lalu dipasang lagi dibaca ulang.
- Bila ada file bernama sama (huruf besar/kecil tidak dibedakan), agent menghapus tugas terjadwal, menghentikan prosesnya, membersihkan kebijakan registry, lalu menghapus folder pemasangan dan dirinya sendiri.
- Tampilkan ekstensi file di Windows agar nama tidak jadi `nama.txt.txt`.
- Siapa pun yang tahu nama file itu bisa mencopot agent. Simpan seperti password.

## Memakai Claude dari komputer lain
Claude Code di komputer lain (misalnya PC di sebelah, atau laptop admin) bisa memasang dan mengoperasikan sistem ini.

1. Salin folder `lab-control` ke komputer itu (atau `git clone` bila sudah di repositori), lalu jalankan `claude` di dalam folder tersebut. Claude otomatis membaca `CLAUDE.md` yang berisi struktur, perintah uji, dan contoh API.
2. Beri akses ke server dengan variabel lingkungan, bukan menempelkan password di chat:
   ```bash
   export LAB_URL=http://192.168.1.10:8080     # atau https://lab.contoh.com dari luar jaringan
   export LAB_USER=admin LAB_PASS='password-dashboard'
   ```
   Komputer itu harus bisa menjangkau `LAB_URL` (satu LAN, atau lewat alamat Cloudflare). Kalau dashboard dilindungi Cloudflare Access, buat *service token* dan beri tahu Claude memakai header `CF-Access-Client-Id` dan `CF-Access-Client-Secret`.
3. Contoh perintah ke Claude: "Tampilkan PC yang online dan aplikasi yang sedang dibuka", "Kunci PC-05 selama 30 menit", "Ubah jadwal jadi Senin-Jumat 08:00-15:00".
4. Claude diminta konfirmasi sebelum tindakan yang berdampak (kunci/buka semua PC, shutdown, ubah jadwal).

Kalau Claude berjalan langsung di server Linux, cukup `LAB_URL=http://localhost:8080`. Untuk memasang agent di PC Windows, jalankan Claude di PC itu atau di komputer yang punya akses Administrator ke PC tersebut. `install.ps1` harus dijalankan di PC Windows dengan hak Administrator.

Password dashboard memberi kendali penuh atas lab. Beri hanya ke sesi Claude yang Anda percaya, dan ganti password di `config.toml` (lalu restart server) bila pernah terbagikan.

## Pengujian
```bash
cd server && python -m pytest      # jadwal, kunci, API, log, perintah, keamanan akses tunnel
cd agent  && python -m pytest      # logika agent dengan platform palsu
```
Bagian khusus Windows (`agent/labagent/winapi.py`, `overlay.py`) dan skrip PowerShell tidak bisa diuji di Linux. **Uji dulu di satu PC** sebelum dipasang ke seluruh lab, khususnya: overlay menutup semua monitor, keyboard terblokir, layar Ctrl+Alt+Del, dan tombol shutdown.
