# Registers the "Sai robot" task: starts Sai hidden whenever this user logs in to Windows.
# Run once:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$root = $PSScriptRoot
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}"' -f (Join-Path $root 'start_sai_hidden.vbs'))
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Sai robot' -Description 'Starts the Sai desk robot brain at login.' `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output 'Registered scheduled task "Sai robot" (runs at login).'
