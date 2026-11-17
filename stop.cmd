@echo off
rem Para somente o LiveForge. Nunca toca no OBS nem na live.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
pause
