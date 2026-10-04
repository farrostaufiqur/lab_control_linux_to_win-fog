# Bangun labagent.exe (di satu PC Windows yang punya Python 3.11+ dengan tkinter). Cara termudah: klik dua kali build.cmd.
# Bila agent\rilis.key ada, exe langsung ditandatangani (hasil: labagent.exe.release.json). Alamat server ditanam ke exe dari build.conf (satu baris ServerUrl=... per alamat, LAN dan/atau internet).
$ErrorActionPreference = "Stop"
$confPath = Join-Path $PSScriptRoot "build.conf"
$urls = @()
if (Test-Path $confPath) {
    foreach ($line in Get-Content $confPath) {
        if ($line -match '^\s*ServerUrl\s*=\s*(.*?)\s*$' -and $Matches[1]) { $urls += $Matches[1].TrimEnd("/") }
    }
}
if (-not $urls) {
    Write-Host "build.conf belum ada. Isi alamat server (Enter = lewati); disimpan untuk build berikutnya."
    foreach ($ask in "Alamat LAN (mis. http://192.168.1.17:8080)", "Alamat internet (mis. https://lab.fog.tf)") {
        $url = (Read-Host $ask).Trim().TrimEnd("/")
        if ($url) { $urls += $url }
    }
    if (-not $urls) { throw "Minimal satu alamat server" }
    $urls | ForEach-Object { "ServerUrl=$_" } | Set-Content -Path $confPath -Encoding ASCII
}
foreach ($url in $urls) {
    if ($url -notmatch '^https?://[A-Za-z0-9.:\-\[\]/]+$') { throw "Alamat server tidak valid: $url" }
}
Write-Host ("Alamat server: " + ($urls -join ", "))

Set-Location (Split-Path $PSScriptRoot -Parent)
"SERVER_URLS = (" + (($urls | ForEach-Object { '"' + $_ + '"' }) -join ", ") + ",)" |
    Set-Content -Path "labagent\_build.py" -Encoding ASCII
py -m pip install --upgrade pyinstaller pillow   # pillow: screenshot berkala
if ($LASTEXITCODE) { throw "pip gagal" }
py -m PyInstaller --noconsole --onefile --name labagent --hidden-import PIL.ImageGrab --hidden-import PIL.JpegImagePlugin run_agent.py
if ($LASTEXITCODE) { throw "PyInstaller gagal" }
Copy-Item dist\labagent.exe deploy\labagent.exe -Force
Write-Host "Selesai: deploy\labagent.exe"

# Tanda tangan rilis otomatis bila kunci privat ada di folder agent (dibuat sekali: py tools\release.py keygen rilis.key)
if (Test-Path "rilis.key") {
    $version = (py -c "from labagent import VERSION; print(VERSION)").Trim()
    py tools\release.py sign deploy\labagent.exe --key rilis.key --version $version
    if ($LASTEXITCODE) { throw "Penandatanganan gagal" }
    Write-Host "Siap diunggah di dashboard (Pembaruan agent): deploy\labagent.exe dan deploy\labagent.exe.release.json"
} else {
    Write-Warning "rilis.key tidak ada di folder agent: exe tidak ditandatangani, jadi tidak bisa dipakai untuk pembaruan lewat jaringan (pemasangan manual tetap bisa)."
}
