# Keeps Sai's brain running: starts it, and restarts it 10 seconds after it stops for any reason.
# Started hidden at Windows login by the "Sai robot" scheduled task (see install_autostart.ps1).
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Force $runtime | Out-Null
$keeperLog = Join-Path $runtime 'keeper.log'

function Write-KeeperLog([string]$message) {
    Add-Content -Path $keeperLog -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message) -Encoding utf8
}

function Limit-Log([string]$path) {
    if ((Test-Path $path) -and (Get-Item $path).Length -gt 5MB) {
        Move-Item -Force $path "$path.old"
    }
}

# Only one keeper may run: two brains would fight over the robot.
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Global\SaiRobotKeeper', [ref]$created)
if (-not $created) {
    Write-KeeperLog 'another keeper is already running; exiting'
    exit 0
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe' }

try {
    while ($true) {
        Limit-Log $keeperLog
        Limit-Log (Join-Path $runtime 'gateway.log')
        Write-KeeperLog 'starting brain'
        $process = Start-Process -FilePath $uv -ArgumentList 'run', 'python', 'main.py' `
            -WorkingDirectory (Join-Path $root 'brain') -WindowStyle Hidden -PassThru `
            -RedirectStandardError (Join-Path $runtime 'brain-stderr.log')
        $null = $process.Handle  # keep a handle open so ExitCode is available after exit
        $process.WaitForExit()
        Write-KeeperLog ("brain exited with code {0}; restarting in 10 seconds" -f $process.ExitCode)
        Start-Sleep -Seconds 10
    }
}
finally {
    $mutex.ReleaseMutex()
}
