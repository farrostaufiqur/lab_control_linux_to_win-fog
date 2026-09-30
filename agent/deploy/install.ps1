#Requires -RunAsAdministrator
<#
  Memasang agent di PC client. Jalankan sebagai Administrator di setiap PC lab:
    .\install.ps1 -ServerUrl http://192.168.1.10:8080 -Token <agent_token>
#>
param(
    [Parameter(Mandatory)][string]$ServerUrl,
    [Parameter(Mandatory)][string]$Token,
    [string]$Exe = "$PSScriptRoot\labagent.exe",
    [string[]]$ExemptUsers = @("administrator", "admin", "labadmin"),
    [ValidateSet("lock", "unlock")][string]$OfflineBootPolicy = "lock",
    [string]$KillFileName = ""    # opsional: nama file rahasia untuk pemulihan darurat (lihat README)
)
$ErrorActionPreference = "Stop"
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
