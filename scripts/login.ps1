# Login único do Claude Code na sua assinatura Claude (abre o navegador). Não usa API paga.
Set-Location (Split-Path $PSScriptRoot)
# Ignora variáveis de sessões do app desktop, para o login valer fora dele.
Get-ChildItem env: | Where-Object { $_.Name -match '^(CLAUDE|ANTHROPIC)' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
$exe = & .venv\Scripts\python.exe -c "from liveforge.llm import find_claude; print(find_claude() or '')"
if (-not $exe) { Write-Host 'Claude Code não encontrado (instale/abra o app Claude desktop).' -ForegroundColor Red; exit 1 }
& $exe auth login --claudeai
& $exe auth status --text
