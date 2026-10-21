# Общие функции для скриптов windows\*.ps1 (подключается через dot-source).
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Image) { $Image = 'safety-mcp-server:0.2.0' }
if (-not $Dir) { $Dir = Join-Path $Root 'local' }
$Dir = [IO.Path]::GetFullPath($Dir)

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') завершился с кодом $LASTEXITCODE" }
}

function Assert-Image {
    & docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "нет образа $Image. Соберите: docker compose build (в корне проекта) или загрузите: docker load -i dist\<архив>.tar.gz"
    }
}

function Assert-Stand([string]$Name) {
    if ($Name -cnotmatch '^[a-z][a-z0-9]{0,15}$' -or $Name -eq 'global') { throw "недопустимое имя стенда: $Name" }
}

# Значение переменной из local\.env (или $null)
function Get-EnvValue([string]$Name) {
    $envFile = Join-Path $Dir '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^$Name=(.+?)\s*$") { return $Matches[1] }
    }
    return $null
}

# Папки с ключами: SAFETY_KEYS_RO_DIR / SAFETY_KEYS_RW_DIR из local\.env, иначе local\keys-ro и local\keys-rw
function Get-KeyDir([ValidateSet('ro', 'rw')][string]$Role) {
    $v = Get-EnvValue "SAFETY_KEYS_$($Role.ToUpper())_DIR"
    if (-not $v) { $v = "keys-$Role" }
    if (-not [IO.Path]::IsPathRooted($v)) { $v = Join-Path $Dir $v }
    return [IO.Path]::GetFullPath($v)
}

# UID/GID пользователей контейнера из local\.env (по умолчанию 1000 и 1001)
function Get-UidEnv {
    $ids = @{ MCP_UID = '1000'; MCP_GID = '1000'; APPROVER_UID = '1001'; APPROVER_GID = '1001' }
    $envFile = Join-Path $Dir '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^(MCP_UID|MCP_GID|APPROVER_UID|APPROVER_GID)=(\d+)\s*$') { $ids[$Matches[1]] = $Matches[2] }
        }
    }
    $out = @()
    foreach ($k in $ids.Keys) { $out += @('-e', "$k=$($ids[$k])") }
    return $out
}

function Mount-Bind([string]$Src, [string]$Dst, [switch]$ReadOnly) {
    $m = "type=bind,src=$Src,dst=$Dst"
    if ($ReadOnly) { $m += ',readonly' }
    return @('--mount', $m)
}

function Compose-Args {
    return @('compose', '--project-directory', $Dir, '-f', (Join-Path $Root 'docker-compose.yml'),
             '-f', (Join-Path $Root 'docker-compose.windows.yml'))
}
