<#
Pasang Lab Control Server di komputer Windows (jalankan sebagai Administrator, dari folder ini).

Prasyarat: folder `server` sudah berisi config.toml dan labcontrol.db dari server lama
(lihat README-pindah.md). Tanpa config.toml, run.py membuat token dan password BARU
dan semua agent di PC lab tidak akan tersambung, jadi skrip ini berhenti kalau berkas itu tidak ada.

  powershell -ExecutionPolicy Bypass -File .\install-server.ps1
  powershell -ExecutionPolicy Bypass -File .\install-server.ps1 -Baru     # server benar-benar baru
#>
#requires -RunAsAdministrator
param(
    [switch]$Baru,
    [int]$Port = 8080,
    [string]$NamaTugas = "LabControlServer"
)
$ErrorActionPreference = "Stop"
$Dir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Dir

if (-not $Baru) {
    foreach ($f in @("config.toml", "labcontrol.db")) {
        if (-not (Test-Path (Join-Path $Dir $f))) {
            throw "$f tidak ada di $Dir. Salin dari server lama dulu (README-pindah.md), atau pakai -Baru."
        }
    }
}

# 1. Python
$py = $null
foreach ($c in @("py", "python")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    throw "Python belum terpasang. Jalankan: winget install Python.Python.3.12  lalu buka PowerShell baru."
}
$pyArgs = if ($py -eq "py") { @("-3") } else { @() }

# 2. venv + dependensi
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $py @pyArgs -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

# 3. Firewall: hanya jaringan lokal (LAN dan alamat server tunnel di LAN)
$aturan = "Lab Control Server $Port"
if (-not (Get-NetFirewallRule -DisplayName $aturan -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $aturan -Direction Inbound -Protocol TCP -LocalPort $Port `
        -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
}

# 4. Komputer jangan tidur (server harus selalu nyala)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

# 5. Tugas terjadwal: jalan saat boot, SYSTEM, dinyalakan ulang kalau mati
$aksi = New-ScheduledTaskAction -Execute (Join-Path $Dir ".venv\Scripts\python.exe") `
    -Argument "run.py" -WorkingDirectory $Dir
$pemicu = New-ScheduledTaskTrigger -AtStartup
$pengaturan = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$akun = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $NamaTugas -Action $aksi -Trigger $pemicu -Settings $pengaturan `
    -Principal $akun -Force | Out-Null
Start-ScheduledTask -TaskName $NamaTugas

# 6. Cek
Start-Sleep -Seconds 5
try {
    $r = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 10
    Write-Host "OK: server menjawab $($r | ConvertTo-Json -Compress)"
} catch {
    Write-Warning "Server belum menjawab di http://127.0.0.1:$Port/healthz : $($_.Exception.Message)"
    exit 1
}
Write-Host "Alamat LAN komputer ini:"
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" } |
    ForEach-Object { "  http://$($_.IPAddress):$Port" }
