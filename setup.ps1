#Requires -Version 5.1
<#
.SYNOPSIS
    Install the Elgato meeting light controller and register it as a startup task.
.PARAMETER Uninstall
    Remove the scheduled task instead of creating it.
.PARAMETER Brightness
    Light brightness 0-100 (default 80).
#>
param(
    [switch]$Uninstall,
    [int]$Brightness = 80
)

$ErrorActionPreference = "Stop"
$TaskName = "ElgatoMeetingLight"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainScript = Join-Path $ScriptDir "main.py"

# --- Uninstall ---
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Scheduled task '$TaskName' removed."
    } else {
        Write-Host "Task '$TaskName' not found — nothing to remove."
    }
    exit 0
}

# --- Find Python (bypass Microsoft Store stub) ---
$PythonExe = $null

# 1. Python Launcher (py.exe) — installed by the official Python installer, lives in C:\Windows
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PyLauncher = (Get-Command py).Source
    $PythonExe = & $PyLauncher -c "import sys; print(sys.executable)" 2>$null
}

# 2. Common user-install paths for Python 3.12 / 3.11 / 3.10
if (-not $PythonExe) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PythonExe = $c; break }
    }
}

if (-not $PythonExe) {
    Write-Error @"
Real Python not found.
Fix: Settings > Apps > Advanced app settings > App execution aliases
     toggle OFF python.exe and python3.exe, then re-open this terminal.
"@
    exit 1
}

Write-Host "Python: $PythonExe"

# Prefer pythonw.exe (no console window when running as startup task)
$PythonwExe = Join-Path (Split-Path $PythonExe) "pythonw.exe"
if (-not (Test-Path $PythonwExe)) {
    $PythonwExe = $PythonExe
    Write-Host "pythonw.exe not found — startup task will show a brief console window"
}

# --- Install dependencies ---
Write-Host "`nInstalling Python dependencies..."
& $PythonExe -m pip install -r (Join-Path $ScriptDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }
Write-Host "Dependencies installed."

# --- Connectivity check ---
# Blinks the light on then off, then leaves it matched to the camera.
Write-Host "`nChecking the light (on, then off), then matching it to the camera..."
& $PythonExe $MainScript --test
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Light check reported issues. Check elgato-light.log for details."
    Write-Host "Continuing with task registration anyway."
}

# --- Register scheduled task ---
$Action = New-ScheduledTaskAction `
    -Execute $PythonwExe `
    -Argument "`"$MainScript`" --brightness $Brightness" `
    -WorkingDirectory $ScriptDir

$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal | Out-Null

Write-Host "`nScheduled task '$TaskName' registered — will run at every login."
Write-Host "Starting it now..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Done. Logs: $ScriptDir\elgato-light.log"
