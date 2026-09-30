#Requires -RunAsAdministrator
$ErrorActionPreference = "SilentlyContinue"
Unregister-ScheduledTask -TaskName "LabAgent" -Confirm:$false
Unregister-ScheduledTask -TaskName "LabAgentKillSwitch" -Confirm:$false
Unregister-ScheduledTask -TaskName "LabAgentHelper" -Confirm:$false
Unregister-ScheduledTask -TaskName "LabAgentUpdate" -Confirm:$false
Get-Process labagent | Stop-Process -Force
Remove-Item "$env:ProgramFiles\LabAgent" -Recurse -Force
Remove-Item "$env:ProgramData\LabAgent" -Recurse -Force
Remove-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name HideFastUserSwitching

# Bersihkan kebijakan yang dipasang agent di profil user yang sedang termuat
Get-ChildItem Registry::HKEY_USERS | Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' } | ForEach-Object {
    $root = "Registry::HKEY_USERS\$($_.PSChildName)\Software\Microsoft\Windows\CurrentVersion\Policies"
    foreach ($name in "DisableTaskMgr", "DisableLockWorkstation", "DisableChangePassword") {
        Remove-ItemProperty "$root\System" -Name $name
    }
    Remove-ItemProperty "$root\Explorer" -Name NoLogoff
}
Write-Host "Agent dihapus."
