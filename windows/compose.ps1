<#
.SYNOPSIS
  docker compose с каталогом local\ и Windows-томами. Все аргументы передаются как есть.
.EXAMPLE
  .\windows\compose.ps1 up -d
.EXAMPLE
  .\windows\compose.ps1 run --rm approver enroll --account ivan
.EXAMPLE
  .\windows\compose.ps1 logs -f safety-mcp-server
#>
$Dir = $env:SAFETY_DATA_DIR
. "$PSScriptRoot\common.ps1"
if (-not (Test-Path (Join-Path $Dir 'mcp-token'))) { throw "нет $Dir\mcp-token: сначала .\windows\init.ps1" }
$a = (Compose-Args) + $args
& docker @a
exit $LASTEXITCODE
