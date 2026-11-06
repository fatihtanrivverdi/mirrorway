# Mirrorway — automatic installation on Windows
# Run by install-windows.bat. No administrator rights needed (except for the optional firewall step).
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$src    = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest   = Join-Path $env:LOCALAPPDATA 'Mirrorway'
$cfgDir = Join-Path $env:USERPROFILE '.mirrorway'
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Step($msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host ''
    Write-Host "ERROR: $msg" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}

Write-Host '==============================================' -ForegroundColor Cyan
Write-Host '   Mirrorway — Windows installation' -ForegroundColor Cyan
Write-Host '==============================================' -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $src 'mirrorway\win\app.py'))) {
    Fail "The installation files were not found. Right-click the zip file and choose 'Extract All', then run install-windows.bat from the extracted folder."
}

# ---------------------------------------------------------------------------
# 1) Python
# ---------------------------------------------------------------------------
function Test-Python([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $lines = @($out)
            if ($lines.Count -ge 2 -and ([version]$lines[0]) -ge ([version]'3.9') -and ($lines[1] -notmatch 'WindowsApps')) {
                return $lines[1].Trim()
            }
        }
    } catch {}
    return $null
}

function Find-Python {
    $cands = @(
        @{ exe = 'py';      pre = @('-3') },
        @{ exe = 'python';  pre = @() },
        @{ exe = 'python3'; pre = @() }
    )
    foreach ($c in $cands) {
        $cmd = Get-Command $c.exe -ErrorAction SilentlyContinue
        if ($cmd -and ($cmd.Source -notmatch 'WindowsApps')) {
            $p = Test-Python $c.exe $c.pre
            if ($p) { return $p }
        }
    }
    $found = Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending
    foreach ($f in $found) {
        $p = Test-Python $f.FullName @()
        if ($p) { return $p }
    }
    return $null
}

Step 'Checking for Python…'
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Warn 'Python was not found; installing it with winget (1-3 minutes)…'
        & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $py = Find-Python
    }
    if (-not $py) {
        Warn 'Downloading Python from python.org…'
        $arch = 'amd64'
        if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $arch = 'arm64' }
        elseif (-not [Environment]::Is64BitOperatingSystem) { $arch = $null }
        $name = if ($arch) { "python-3.12.10-$arch.exe" } else { 'python-3.12.10.exe' }
        $installer = Join-Path $env:TEMP $name
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.12.10/$name" -OutFile $installer
        } catch { Fail "Could not download Python: $($_.Exception.Message)" }
        Warn 'Installing Python…'
        Start-Process -FilePath $installer -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0' -Wait
        Remove-Item $installer -ErrorAction SilentlyContinue
        $py = Find-Python
    }
    if (-not $py) { Fail 'Could not install Python. Install it by hand from https://www.python.org/downloads/ and run install-windows.bat again.' }
}
Ok "Python: $py"

# ---------------------------------------------------------------------------
# 2) Files (and moving over from Köprü, the app's former name)
# ---------------------------------------------------------------------------
Step "Copying the files → $dest"
# Close a running Mirrorway (or the old Köprü)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like '*mirrorway.win*' -or $_.CommandLine -like '*kopru.win*') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
# Köprü's shortcuts and program folder go; its settings and pairing (in .kopru) are imported on first start
foreach ($folder in @('Programs', 'Desktop', 'Startup')) {
    try {
        $dir = [Environment]::GetFolderPath($folder)
        foreach ($n in @('Köprü.lnk', 'Kopru.lnk')) {
            $old = Join-Path $dir $n
            if ($dir -and (Test-Path $old)) { Remove-Item -Force $old -ErrorAction SilentlyContinue }
        }
    } catch {}
}
$oldDest = Join-Path $env:LOCALAPPDATA 'Kopru'
if (Test-Path $oldDest) {
    Remove-Item -Recurse -Force $oldDest -ErrorAction SilentlyContinue
    Ok 'Removed the old Köprü installation'
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
if (Test-Path "$dest\mirrorway") { Remove-Item -Recurse -Force "$dest\mirrorway" }
Copy-Item -Recurse -Force (Join-Path $src 'mirrorway') (Join-Path $dest 'mirrorway')
Copy-Item -Force (Join-Path $src 'requirements-windows.txt') $dest
if (Test-Path (Join-Path $src 'CHANGELOG.md')) { Copy-Item -Force (Join-Path $src 'CHANGELOG.md') $dest }
Get-ChildItem -Recurse -File $dest -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Ok 'Done'

# ---------------------------------------------------------------------------
# 3) Virtual environment + packages
# ---------------------------------------------------------------------------
$venvPy = Join-Path $dest '.venv\Scripts\python.exe'
$pyw    = Join-Path $dest '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $venvPy)) {
    Step 'Creating the virtual environment…'
    & $py -m venv (Join-Path $dest '.venv') | Out-Host
    if (-not (Test-Path $venvPy)) { Fail 'Could not create the virtual environment.' }
}
Step 'Installing the required packages (pinned versions, verified by hash)…'
& $venvPy -m pip install --disable-pip-version-check -q --upgrade pip | Out-Host
& $venvPy -m pip install --disable-pip-version-check -r (Join-Path $dest 'requirements-windows.txt') | Out-Host
if ($LASTEXITCODE -ne 0) { Fail 'Could not install the packages. Check the internet connection and try again.' }
& $venvPy -c "import PIL, pystray, pyperclip, cryptography" 2>$null
if ($LASTEXITCODE -ne 0) { Fail 'The packages were installed but could not be loaded.' }
Ok 'Done'

