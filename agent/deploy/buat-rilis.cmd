@echo off
rem Tanda tangani deploy\labagent.exe menjadi rilis. Hasil: deploy\labagent.exe.release.json
rem Versi diambil dari agent\labagent\__init__.py. Jalankan SETELAH build.cmd (atau pakai build-rilis.cmd).
cd /d "%~dp0.."
if not exist rilis.key (
    echo rilis.key tidak ada di %CD%. Jalankan buat-key.cmd, atau salin rilis.key lama ke folder itu.
    goto fail
)
if not exist deploy\labagent.exe (
    echo deploy\labagent.exe tidak ada. Jalankan build.cmd dulu.
    goto fail
)
set "VER="
for /f "delims=" %%v in ('py -c "from labagent import VERSION; print(VERSION)"') do set "VER=%%v"
if not defined VER (
    echo Versi tidak terbaca dari labagent\__init__.py.
    goto fail
)
py tools\release.py sign deploy\labagent.exe --key rilis.key --version %VER%
if errorlevel 1 goto fail
echo.
echo Rilis v%VER% siap. Unggah KEDUA berkas ini di dashboard ^(Pembaruan agent^):
echo   %CD%\deploy\labagent.exe
echo   %CD%\deploy\labagent.exe.release.json
echo.
if /i not "%~1"=="nopause" pause
exit /b 0

:fail
echo.
echo GAGAL. Baca pesan di atas.
if /i not "%~1"=="nopause" pause
exit /b 1
