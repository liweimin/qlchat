$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Browsers = Join-Path $env:LOCALAPPDATA "ms-playwright"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    $Python = "python"
}

if (-not (Test-Path $Browsers)) {
    & $Python -m playwright install chromium
}

& $Python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name qlchat-downloader `
    --distpath $Root `
    --workpath (Join-Path $Root "build\pyinstaller") `
    --specpath (Join-Path $Root "build\pyinstaller") `
    --add-data "$Browsers;ms-playwright" `
    (Join-Path $Root "tools\qlchat_downloader_gui.py")
