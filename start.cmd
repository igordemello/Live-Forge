@echo off
rem Inicia o LiveForge (doctor + agentes do config.yaml). Pode dar duplo clique.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
pause
