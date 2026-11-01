@echo off
rem Stops Sai: the keeper first (so it doesn't restart the brain), then the brain and the gateway.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run_sai\.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='uv.exe'\" | Where-Object { $_.CommandLine -match 'main\.py|stackchan-mcp' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Sai is stopped.
timeout /t 3 >nul
