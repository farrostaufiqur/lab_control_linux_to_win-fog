# Bangun labagent.exe (jalankan di satu PC Windows yang punya Python 3.11+ dengan tkinter).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
py -m pip install --upgrade pyinstaller pillow   # pillow: screenshot berkala
py -m PyInstaller --noconsole --onefile --name labagent --hidden-import PIL.ImageGrab --hidden-import PIL.JpegImagePlugin run_agent.py
Copy-Item dist\labagent.exe deploy\labagent.exe -Force
Write-Host "Selesai: deploy\labagent.exe"
