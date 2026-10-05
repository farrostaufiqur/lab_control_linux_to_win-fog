@echo off
rem Buat kunci rilis (SEKALI SAJA). Hasil: agent\rilis.key (kunci privat) dan kunci publik yang dicetak di layar.
cd /d "%~dp0.."
if exist rilis.key (
    echo rilis.key sudah ada di %CD%. Tidak ditimpa.
    echo Untuk membuat kunci baru, hapus atau pindahkan rilis.key dulu.
    echo.
    pause
    exit /b 1
)
py tools\release.py keygen rilis.key > rilis.out.txt
set RC=%errorlevel%
type rilis.out.txt
if not "%RC%"=="0" (
    del rilis.out.txt
    echo.
    echo GAGAL. Baca pesan di atas.
    pause
    exit /b 1
)
findstr /b /c:"Kunci publik" rilis.out.txt > kunci-publik.txt
del rilis.out.txt
echo.
echo Kunci privat : %CD%\rilis.key   ^(simpan aman, buat cadangan, JANGAN dibagikan atau di-commit^)
echo Kunci publik : %CD%\kunci-publik.txt
echo Pasang kunci publik di:
echo   - update_public_key di server\config.toml (lalu restart server)
echo   - UpdatePublicKey di deploy\install.conf
echo.
pause
