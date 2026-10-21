<#
.SYNOPSIS
  Добавляет ключ хоста (ed25519) в local\conf\known_hosts.<стенд> и показывает отпечаток для сверки.
.DESCRIPTION
  Вместо "ssh-keyscan ... >> known_hosts": в PowerShell '>>' пишет UTF-16, и ssh такой файл не прочитает.
.EXAMPLE
  .\windows\keyscan.ps1 -Stand vpn -Address 10.0.0.5
#>
param(
    [Parameter(Mandatory)][string]$Stand,
    [Parameter(Mandatory)][string]$Address,
    [int]$Port = 22,
    [string]$Dir,
    [string]$Image
)
. "$PSScriptRoot\common.ps1"
Assert-Stand $Stand
Assert-Image
$run = @('run', '--rm', '--entrypoint', 'sh') + (Mount-Bind (Join-Path $Dir 'conf') '/conf-rw') +
       @($Image, '/app/docker/keyscan.sh', $Stand, $Address, "$Port")
Invoke-Docker @run
