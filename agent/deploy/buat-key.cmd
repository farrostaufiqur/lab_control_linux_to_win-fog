@echo off
rem Buat kunci rilis (SEKALI SAJA). Hasil: agent\rilis.key (kunci privat) dan kunci publik yang dicetak di layar.
cd /d "%~dp0.."
if exist rilis.key (
    echo rilis.key sudah ada di %CD%. Tidak ditimpa.
    echo Kunci lama dipakai server dan semua PC; kunci baru berarti server dan semua PC harus diganti.
    echo.
    pause
    exit /b 1
)
py tools\release.py keygen rilis.key
if errorlevel 1 (
    echo.
    echo GAGAL. Baca pesan di atas.
    pause
    exit /b 1
)
echo.
echo File kunci privat : %CD%\rilis.key   ^(simpan aman, buat cadangan, JANGAN dibagikan atau di-commit^)
echo Kunci publik      : salin baris "Kunci publik" di atas ke
echo                      - update_public_key di server\config.toml (lalu restart server)
echo                      - UpdatePublicKey di deploy\install.conf
echo.
pause
