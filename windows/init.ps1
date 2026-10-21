<#
.SYNOPSIS
  Готовит safety-mcp-server на Windows: каталог local\, токен MCP, папки для ключей и (по желанию) сами ключи.
.DESCRIPTION
  Ничего не перезаписывает: существующие файлы и ключи остаются как есть, запускать можно повторно
  (например, чтобы выпустить ключи нового стенда).
  Права на файлы ключей в Windows настраивать не нужно: контейнер при старте сам копирует ключи в память
  с правами 600. Готовые ключи можно просто положить в папки keys-ro и keys-rw.
.EXAMPLE
  .\windows\init.ps1                 # только каталоги и токен
.EXAMPLE
  .\windows\init.ps1 -Keys vpn       # плюс новые ключи safety_vpn_ro и safety_vpn_rw
#>
param(
    [string[]]$Keys = @(),
    [string]$Dir,
    [string]$Image
)
. "$PSScriptRoot\common.ps1"
foreach ($s in $Keys) { Assert-Stand $s }

$created = @(); $kept = @()
$newConf = -not (Test-Path (Join-Path $Dir 'conf'))
foreach ($d in 'conf', 'state', 'queue', 'queue\requests', 'queue\results') {
    $p = Join-Path $Dir $d
    if (Test-Path $p) { $kept += "$d\" } else { New-Item -ItemType Directory -Path $p | Out-Null; $created += "$d\" }
}

foreach ($f in Get-ChildItem (Join-Path $Root 'conf.example') -File) {
    $dst = Join-Path $Dir "conf\$($f.Name)"
    if (Test-Path $dst) { $kept += "conf\$($f.Name)"; continue }
    if ($f.Name -like 'known_hosts.*' -and -not $newConf) { continue }   # заглушки примера — только в новый conf\
    Copy-Item $f.FullName $dst; $created += "conf\$($f.Name)"
}
$envDst = Join-Path $Dir '.env'
if (Test-Path $envDst) { $kept += '.env' } else { Copy-Item (Join-Path $Root '.env.example') $envDst; $created += '.env' }

# папки ключей — после .env: их пути можно переопределить там (SAFETY_KEYS_RO_DIR / SAFETY_KEYS_RW_DIR)
$roDir = Get-KeyDir ro; $rwDir = Get-KeyDir rw
foreach ($p in $roDir, $rwDir) {
    if (Test-Path $p) { $kept += $p } else { New-Item -ItemType Directory -Path $p | Out-Null; $created += $p }
}

$tokenFile = Join-Path $Dir 'mcp-token'
if (Test-Path $tokenFile) { $kept += 'mcp-token' } else {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [IO.File]::WriteAllText($tokenFile, $token)                        # без BOM и перевода строки
    $created += 'mcp-token (Bearer-токен для клиента MCP)'
}

if ($Keys.Count -gt 0) {
    Assert-Image
    $run = @('run', '--rm', '--entrypoint', 'sh') + (Mount-Bind $roDir '/out-ro') + (Mount-Bind $rwDir '/out-rw') +
           @($Image, '/app/docker/keys-init.sh') + $Keys
    Invoke-Docker @run
}

Write-Host "Каталог данных: $Dir"
foreach ($c in $created) { Write-Host "  создано:   $c" }
foreach ($k in $kept) { Write-Host "  оставлено: $k" }
Write-Host @"

Ключи: ro — $roDir, rw — $rwDir
  Имена: safety_<стенд>_ro / safety_<стенд>_rw (+ .pub). Права в Windows не важны.

Дальше (подробно — README, раздел Windows):
  1. Стенды и хосты: $Dir\conf\inventory.json (ключи: /keys/safety_<стенд>_ro и /keys-rw/safety_<стенд>_rw),
     команды: $Dir\conf\commands.json
  2. Ключ хоста:   .\windows\keyscan.ps1 -Stand <стенд> -Address <адрес>     (сверьте отпечаток!)
  3. Обёртка:      .\windows\deploy.ps1 -Stand <стенд> -DeployKey <ваш ключ доступа> -InstallKeys
  4. FreeOTP:      .\windows\compose.ps1 run --rm approver enroll --account <имя>   (в своём окне PowerShell)
  5. Запуск:       .\windows\compose.ps1 up -d
     MCP: http://127.0.0.1:8766/mcp, токен в $tokenFile
"@
