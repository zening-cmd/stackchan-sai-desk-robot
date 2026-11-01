@echo off
rem Starts Sai in the background (no window). Safe to run twice: only one copy runs.
wscript.exe "%~dp0start_sai_hidden.vbs"
echo Sai is starting. The robot will say hello in about 30 seconds.
timeout /t 5 >nul