# ---------------------------------------------------------------------------
# 4) Settings (an optional mirrorway-settings.json next to this script is merged in)
# ---------------------------------------------------------------------------
Step 'Writing the settings…'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
$cfgFile = Join-Path $cfgDir 'windows.json'
$cfg = [ordered]@{}
if (Test-Path $cfgFile) {
    try {
        (Get-Content -Raw -Encoding UTF8 $cfgFile | ConvertFrom-Json).PSObject.Properties |
            ForEach-Object { $cfg[$_.Name] = $_.Value }
    } catch {}
}
$presetFile = Join-Path $src 'mirrorway-settings.json'
if (Test-Path $presetFile) {
    (Get-Content -Raw -Encoding UTF8 $presetFile | ConvertFrom-Json).PSObject.Properties |
        ForEach-Object { $cfg[$_.Name] = $_.Value }
    Ok 'Settings taken from mirrorway-settings.json'
}
if ($cfg.Count -gt 0) {
    [System.IO.File]::WriteAllText($cfgFile, ($cfg | ConvertTo-Json), $utf8NoBom)
}

$dbg = "@echo off`r`ncd /d `"%~dp0`"`r`n`".venv\Scripts\python.exe`" -m mirrorway.win --debug`r`npause`r`n"
[System.IO.File]::WriteAllText((Join-Path $dest 'debug.bat'), $dbg, [System.Text.Encoding]::ASCII)

# ---------------------------------------------------------------------------
# 5) Shortcuts + starting with Windows
# ---------------------------------------------------------------------------
Step 'Creating shortcuts (Start menu, desktop, Windows startup)…'
$ws = New-Object -ComObject WScript.Shell
function New-MirrorwayShortcut([string]$path) {
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $pyw
    $s.Arguments = '-m mirrorway.win'
    $s.WorkingDirectory = $dest
    $s.Description = "Mirrorway: use the Mac's keyboard and mouse here and see the Mac's screen"
    $ico = Join-Path $dest 'mirrorway\assets\mirrorway.ico'
    if (Test-Path $ico) { $s.IconLocation = $ico }
    $s.Save()
}
$made = 0
foreach ($folder in @('Programs', 'Desktop', 'Startup')) {
    try {
        $dir = [Environment]::GetFolderPath($folder)
        if ($dir) { New-MirrorwayShortcut (Join-Path $dir 'Mirrorway.lnk'); $made++ }
    } catch { Warn "Could not create a shortcut ($folder): $($_.Exception.Message)" }
}
if ($made -gt 0) { Ok 'Done (Mirrorway also starts by itself whenever Windows starts)' }

# ---------------------------------------------------------------------------
# 6) Firewall (so that the Mac is found automatically; optional)
#    The rule is bound to the port, not the program: on Windows the venv python starts the real interpreter as a
#    separate process.
# ---------------------------------------------------------------------------
Step 'Adding a firewall rule…'
Warn "If Windows asks 'Do you want to allow this app to make changes to your device?', answer YES."
$fw = @"
Remove-NetFirewallRule -DisplayName 'Köprü otomatik bulma' -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName 'Mirrorway discovery' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'Mirrorway discovery' -Direction Inbound -Action Allow -Protocol UDP -LocalPort 24851 -Profile Any | Out-Null
"@
$enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($fw))
try {
    Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -ErrorAction Stop -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc"
    Ok 'Done'
} catch {
    Warn 'Skipped. No problem: the Mac is still found at its last known address.'
}

# ---------------------------------------------------------------------------
# 7) Start and check the connection
# ---------------------------------------------------------------------------
Step 'Starting Mirrorway…'
$log = Join-Path $cfgDir 'mirrorway-win.log'
$before = 0
if (Test-Path $log) { $before = @(Get-Content -Encoding UTF8 $log).Count }
Start-Process -FilePath $pyw -ArgumentList '-m', 'mirrorway.win' -WorkingDirectory $dest

$connected = $false
$lastErr = ''
for ($i = 0; $i -lt 25; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $log) {
        $new = @(Get-Content -Encoding UTF8 $log) | Select-Object -Skip $before
        if ($new | Where-Object { $_ -match 'mirrorway\.client: Connected to the Mac' }) { $connected = $true; break }
        $e = $new | Where-Object { $_ -match 'ERROR|Traceback' } | Select-Object -Last 1
        if ($e) { $lastErr = $e }
    }
}

Write-Host ''
if ($connected) {
    Write-Host '==============================================' -ForegroundColor Green
    Write-Host '  INSTALLATION COMPLETE — connected to the Mac!' -ForegroundColor Green
    Write-Host '==============================================' -ForegroundColor Green
    Write-Host ''
    Write-Host "  • Push the Mac's pointer against the right edge of its screen: it moves to this PC."
    Write-Host '    Shortcuts: Ctrl+Option+→ (to Windows), Ctrl+Option+← (back to the Mac)'
    Write-Host "  • To see the Mac's screen, click the Mirrorway icon next to the clock."
} else {
    Write-Host '==============================================' -ForegroundColor Yellow
    Write-Host '  Installed, but the connection to the Mac is not confirmed yet.' -ForegroundColor Yellow
    Write-Host '==============================================' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  • Is Mirrorway running on the Mac? (the Mirrorway icon in the menu bar)'
    Write-Host '  • Pair this PC: on the Mac choose Mirrorway › Pair a New Device… and enter the code in the Mirrorway settings here.'
    Write-Host '  • Are both computers on the same Wi-Fi/network? (if the Mac uses a VPN, try turning it off)'
    Write-Host "  • Right-click the Mirrorway icon next to the clock › Settings: enter the address shown as 'This Mac' in the Mac's menu."
    if ($lastErr) { Write-Host "  • Last error: $lastErr" -ForegroundColor DarkYellow }
    Write-Host "  • Detailed log: $log"
}
Write-Host ''
Read-Host 'Press Enter to close this window' | Out-Null
