<#
.SYNOPSIS
  Раскладывает safety-wrapper и правила на хосты стенда и (с -InstallKeys) добавляет ключи safety в authorized_keys.
.DESCRIPTION
  Запускает wrapper/deploy.sh в контейнере: на Windows не нужны bash, ssh и права на файлы ключей.
  -DeployKey — ВАШ ключ доступа к ssh_user (не ключ safety). Он только читается; если у него есть пароль,
  его спросит ssh-add. Ключи safety берутся из папок keys-ro и keys-rw (из keys-rw — только публичный ключ).
.EXAMPLE
  .\windows\deploy.ps1 -Stand vpn -DeployKey $HOME\.ssh\id_ed25519 -InstallKeys
.EXAMPLE
  .\windows\deploy.ps1 -Stand vpn -HostName vpn1 -DeployKey $HOME\.ssh\id_ed25519
#>
param(
    [Parameter(Mandatory)][string]$Stand,
    [Parameter(Mandatory)][string]$DeployKey,
    [string]$HostName,
    [switch]$InstallKeys,
    [string]$Dir,
    [string]$Image
)
. "$PSScriptRoot\common.ps1"
Assert-Stand $Stand
Assert-Image
$DeployKey = (Resolve-Path $DeployKey).Path
$run = @('run', '--rm', '-it', '--user', '0', '--entrypoint', 'bash') +
       (Mount-Bind (Join-Path $Dir 'conf') '/conf' -ReadOnly) +
       (Mount-Bind (Get-KeyDir ro) '/keys-src' -ReadOnly) + (Mount-Bind (Get-KeyDir rw) '/keys-rw-src' -ReadOnly) +
       (Mount-Bind $DeployKey '/in/deploy_key' -ReadOnly) +
       @($Image, '/app/docker/deploy-in-container.sh', $Stand)
if ($HostName) { $run += $HostName }
if ($InstallKeys) { $run += '--install-keys' }
Invoke-Docker @run
