@echo off
rem Pasang agent Lab Control di VM Windows (pengaturan dari install-vm.conf): klik dua kali. Meminta hak Administrator sendiri (UAC).
cd /d "%~dp0"
fltmc >nul 2>&1
if not errorlevel 1 goto admin
echo Meminta hak Administrator...
powershell -NoProfile -Command "Start-Process -FilePath \"%~f0\" -Verb RunAs"
if not errorlevel 1 exit /b
echo Hak Administrator ditolak. Dibatalkan.
pause
exit /b

:admin
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -ConfigFile "%~dp0install-vm.conf"
if errorlevel 1 echo GAGAL. Baca pesan di atas.
echo.
pause
