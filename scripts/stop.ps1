# Para SOMENTE o LiveForge e seus processos filhos (ffmpeg/node/chrome). Nunca toca no OBS nem na live.
Set-Location (Split-Path $PSScriptRoot)
$pidFile = 'data\liveforge.pid'
Set-Content -Path 'data\STOP' -Value 'stop'
for ($i = 0; $i -lt 10 -and (Test-Path $pidFile); $i++) { Start-Sleep -Seconds 1 }
if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile)
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like 'python*') { taskkill /PID $id /T /F | Out-Null; Write-Host "LiveForge (PID $id) encerrado; o job em andamento será refeito no próximo início." }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
} else { Write-Host 'LiveForge encerrado.' }
