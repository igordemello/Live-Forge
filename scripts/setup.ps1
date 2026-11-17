# Instalação idempotente (tudo local à pasta do LiveForge; nada global além dos pré-requisitos abaixo).
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)

$missing = @()
if (-not (Get-Command uv -ErrorAction SilentlyContinue))     { $missing += 'winget install astral-sh.uv' }
if (-not (Get-Command node -ErrorAction SilentlyContinue))   { $missing += 'winget install OpenJS.NodeJS.LTS' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { $missing += 'winget install Gyan.FFmpeg' }
if ($missing) {
    Write-Host 'Faltam programas. Rode no Prompt de Comando, feche e abra o terminal, e rode setup.cmd de novo:' -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "  $_" }
    exit 1
}

if (-not (Test-Path .venv)) { uv venv --python 3.13 .venv }
uv pip install --python .venv\Scripts\python.exe -r requirements.lock
Push-Location hyperframes
npm ci
node node_modules\hyperframes\bin\hyperframes.mjs browser ensure
Pop-Location
'data', 'output', 'recordings', 'work', 'logs' | ForEach-Object { New-Item -ItemType Directory -Force $_ | Out-Null }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Write-Host ''
Write-Host 'Instalado. Próximos passos: login.cmd (Claude) e start.cmd — o painel abre sozinho; veja a aba Configuração.' -ForegroundColor Green
