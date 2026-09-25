# setup.ps1: sets up LiveCams + PierTracker on this PC and starts it (run by "Set up and start LiveCams.bat").
#
# Safe to run any time: every step checks first and is skipped when it's already done. It never
# overwrites or deletes your data: a restore only fills in files that are missing, and the only thing
# it ever deletes is a broken tracker\.venv (a Python environment it can rebuild). After a Windows
# reset or on a new PC it installs what's missing (Python 3.12, .NET 8 SDK, Git, WebView2), rebuilds
# the tracker's Python environment and models, restores the data from the Google Drive backup, builds
# the app, and starts the cam wallpapers, the tracker and the tray icon.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1 [-Rebuild] [-Fresh]
#     -Rebuild  rebuild the app even if it's there
#     -Fresh    start with empty data even though a backup is configured but can't be reached

param([switch]$Rebuild, [switch]$Fresh)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$App = Join-Path $Root 'app\LiveCams.exe'
$Venv = Join-Path $Root 'tracker\.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Data = Join-Path $Root 'data'

function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "   $text" -ForegroundColor Green }
function Note($text) { Write-Host "   $text" -ForegroundColor Yellow }
function Have($command) { [bool](Get-Command $command -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Install-Package($id, $name) {
    if (-not (Have 'winget')) { throw "$name is missing and winget isn't available. Install $name, then run this again." }
    Write-Host "   installing $name..."
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw "Installing $name failed (winget exit code $LASTEXITCODE)." }
    Refresh-Path
}
function Run($what) {
    # Runs a native command (a script block) and stops on failure.
    & $what
    if ($LASTEXITCODE -ne 0) { throw "Failed (exit code $LASTEXITCODE): $what" }
}
function Stop-LiveCams {
    if ((Get-Process -Name LiveCams -ErrorAction SilentlyContinue) -and (Test-Path $App)) {
        Write-Host '   stopping LiveCams for the update...'
        & $App --quit
        for ($i = 0; $i -lt 30 -and (Get-Process -Name LiveCams -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
    }
}
function Test-Command([scriptblock]$command) {
    # True if the native command exits 0. Its output is discarded; in Windows PowerShell a warning on
    # stderr would otherwise count as an error.
    $ErrorActionPreference = 'Continue'
    try { & $command *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}

# The two settings this needs from livecams.json (it has // comments, so no JSON parser).
$config = Get-Content (Join-Path $Root 'livecams.json') -Raw
$backupDir = if ($config -match '"backupDir"\s*:\s*"([^"]+)"') { $Matches[1] -replace '/', '\' } else { $null }
$publish = $config -match '"publishResults"\s*:\s*true'

Step 'Tools'
if (Test-Command { py -3.12 -c 'import sys' }) { Done 'Python 3.12' } else { Install-Package 'Python.Python.3.12' 'Python 3.12' }
if ((Have 'dotnet') -and ((dotnet --list-sdks) -match '^8\.')) { Done '.NET 8 SDK' } else { Install-Package 'Microsoft.DotNet.SDK.8' '.NET 8 SDK' }
if (Have 'git') { Done 'Git' } else { Install-Package 'Git.Git' 'Git' }
$webview = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
if ((Test-Path "HKLM:\$webview") -or (Test-Path 'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')) {
    Done 'WebView2 runtime'
} else { Install-Package 'Microsoft.EdgeWebView2Runtime' 'WebView2 runtime' }

Step 'Your data'
if (Test-Path (Join-Path $Data 'piertracker.db')) {
    Done 'already here'
} elseif ($backupDir -and (Test-Path (Join-Path $backupDir 'piertracker.db'))) {
    Write-Host "   restoring from $backupDir..."
    New-Item -ItemType Directory -Force $Data | Out-Null
    # Only files that are missing here are copied; nothing on this PC is overwritten.
    foreach ($file in @(Get-Item (Join-Path $backupDir 'piertracker.db')) + @(Get-ChildItem $backupDir -Filter *.csv)) {
        $dest = Join-Path $Data $file.Name
        if (-not (Test-Path $dest)) { Copy-Item $file.FullName $dest }
    }
    foreach ($folder in 'review', 'frame_bank') {
        $from = Join-Path $backupDir $folder
        if (Test-Path $from) {
            # /XC /XN /XO: skip every file that already exists here, whatever its date or size
            robocopy $from (Join-Path $Data $folder) /E /XC /XN /XO /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "Restoring $folder failed (robocopy exit code $LASTEXITCODE)." }
        }
    }
    $global:LASTEXITCODE = 0
    Done 'restored: database, CSVs, review answers, frame bank'
} elseif ($backupDir -and -not $Fresh) {
    Note "The backup folder isn't reachable: $backupDir"
    Note 'Install Google Drive for desktop, sign in, wait until "My Drive" shows up, then run this again.'
    Note '(To start over with empty data instead, run: setup.ps1 -Fresh)'
    throw 'Stopped so a fresh start never replaces your backup.'
} else {
    Note 'no data yet: starting fresh'
}

Step 'Tracker (Python environment and models)'
$pythonRuns = (Test-Path $Py) -and (Test-Command { & $Py -c 'import sys' })
if ($pythonRuns -and (Test-Command { & $Py -c 'import openvino, numpy, PIL, sklearn, pandas, statsmodels' })) {
    Done 'Python environment'
} else {
    Stop-LiveCams
    if (-not $pythonRuns) {
        if (Test-Path $Venv) {
            # e.g. after a Windows reset: the Python it was made from is gone. Deleted only if it
            # really is the tracker's .venv folder.
            if ((Split-Path $Venv -Leaf) -ne '.venv' -or (Split-Path (Split-Path $Venv -Parent) -Leaf) -ne 'tracker') {
                throw "Refusing to delete $Venv"
            }
            Write-Host '   the old environment no longer runs; making a new one...'
            Remove-Item -Recurse -Force $Venv
        }
        Run { py -3.12 -m venv $Venv }
        Run { & $Py -m pip install --upgrade pip }
        # CPU-only PyTorch first so nothing pulls a CUDA build.
        Run { & $Py -m pip install torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cpu }
    }
    Run { & $Py -m pip install -r (Join-Path $Root 'tracker\requirements-setup.txt') -r (Join-Path $Root 'tracker\requirements-ml.txt') }
    Done 'Python environment'
}
$models = 'detector.xml', 'classifier.xml', 'classifier.json', 'label_embeddings.npy' | ForEach-Object { Join-Path $Root "tracker\models\$_" }
if ($models | Where-Object { -not (Test-Path $_) }) {
    Stop-LiveCams
    Write-Host '   downloading and converting the models (about 1.9 GB, a few minutes)...'
    Run { & $Py (Join-Path $Root 'tracker\setup_models.py') }
    Done 'models'
} elseif ((Get-Item (Join-Path $Root 'tracker\species.json')).LastWriteTime -gt (Get-Item $models[3]).LastWriteTime) {
    Write-Host '   the species list changed; updating the labels...'
    Run { & $Py (Join-Path $Root 'tracker\setup_models.py') --labels-only }
    Done 'models (labels updated)'
} else {
    Done 'models'
}

Step 'Wallpaper app'
if ($Rebuild -or -not (Test-Path $App)) {
    Stop-LiveCams
    Run { dotnet publish (Join-Path $Root 'host') -c Release -o (Join-Path $Root 'app') }
    Done 'built'
} else {
    Done 'already built (run with -Rebuild to rebuild)'
}

Step 'Starting'
$manager = Join-Path (Split-Path $Root -Parent) 'set-wallpaper-mode.ps1'
if (Get-Process -Name LiveCams -ErrorAction SilentlyContinue) {
    Done 'already running'
} elseif (Test-Path $manager) {
    & $manager -Mode livecams | Out-Null  # through the Wallpaper Manager, so it knows cams are on
    Done 'live cams on (via the Wallpaper Manager); they also start at every login'
} else {
    Start-Process -FilePath $App -ArgumentList '--on'
    Done 'live cams on; they also start at every login'
}

# Show the tray icon on the taskbar instead of under the ^ arrow. Windows lists it once it has run.
$icon = $null
for ($i = 0; $i -lt 30 -and -not $icon; $i++) {
    $icon = Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' -ErrorAction SilentlyContinue |
        Where-Object { (Get-ItemProperty $_.PSPath).ExecutablePath -eq $App } | Select-Object -First 1
    if (-not $icon) { Start-Sleep -Seconds 1 }
}
if ($icon) {
    Set-ItemProperty -Path $icon.PSPath -Name IsPromoted -Value 1 -Type DWord
    Done 'tray icon pinned to the taskbar'
} else {
    Note 'tray icon not listed yet; pin it in Settings > Personalization > Taskbar > Other system tray icons'
}

function Tracker-Running {
    [bool](Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*tracker.py*' })
}
for ($i = 0; $i -lt 60 -and -not (Tracker-Running); $i++) { Start-Sleep -Seconds 1 }
if (Tracker-Running) { Done 'tracker running' }
else { Note "tracker not running yet; see $(Join-Path $Root 'logs\tracker.log')" }

if ($publish) {
    Step 'GitHub (for the nightly results)'
    if (Test-Command { git -C $Root push --dry-run }) { Done 'signed in' }
    else { Note "Not signed in to GitHub. Run 'git push' once in $Root and sign in when asked." }
}

Write-Host ''
Write-Host 'All set.' -ForegroundColor Green
