@echo off
rem Bangun labagent.exe: klik dua kali. Butuh Python 3.11+ (perintah "py"). Tidak butuh hak Administrator.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
if errorlevel 1 echo GAGAL. Baca pesan di atas.
echo.
pause
