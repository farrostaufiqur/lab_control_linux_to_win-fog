#Requires -RunAsAdministrator
<#
  Memasang agent di PC client. Cara termudah: klik dua kali install.cmd (meminta hak Administrator sendiri).
  Atau dari PowerShell Administrator:
    .\install.ps1 -ServerUrl http://192.168.1.10:8080 -Token <agent_token>
  Parameter yang tidak diberikan dibaca dari install.conf di folder ini (baris Kunci=Nilai; -Config <berkas> untuk
  berkas lain, mis. install-vm.cmd memakai install-vm.conf; ExemptUsers=nama1,nama2 mengecualikan user dari penguncian). Bila ServerUrl/Token
  tetap kosong, skrip menanyakannya lalu menyimpannya ke install.conf supaya PC berikutnya tinggal klik.
#>
param(
    [string]$ServerUrl = "",
    [string]$Token = "",
    [string]$Exe = "$PSScriptRoot\labagent.exe",
    [string[]]$ExemptUsers = @("administrator", "admin", "labadmin"),
    [ValidateSet("lock", "unlock")][string]$OfflineBootPolicy = "lock",
    [string]$KillFileName = "",   # opsional: nama file rahasia untuk pemulihan darurat (lihat README)
    [string]$Config = ""          # opsional: berkas pengaturan selain install.conf
)
$ErrorActionPreference = "Stop"

# install.conf: berisi token, jangan dibagikan atau di-commit
$confPath = if ($Config) { $Config } else { Join-Path $PSScriptRoot "install.conf" }
$conf = @{}
if (Test-Path $confPath) {
    foreach ($line in Get-Content $confPath) {
        if ($line -match '^\s*([A-Za-z]+)\s*=\s*(.*?)\s*$') { $conf[$Matches[1]] = $Matches[2] }
    }
}
if (-not $ServerUrl)       { $ServerUrl = [string]$conf["ServerUrl"] }
if (-not $Token)           { $Token = [string]$conf["Token"] }
if (-not $KillFileName)    { $KillFileName = [string]$conf["KillFileName"] }
if (-not $PSBoundParameters.ContainsKey("ExemptUsers") -and $conf["ExemptUsers"]) {
    $ExemptUsers = @($conf["ExemptUsers"] -split '\s*,\s*' | Where-Object { $_ })
}
if (-not $PSBoundParameters.ContainsKey("OfflineBootPolicy") -and $conf["OfflineBootPolicy"] -in "lock", "unlock") {
    $OfflineBootPolicy = $conf["OfflineBootPolicy"]
}
if (-not $ServerUrl -or -not $Token) {
    Write-Host "Pengaturan belum ada. Isi sekali; disimpan ke install.conf untuk PC berikutnya."
    if (-not $ServerUrl) { $ServerUrl = (Read-Host "Alamat server (mis. http://192.168.1.10:8080 atau https://lab.contoh.com)").Trim() }
    if (-not $Token)     { $Token = (Read-Host "Token agent (agent_token di config.toml server)").Trim() }
    if (-not $KillFileName)    { $KillFileName = (Read-Host "Nama file pemulihan darurat (Enter = tidak dipakai)").Trim() }
    @("ServerUrl=$ServerUrl", "Token=$Token", "KillFileName=$KillFileName", "ExemptUsers=$($ExemptUsers -join ',')",
      "OfflineBootPolicy=$OfflineBootPolicy") | Set-Content -Path $confPath -Encoding ASCII
}
if ($ServerUrl -notmatch '^https?://') { throw "ServerUrl harus diawali http:// atau https://" }
if (-not $Token) { throw "Token wajib diisi" }

if (-not (Test-Path $Exe)) { throw "labagent.exe tidak ditemukan di $Exe (jalankan build.ps1 dulu)" }

$dir  = Join-Path $env:ProgramFiles "LabAgent"     # hanya admin yang bisa menulis
$data = Join-Path $env:ProgramData  "LabAgent"     # folder status, boleh ditulis user
New-Item -ItemType Directory -Force $dir, $data | Out-Null

Get-Process labagent -ErrorAction SilentlyContinue | Stop-Process -Force
Copy-Item $Exe "$dir\labagent.exe" -Force

$config = [ordered]@{
    server_url          = $ServerUrl
    token               = $Token
    offline_boot_policy = $OfflineBootPolicy
    exempt_users        = $ExemptUsers
    state_dir           = $data
}
if ($KillFileName) {
    # hanya hash-nya yang disimpan; sama dengan killswitch.name_hash (trim + huruf kecil)
    $bytes = [Text.Encoding]::UTF8.GetBytes($KillFileName.Trim().ToLowerInvariant())
    $config["kill_file_sha256"] = ([Security.Cryptography.SHA256]::Create().ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
}
$config | ConvertTo-Json | Set-Content -Path "$dir\config.json" -Encoding UTF8
icacls $data /grant "BUILTIN\Users:(OI)(CI)M" | Out-Null

# Cek apakah server terjangkau dari PC ini
try {
    Invoke-RestMethod -Uri ($ServerUrl.TrimEnd("/") + "/healthz") -TimeoutSec 5 | Out-Null
    Write-Host "Server terjangkau: $ServerUrl"
} catch {
    Write-Warning "Server TIDAK terjangkau dari PC ini ($ServerUrl). Cek alamat IP, port 8080 di firewall server, dan kabel/jaringan. Agent tetap dipasang."
}

# Sembunyikan "Switch user" di layar Ctrl+Alt+Del
$sys = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
New-ItemProperty -Path $sys -Name HideFastUserSwitching -Value 1 -PropertyType DWord -Force | Out-Null

# Jalan saat login siapa pun, dan dicek ulang tiap menit (watchdog; instance ganda otomatis keluar)
$action    = New-ScheduledTaskAction -Execute "$dir\labagent.exe"
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$repeat    = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
$trigger.Repetition = $repeat.Repetition
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Users" -RunLevel Limited
# Priority 4 (normal): bawaan Task Scheduler adalah 7 (di bawah normal), yang memperlambat agent saat logon ramai
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 4 `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "LabAgent" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

# Pembantu SYSTEM: menulis kebijakan registry Ctrl+Alt+Del (user biasa tidak bisa) dan memasang rilis baru agent
$helperAction    = New-ScheduledTaskAction -Execute "$dir\labagent.exe" -Argument "--helper"
$helperTrigger   = New-ScheduledTaskTrigger -AtStartup
$helperPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$helperSettings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 4 `
                   -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "LabAgentHelper" -Action $helperAction -Trigger $helperTrigger `
    -Principal $helperPrincipal -Settings $helperSettings -Force | Out-Null
Start-ScheduledTask -TaskName "LabAgentHelper"
Write-Host "Pembaruan otomatis aktif (rilis diunggah admin lewat dashboard)."

# Pemulihan darurat: proses SYSTEM yang membaca nama file tingkat pertama tiap drive yang terpasang
if ($KillFileName) {
    $watchAction    = New-ScheduledTaskAction -Execute "$dir\labagent.exe" -Argument "--watch-usb"
    $watchTrigger   = New-ScheduledTaskTrigger -AtStartup
    $watchPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $watchSettings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
                      -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName "LabAgentKillSwitch" -Action $watchAction -Trigger $watchTrigger `
        -Principal $watchPrincipal -Settings $watchSettings -Force | Out-Null
    Start-ScheduledTask -TaskName "LabAgentKillSwitch"
    Write-Host "Pemulihan darurat aktif."
}

Write-Host "Agent terpasang. Logout lalu login lagi (atau restart) agar aktif."
