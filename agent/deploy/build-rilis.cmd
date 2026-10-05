@echo off
rem Satu klik: build labagent.exe lalu tanda tangani. Naikkan VERSION di agent\labagent\__init__.py dulu.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
if errorlevel 1 (
    echo.
    echo BUILD GAGAL. Baca pesan di atas.
    pause
    exit /b 1
)
call "%~dp0buat-rilis.cmd" nopause
set RC=%errorlevel%
echo.
pause
exit /b %RC%
