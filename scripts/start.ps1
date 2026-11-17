# Inicia o LiveForge (agentes 1 e 2 por padrão). Ex.: .\scripts\start.ps1 --agents monitor,shorts
Set-Location (Split-Path $PSScriptRoot)
.venv\Scripts\python.exe main.py doctor
if ($LASTEXITCODE -ne 0) { Write-Host 'Corrija os itens [!!] acima antes de iniciar.' -ForegroundColor Red; exit 1 }
.venv\Scripts\python.exe main.py run @args
