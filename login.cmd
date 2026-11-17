@echo off
rem Login unico do Claude Code na assinatura (abre o navegador). Pode dar duplo clique.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\login.ps1"
pause
