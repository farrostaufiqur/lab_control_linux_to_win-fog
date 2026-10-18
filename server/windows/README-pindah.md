# Memindahkan Lab Control Server ke komputer Windows

Tujuan: PC lab yang sudah tersambung tetap tersambung tanpa dipasang ulang. Agent yang sudah terbangun memakai dua alamat, `http://192.168.1.17:8080` (LAN) dan `https://lab.fog.tf` (tunnel). Keduanya harus tetap menjawab dari server baru, dengan `agent_token`, password, dan database yang sama.

**Kode Windows di folder ini belum dicoba di Windows.** Uji di komputer tujuan sebelum pindah sungguhan.

## Prasyarat di komputer tujuan
1. IP **192.168.1.17** tetap (reservasi DHCP di router lebih aman daripada IP statis manual).
2. Python 3.12 (`winget install Python.Python.3.12`), terpasang untuk semua pengguna.
3. Komputer tidak tidur dan nyala terus (skrip memasang `powercfg` untuk itu).

## Uji coba (tanpa mengganggu PC lab)
Jalankan salinan di komputer tujuan dengan **port lain** supaya agent tidak berpindah ke server uji:
1. Di Linux (server tetap jalan, ini hanya membaca):
   `sqlite3 /home/albayan/WEBSITE/lab-control/server/labcontrol.db ".backup /tmp/labcontrol-uji.db"`
2. Salin folder `server` (tanpa `.venv`) ke `C:\LabControl\server`, taruh `config.toml` dan `labcontrol-uji.db` (ganti nama jadi `labcontrol.db`) di dalamnya.
3. Di `config.toml` ubah `port = 8081`, lalu jalankan `install-server.ps1 -Port 8081 -NamaTugas LabControlUji`.
4. Buka `http://192.168.1.17:8081`, pastikan login dan daftar PC tampil. Setelah selesai: `Unregister-ScheduledTask LabControlUji -Confirm:$false` dan kembalikan `port = 8080`.

## Pindah sungguhan (±5 menit, lakukan di luar jam lab)
Urutannya penting: server lama baru dimatikan setelah paketnya diambil, dan autostart Linux baru dimatikan setelah server baru terbukti jalan.

1. **Hentikan server lama** (PC lab jadi offline sebentar; agent mencoba lagi tiap 5 detik):
   `sudo systemctl stop lab-control`
2. **Buat paket** (data dibaca setelah server berhenti, jadi database konsisten):
   ```
   cd /home/albayan/WEBSITE/lab-control
   tar czf /tmp/lab-server.tar.gz --exclude=.venv --exclude=__pycache__ --exclude=cloudflared \
       --exclude=tests server
   ```
   Paket berisi `config.toml` (token dan password): jangan dikirim lewat saluran terbuka dan hapus setelah dipakai.
3. **Di Windows**: salin ke `C:\LabControl`, lalu `tar xzf lab-server.tar.gz`. Pastikan `server\config.toml` dan `server\labcontrol.db` ada, lalu jalankan sebagai Administrator:
   `powershell -ExecutionPolicy Bypass -File C:\LabControl\server\windows\install-server.ps1`
4. **Cek**: dari PC lain buka `http://192.168.1.17:8080`. Login memakai password lama (cookie sesi lama juga masih berlaku). Dalam sekitar 20 detik PC lab harus kembali online dengan nama, alias, dan jadwal yang sama.
5. **Arahkan tunnel** supaya `lab.fog.tf` menuju server baru: di `WEBSITE/fog/tunnel/config.yaml` ubah
   `service: http://127.0.0.1:8080` pada `hostname: lab.fog.tf` menjadi `service: http://192.168.1.17:8080`,
   lalu `sudo systemctl restart cloudflared-fog` dan cek `curl https://lab.fog.tf/healthz`.
   Catatan: jalur ini tetap lewat tunnel di server Linux, jadi `lab.fog.tf` hanya hidup selama server Linux dan tunnelnya nyala. Alternatifnya memasang `cloudflared` di Windows.
6. **Matikan autostart Linux** hanya setelah langkah 4 dan 5 lolos: `sudo systemctl disable lab-control`.

## Kalau gagal (kembali ke Linux)
Hentikan tugas di Windows (`Stop-ScheduledTask LabControlServer`), kembalikan `service` tunnel ke `http://127.0.0.1:8080`, restart `cloudflared-fog`, lalu `sudo systemctl enable --now lab-control`. Perubahan yang terjadi di server Windows sesudah pindah (jadwal, alias, rilis, berkas bersama) tidak ikut kembali kecuali disalin balik.

## Yang tidak berubah
`agent_token`, password dashboard, `session_secret`, tabel `pcs`/`events`/`settings`/`renames`, rilis agent, folder bersama, dan screenshot ikut dalam paket. Tombol "restart server" di dashboard tidak berfungsi di Windows (butuh systemd, jawabannya 409); restart dengan `Stop-ScheduledTask LabControlServer` lalu `Start-ScheduledTask LabControlServer`.
