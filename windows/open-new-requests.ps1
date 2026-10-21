<#
.SYNOPSIS
  Следит за очередью заявок и открывает страницу подтверждения в браузере. Ничего не подтверждает.
.EXAMPLE
  Start-Process powershell -WindowStyle Minimized -ArgumentList '-File', '.\windows\open-new-requests.ps1'
#>
param(
    [string]$Dir,
    [string]$Url = 'http://127.0.0.1:8765'
)
. "$PSScriptRoot\common.ps1"
$queue = Join-Path $Dir 'queue\requests'
$seen = @{}
Get-ChildItem $queue -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object { $seen[$_.BaseName] = $true }  # старые не открываем
Write-Host "Слежу за $queue (Ctrl+C — выход)"
while ($true) {
    Get-ChildItem $queue -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object {
        $id = $_.BaseName
        if ($id -match '^[0-9a-f]{32}$' -and -not $seen.ContainsKey($id)) {
            $seen[$id] = $true
            Start-Process "$Url/r/$id"
        }
    }
    Start-Sleep -Seconds 1
}
