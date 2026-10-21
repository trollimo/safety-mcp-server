# Задание: MCP-сервер `safety-mcp-server` (чтение на стендах + согласуемая запись)

Исполнитель: иди по пунктам по порядку, ничего сверх описанного не добавляй. Если что-то неясно, остановись и спроси.

## 1. Цель

Название проекта: **safety-mcp-server**. Внутренние имена каталогов и переменных (`/etc/mcp-ssh`, `~/.mcp-ssh`, `MCP_SSH_CONF`, `MCP_SSH_HOME`, `MCP_SSH_ALLOW_WRITABLE`) остаются как в документе и в названии проекта не участвуют.

MCP-сервер на Python (`mcp` SDK, `FastMCP`) даёт ИИ-агенту:
- выполнять **только заранее описанные read-команды** на хостах нескольких стендов (`dev`, `test`, `prod`, ... от 3 и больше) по SSH;
- выполнять **только заранее описанные HTTP-проверки** (например, «доступна ли форма авторизации на prod»): привязанные к стенду либо независимые от стендов (`global`);
- смотреть **список хостов стенда с тегами ролей** и не иметь возможности его менять;
- запрашивать **команды изменения** только через отдельное согласование, которое агент подтвердить не может.

Сценарий: «покажи воркер-ноды swarm на dev» →
1. агент вызывает `dev_hosts_list` (видит теги) и затем `dev_swarm_nodes_list`;
2. сервер сам выбирает manager-хост стенда `dev` и выполняет команду из JSON.

## 2. Не цели

- Агент не передаёт произвольную строку команды.
- Сервер не пишет в конфигурацию и инвентарь (только читает).
- Нет веб-интерфейса, БД, шифрования секретов, Windows.
- HTTP: в первой версии только `GET`/`HEAD`, без JS и браузера, без авторизации, без записи (`POST/PUT/DELETE`). Для них заложены поля в схеме, но при старте сервер **отклоняет** такие команды с понятной ошибкой.

## 3. Раскладка файлов

```
safety-mcp-server/
  server.py              # весь код MCP + CLI-команды (см. п. 9.1)
  wrapper/
    mcp-wrapper          # серверная обёртка (п. 9.2), одна для ro и rw
    deploy.sh            # генерация правил и копирование в ~/.mcp-ssh на хосты (п. 9.4)
  conf/                  # образец; в бою /etc/mcp-ssh/ (root:root, 0644)
    inventory.json
    commands.json
    known_hosts.dev      # по файлу на стенд
  tests/test_server.py   # pytest (п. 10)
  README.md
```

Состояние (не конфиг): `~/.local/state/safety-mcp-server/` (`pending/`, `audit.log`).
Каталог конфига: переменная `MCP_SSH_CONF`, по умолчанию `/etc/mcp-ssh`.

## 4. `inventory.json`

```json
{
  "stands": {
    "dev": {
      "ssh_user": "opsuser",
      "known_hosts": "known_hosts.dev",
      "keys": {
        "ro": "/home/user/.ssh/mcp_dev_ro",
        "rw": "/home/user/.ssh/mcp_dev_rw"
      },
      "hosts": {
        "dev-sw-m1": {"address": "10.10.0.11", "tags": ["swarm_manager", "docker"]},
        "dev-sw-w1": {"address": "10.10.0.21", "tags": ["swarm_worker", "docker"]},
        "dev-sw-w2": {"address": "10.10.0.22", "tags": ["swarm_worker", "docker"]}
      }
    },
    "test": {"ssh_user": "opsuser", "known_hosts": "known_hosts.test",
             "keys": {"ro": "/home/user/.ssh/mcp_test_ro"}, "hosts": {}}
  }
}
```

- `keys.rw` необязателен. Без него у стенда нет write-инструментов и нет `rules.rw`.
- `tags`: произвольный список `^[a-z0-9_]{1,32}$`, количество не ограничено.
- Имя стенда `^[a-z][a-z0-9]{0,15}$`, **кроме зарезервированного `global`**; имя хоста `^[a-z0-9][a-z0-9.-]{0,62}$`.
- Любая ошибка валидации: сервер не стартует и пишет понятное сообщение.

### 4.0 Настройки параллельности

В корне `inventory.json` необязательный блок:

```json
"settings": {"max_parallel": 5, "max_hosts": 20}
```

- `max_parallel`: сколько хостов обрабатывается **одновременно** при `pick: all` (по умолчанию 5; допустимо 1–50). Значение `1` включает последовательное выполнение.
- `max_hosts`: сколько хостов максимум допускается в одном вызове (по умолчанию 20; допустимо 1–200). Если подходящих хостов больше, вызов возвращает ошибку «слишком много хостов: N > max_hosts», ничего не выполняя.
- Для стенда можно переопределить `max_parallel` внутри блока стенда (`"max_parallel": 10`), если он отличается от общего.
- Значения вне диапазона и нецелые отклоняются при старте.

### 4.1 HTTP-эндпоинты

Базовые URL задаются **в инвентаре**, а не в командах и не агентом. Для стенда: ключ `endpoints` внутри стенда. Для не привязанных к стенду: верхний ключ `global.endpoints`.

```json
{
  "stands": {
    "prod": {
      "ssh_user": "opsuser", "known_hosts": "known_hosts.prod",
      "keys": {"ro": "/home/user/.ssh/mcp_prod_ro"},
      "hosts": {},
      "endpoints": {
        "app": {"base_url": "https://app.example.com"},
        "api": {"base_url": "https://api.example.com", "ca_file": "ca_internal.pem"}
      }
    }
  },
  "global": {
    "endpoints": {
      "status_page": {"base_url": "https://status.example.com"},
      "sso": {"base_url": "https://sso.example.com"}
    }
  }
}
```

- `base_url`: только `https://` (или `http://`, если задано `"allow_http": true`), без пути, без пользователя/пароля, без query. Имя эндпоинта `^[a-z0-9_]{1,32}$`.
- `ca_file` необязателен (относительно каталога конфига). Проверка TLS-сертификата всегда включена и отключаться не может.
- `stands.<s>.hosts` и `keys` необязательны, если у стенда есть только HTTP-проверки (SSH-инструменты тогда не регистрируются).

## 5. `commands.json`

### 5.0 Общие команды и команды стенда

Файл состоит из двух частей:

```json
{
  "common": { "read": { ... }, "write": { ... } },
  "stands": {
    "dev":  { "read": { "swarm_prune_dry_run": { ... } } },
    "prod": {}
  }
}
```

- `common`: команды **для всех стендов**. Стенд подставляется автоматически (для каждого стенда создаётся свой инструмент).
- `stands.<стенд>`: команды **только этого стенда** (блоки `read` и `write`, оба необязательны). Блок стенда может быть **пустым** или **вообще отсутствовать**: тогда у стенда только общие команды. Блок `stands` целиком тоже необязателен.
- Имя команды уникально в пределах стенда: если имя из `stands.<s>` совпадает с именем из `common`, это **ошибка старта** (тихого переопределения нет).
- Имя стенда в `stands` должно существовать в `inventory.json`, иначе ошибка старта.
- Команда из `stands.<s>` не может иметь `scope: "global"` (она привязана к стенду по определению).
- Для стенда итоговый набор = `common` + `stands.<s>`; применяются все остальные правила (теги, `pick`, `type`, `params`).

Пример ниже и все примеры в разделах 5.1 и 12 показывают содержимое блока `common`, если не сказано иное.

Единый набор команд для всех стендов (блок `common`). Стенд подставляется автоматически.

```json
{
  "read": {
    "uptime": {
      "description": "Аптайм хоста",
      "tags": [], "pick": "host",
      "cmd": "uptime"
    },
    "swarm_nodes_list": {
      "description": "Список нод Docker Swarm (выполняется на manager)",
      "tags": ["swarm_manager"], "pick": "first",
      "cmd": "sudo -n docker node ls"
    },
    "disk_free_workers": {
      "description": "Свободное место на ВСЕХ воркерах",
      "tags": ["swarm_worker"], "pick": "all",
      "cmd": "df -h /"
    },
    "disk_free_worker": {
      "description": "Свободное место на ОДНОМ воркере (параметр host)",
      "tags": ["swarm_worker"], "pick": "host",
      "cmd": "df -h /"
    },
    "service_status": {
      "description": "Статус systemd-юнита на хосте",
      "tags": [], "pick": "host",
      "params": {"unit": "[a-z0-9][a-z0-9@._-]{0,63}"},
      "cmd": "systemctl status --no-pager -- {unit}"
    }
  },
  "write": {
    "service_restart": {
      "description": "Перезапуск systemd-юнита",
      "tags": [], "pick": "host",
      "params": {"unit": "[a-z0-9][a-z0-9@._-]{0,63}"},
      "cmd": "sudo -n systemctl restart -- {unit}"
    }
  }
}
```

Поля:
- `tags`: хост должен иметь **все** теги. `[]` — любой хост стенда.
- `pick`:
  - `first` — один хост, первый подходящий по алфавиту;
  - `all` — **все** подходящие хосты (не больше `max_hosts`, по умолчанию 20), выполняется **параллельно** (п. 7.2), у каждого заголовок с именем хоста, сбой одного не останавливает остальные;
  - `host` — агент **обязан** передать параметр `host`; он должен быть в инвентаре этого стенда и иметь теги команды.
- `params`: имя → regex. Значение подставляется в `cmd` через `shlex.quote`.
- **Regex параметров только в синтаксисе POSIX ERE**: используй `[0-9]`, не `\d`; без `(?...)`, `\w`, `\s`, обратных ссылок. Причина: тот же regex исполняет серверная обёртка на bash. Сервер при старте отклоняет несовместимые regex.
- Имя команды `^[a-z0-9_]{1,40}$`. Команда `cmd` не должна содержать `;|&$\`<>"'*` и подобное (проверка при старте).
- `type`: `"ssh"` (по умолчанию) или `"http"`. Тип определяет, какой **обработчик** (handler) выполняет команду (см. 5.2).

### 5.1 HTTP-команды

Лежат в том же `commands.json`, в блоке `read`, с `"type": "http"`:

```json
"login_form_check": {
  "type": "http", "scope": "stand", "endpoint": "app",
  "description": "Доступна ли форма авторизации",
  "method": "GET", "path": "/login",
  "expect": {"status": [200], "body_contains": "<form", "max_ms": 3000}
},
"sso_health": {
  "type": "http", "scope": "global", "endpoint": "sso",
  "description": "Здоровье SSO (общий для всех стендов)",
  "method": "GET", "path": "/health",
  "expect": {"status": [200]}
},
"page_check": {
  "type": "http", "scope": "stand", "endpoint": "app",
  "description": "Проверка страницы по пути",
  "method": "GET", "path": "/{page}",
  "params": {"page": "[a-z0-9][a-z0-9_-]{0,40}"},
  "expect": {"status": [200]}
}
```

Поля:
- `scope`: `stand`: команда регистрируется для **каждого** стенда, у которого в `endpoints` есть `endpoint` с таким именем (в инструменте имя стенда; стенд без такого эндпоинта пропускается). `global`: регистрируется один раз, с префиксом `global_`, эндпоинт берётся из `global.endpoints`.
- `method`: только `GET` или `HEAD`.
- `path`: начинается с `/`; `{param}` подставляется после `urllib.parse.quote(value, safe="")`; regex параметров по правилам ERE (п. 5).
- `expect` (всё необязательно): `status` (список допустимых кодов), `body_contains` (строка, ищется в первых 200 КБ тела), `max_ms` (порог времени). Если `expect` нет, достаточно получить любой ответ.
- `return_body`: `true` вернёт агенту до 2000 символов тела (по умолчанию `false`; тело в ответ не попадает).
- Зарезервировано и в версии 1 **отклоняется** при старте: `method` кроме GET/HEAD, `auth`, `headers`, `body`, `via` (выполнение запроса через хост стенда по SSH).

### 5.2 Обработчики типов (архитектура)

В `server.py` реализуй реестр обработчиков:

```python
HANDLERS = {"ssh": SshHandler(), "http": HttpHandler()}
```

Каждый обработчик имеет методы:
- `validate(name, cmd, inventory)` — проверка описания при старте, любая ошибка = не стартуем;
- `scopes(cmd)` — список областей, для которых регистрировать инструмент (стенды или `global`);
- `run(scope, cmd, args) -> str` — выполнение и форматирование вывода;
- `rules(cmd, host)` — только у `ssh` (для `gen-rules`); у `http` нет.

Регистрация инструментов, аудит, ограничение размера вывода, запрет произвольных строк общие для всех типов. Новый тип (например, `tcp`) добавляется новым обработчиком без правок ядра.

## 6. MCP-инструменты

Для каждого стенда `S` берётся набор `common` + `stands.S` (п. 5.0). Для стенда `S` и каждой команды `C` из этого набора регистрируется инструмент **`S_C`** (например, `dev_swarm_nodes_list`) через `mcp.add_tool(fn, name=..., description=...)`; `fn` создаётся замыканием, без общей переменной стенда.

| Инструмент | Что делает |
|---|---|
| `S_<read>` | Выполняет read-команду. Параметры: `host` (по `pick`) + `params` команды |
| `S_<write>` | Только если у стенда есть `rw`. **Не выполняет**, а создаёт заявку и возвращает `id` и строку для одобрения |
| `S_hosts_list` | Хосты стенда с тегами. Параметр `tag` (необязательный) |
| `S_<http-команда>` | HTTP-проверка эндпоинта стенда S (`scope: stand`) |
| `global_<http-команда>` | HTTP-проверка независимого эндпоинта (`scope: global`) |
| `S_endpoints_list`, `global_endpoints_list` | Имена и `base_url` эндпоинтов (без `ca_file` и без секретов) |
| `stands_list` | Стенды и признак `rw`; отдельно: есть ли у стенда hosts и endpoints |
| `write_result` | Параметр `id`. Результат одобренной заявки или «ещё не выполнена» |

Имя MCP-сервера: `FastMCP("safety-mcp-server")`.

Описание каждого инструмента начинается с `[СТЕНД: DEV]`. Первая строка вывода: `[stand=dev host=dev-sw-m1 role=ro]`. При `pick: all` такая строка перед выводом каждого хоста.

## 7. Выполнение SSH

Только `subprocess.run([...], shell=False)`:

```
ssh -i <key> -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<conf>/<known_hosts стенда>
    -o ForwardAgent=no -o ClearAllForwardings=yes
    <ssh_user>@<address> <команда>
```

- Таймаут 60 с на хост; вывод до 20 000 символов, с пометкой `[обрезано]`.
- `address` и `ssh_user` только из инвентаря.
- Нет подходящего хоста: ошибка «нет хоста с тегами ...», ничего не выполняется.
- Для `pick: all` хосты обрабатываются параллельно по п. 7.2.
- Каждый вызов в `audit.log`: время, тип, стенд (или `global`), хост/эндпоинт, роль, команда, код возврата.

### 7.2 Параллельное выполнение на нескольких хостах

Относится к `pick: all`:

```python
from concurrent.futures import ThreadPoolExecutor
workers = min(max_parallel, len(hosts))
with ThreadPoolExecutor(max_workers=workers) as ex:
    results = list(ex.map(run_one, sorted_hosts))   # map сохраняет порядок
```

- `run_one(host)` выполняет один `ssh` с собственным таймаутом (60 с) и возвращает текст; **исключения не пробрасываются**, а превращаются в `ERROR <тип>` для этого хоста. Сбой одного хоста не влияет на остальные.
- Вывод всегда в порядке имён хостов (не в порядке завершения), у каждого хоста своя первая строка `[stand=… host=… role=ro]`.
- Общий итог последней строкой: `SUMMARY: ok=N failed=M`.
- Одновременно не больше `max_parallel` ssh-процессов (проверяется тестом). Аудит: по одной строке на хост.
- Общий лимит вывода 20 000 символов делится между хостами поровну (`20000 // число_хостов`); обрезка помечается `[обрезано]`.
- Для `pick: first` и `pick: host` параллельность не используется.

### 7.1 Выполнение HTTP

Библиотека `httpx` (синхронно). Параметры запроса:

```python
httpx.request(method, url, follow_redirects=False, verify=ca_or_True,
              timeout=httpx.Timeout(10.0), headers={"User-Agent": "safety-mcp-server/1"})
```

- `url` = `base_url` из инвентаря + `path` из команды (параметры закодированы). После сборки проверь: `urlparse(url).hostname` и порт совпадают с `base_url`, схема та же. Иначе ошибка, запрос не отправляется.
- Редиректы **не** выполняются. Если ответ 3xx, вывод содержит код и заголовок `Location` (только его), результат оценивается по `expect.status`.
- Читать не больше 200 КБ тела (`stream`), остальное отбросить.
- Заголовки ответа агенту **не** возвращаются, кроме `Location` и `Content-Type`.
- Формат вывода:
  ```
  [stand=prod endpoint=app GET https://app.example.com/login]      # для global: [scope=global endpoint=sso ...]
  RESULT: PASS|FAIL   status=200   time=142ms   size=18234B
  - status in [200]: ok
  - body_contains "<form": ok
  - max_ms 3000: ok
  ```
  При ошибке сети/TLS: `RESULT: ERROR <тип ошибки>` (без трассировки и без внутренних адресов).
- Нельзя ходить на адреса, которых нет в инвентаре: агент не может указать URL, хост или схему.

## 8. Безопасность (обязательно)

1. **Агент не меняет конфиг.** При старте сервер проверяет, что `inventory.json`, `commands.json`, `known_hosts.*` и каталог конфига недоступны текущему пользователю на запись (`os.access(path, os.W_OK)`). Иначе не стартует: «сделайте файлы root:root 0644». В Docker это выполняется монтированием каталога конфига с флагом `:ro` (п. 14). Исключение: `MCP_SSH_ALLOW_WRITABLE=1` (только первая отладка, с предупреждением).
2. Сервер не пишет в каталог конфига.
3. Агент передаёт только имя инструмента и значения параметров, проверенные regex.
4. read всегда использует `keys.ro`. **Ключ `rw` есть только у исполнителя (approver, п. 15), в MCP-сервер он не монтируется.**
5. Стенд зашит в имя инструмента, описание, вывод и аудит; параметра «стенд» нет.
6. **Одобрение записи** выполняется отдельным сервисом-исполнителем по п. 15: веб-страница, 6-значный TOTP-код с телефона, 3 попытки. Подтверждение через `/dev/tty` **не использовать**: агент обходит его через псевдотерминал.
7. Пути ключей не возвращаются агенту ни в каком ответе.
8. **HTTP не должен стать SSRF-прокси.** Агент не задаёт URL, хост, схему, заголовки и метод; хост запроса всегда равен `base_url` из инвентаря; редиректы не выполняются; TLS-проверка не отключается; ответ ограничен по размеру; в ответ не попадают тело (без `return_body`) и заголовки, кроме `Location`/`Content-Type`.
9. HTTP-проверки выполняются с машины, где запущен MCP-сервер. Если эндпоинт стенда доступен только изнутри сети, это нужно учесть (поле `via` зарезервировано, но в версии 1 не реализуется): такие проверки делай SSH-командой с `curl` на хосте стенда.

## 9. Серверная часть

**Пользователей и системные настройки сервера задание не создаёт и не меняет** (никаких `adduser`, `sudoers`, `sshd_config`, файлов вне домашнего каталога). Используется **уже существующий** пользователь `ssh_user` стенда. Обёртка и правила лежат в его домашнем каталоге: `~/.mcp-ssh/`.

Принцип: на хосте у `ssh_user` два ключа MCP (ro и rw), каждый в его `~/.ssh/authorized_keys` с принудительной командой `mcp-wrapper ro` / `mcp-wrapper rw`. Обёртка разрешает только команды, подходящие под regex из `~/.mcp-ssh/rules.<role>`. Сама обёртка не меняется при добавлении команд: меняются только правила. Правила **генерируются из `commands.json`** (единый источник), вручную их не редактируют.

### 9.1 CLI-команды `server.py`

| Команда | Результат |
|---|---|
| `python server.py check` | Валидация инвентаря и команд, проверка прав (п. 8.1) |
| `python server.py list-hosts <stand>` | Строки `имя адрес` |
| `python server.py ssh-user <stand>` | Имя `ssh_user` стенда |
| `python server.py key-path <stand> <ro\|rw>` | Путь к приватному ключу (только для человека; MCP-инструменты путей не возвращают) |
| `python server.py gen-rules <stand> <host> <ro\|rw>` | Правила для хоста в stdout. Включаются только команды роли, у которых `tags` ⊆ теги хоста. Для `rw` без ключа в инвентаре: ошибка (код 2) |

`gen-rules` для хоста стенда `S` берёт команды `common` + `stands.S`, поэтому правила хоста содержат и общие, и «свои» команды стенда, но **не** команды других стендов. Обрабатываются **только команды типа `ssh`**; HTTP-команды в правила не попадают (на хостах для них ничего не нужно).

Формат правил: одна строка = один ERE-regex на **всю** команду. Шаблон `cmd`: литеральные части экранируются (`. ^ $ * + ? ( ) [ ] { } | \`), `{param}` заменяется на `(regex_параметра)`. Первая строка: `# generated sha256=<хэш commands.json+inventory> stand=<s> host=<h> role=<r>`.

Пример для `service_status`:
```
systemctl status --no-pager -- ([a-z0-9][a-z0-9@._-]{0,63})
```

### 9.2 `wrapper/mcp-wrapper`

```bash
#!/bin/bash
# mcp-wrapper <ro|rw>: принудительная команда для ключа.
# Лежит в ~/.mcp-ssh/mcp-wrapper (копируется скриптом deploy.sh), правила рядом: rules.ro / rules.rw
VERSION=2
[[ "${1:-}" == "--version" ]] && { echo "mcp-wrapper $VERSION"; exit 0; }
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
role="${1:-}"
[[ "$role" == ro || "$role" == rw ]] || { echo "bad role" >&2; exit 126; }
base="${MCP_SSH_HOME:-$HOME/.mcp-ssh}"        # MCP_SSH_HOME нужен только для тестов
rules="$base/rules.$role"
cmd="${SSH_ORIGINAL_COMMAND:-}"
from="${SSH_CLIENT:-local}"; from="${from%% *}"
deny() { logger -t mcp-wrapper "DENY role=$role from=$from cmd=$cmd" 2>/dev/null; echo "denied" >&2; exit 126; }

[[ -r "$rules" && -n "$cmd" ]] || deny
charre='^[A-Za-z0-9 ._@:/=,%+-]{1,300}$'      # без ; | & $ ` > < * кавычек и переводов строк
[[ $cmd =~ $charre ]] || deny

while IFS= read -r re || [[ -n "$re" ]]; do
  [[ -z "$re" || "$re" == \#* ]] && continue
  full="^(${re})\$"
  if [[ $cmd =~ $full ]]; then
    logger -t mcp-wrapper "ALLOW role=$role from=$from cmd=$cmd" 2>/dev/null
    read -ra argv <<<"$cmd"                   # деление по пробелам, shell не участвует
    exec "${argv[@]}"
  fi
done < "$rules"
deny
```

Важно для безопасности: ни одно правило не должно позволять писать в `~/.mcp-ssh/` и `~/.ssh/` (команды `tee`, `cp`, `mv`, `sed -i`, `ln`, `chmod` в правила не добавлять).

### 9.3 Ключи в `authorized_keys` (без создания пользователей)

Для каждого ключа в `~/.ssh/authorized_keys` пользователя `ssh_user` нужна строка вида (абсолютный путь):

```
command="/home/<ssh_user>/.mcp-ssh/mcp-wrapper ro",restrict ssh-ed25519 AAAA... mcp dev ro
command="/home/<ssh_user>/.mcp-ssh/mcp-wrapper rw",restrict ssh-ed25519 AAAA... mcp dev rw
```

Эти строки добавляет человек вручную, либо `deploy.sh --install-keys` (дописывает недостающие, существующие не трогает). Если публичный ключ уже есть в `authorized_keys` **без** `command=`, `deploy.sh` выводит предупреждение: такой ключ не ограничен, строку нужно заменить.

Если команде нужен `sudo -n ...`, у существующего `ssh_user` права должны быть уже настроены администратором сервера (`sudo -l`). Задание этого не настраивает. Не добавляйте команды в `commands.json`, пока нужные права не подтверждены.

### 9.4 `wrapper/deploy.sh` (генерация правил и копирование на хост)

```bash
#!/bin/bash
# usage: deploy.sh <stand> [host] [--install-keys]
# Запускает ЧЕЛОВЕК от имени ssh_user стенда (его обычным доступом, переменная DEPLOY_SSH_KEY необязательна).
# НЕ использует ключи mcp_*_ro/rw. Пользователей не создаёт, системные файлы не меняет.
set -euo pipefail
STAND=$1; shift; ONLY=""; INSTALL=0
for a in "$@"; do case "$a" in --install-keys) INSTALL=1;; *) ONLY=$a;; esac; done
cd "$(dirname "$0")"
python3 ../server.py check
U=$(python3 ../server.py ssh-user "$STAND")
CONF="${MCP_SSH_CONF:-/etc/mcp-ssh}"
BASE=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$CONF/known_hosts.$STAND")
SSHO=("${BASE[@]}")
[ -n "${DEPLOY_SSH_KEY:-}" ] && SSHO+=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes)

python3 ../server.py list-hosts "$STAND" | while read -r NAME ADDR; do
  [[ -z "$ONLY" || "$ONLY" == "$NAME" ]] || continue
  T=$(mktemp -d)
  python3 ../server.py gen-rules "$STAND" "$NAME" ro > "$T/rules.ro"
  python3 ../server.py gen-rules "$STAND" "$NAME" rw > "$T/rules.rw" 2>/dev/null || rm -f "$T/rules.rw"
  H=$(ssh -n "${SSHO[@]}" "$U@$ADDR" 'echo $HOME'); D="$H/.mcp-ssh"
  RM=""; [ -f "$T/rules.rw" ] || RM="rm -f $D/rules.rw;"
  ssh -n "${SSHO[@]}" "$U@$ADDR" "umask 077; mkdir -p $D"
  scp -q "${SSHO[@]}" mcp-wrapper "$T"/rules.* "$U@$ADDR:$D/"
  ssh -n "${SSHO[@]}" "$U@$ADDR" "chmod 700 $D $D/mcp-wrapper; chmod 600 $D/rules.*; $RM $D/mcp-wrapper --version"
  rm -rf "$T"

  for ROLE in ro rw; do
    PUB="$HOME/.ssh/mcp_${STAND}_${ROLE}.pub"; [ -f "$PUB" ] || continue
    LINE="command=\"$D/mcp-wrapper $ROLE\",restrict $(cat "$PUB")"
    KEY=$(awk '{print $2}' "$PUB")
    AK=$(ssh -n "${SSHO[@]}" "$U@$ADDR" 'cat ~/.ssh/authorized_keys 2>/dev/null' || true)
    if grep -qxF "$LINE" <<<"$AK"; then echo "$NAME $ROLE: ключ с ограничением на месте"
    elif grep -qF "$KEY" <<<"$AK"; then echo "ВНИМАНИЕ $NAME $ROLE: ключ уже есть БЕЗ ограничения, замените строку на: $LINE"
    elif [ "$INSTALL" = 1 ]; then ssh -n "${SSHO[@]}" "$U@$ADDR" "umask 077; mkdir -p ~/.ssh; printf '%s\n' '$LINE' >> ~/.ssh/authorized_keys"; echo "$NAME $ROLE: ключ добавлен"
    else echo "$NAME $ROLE: добавьте в ~/.ssh/authorized_keys строку: $LINE"; fi
  done

  # проверка: через ro-ключ любая команда вне списка должна быть отклонена кодом 126
  K=$(python3 ../server.py key-path "$STAND" ro)
  set +e; ssh -n -i "$K" -o IdentitiesOnly=yes "${BASE[@]}" "$U@$ADDR" 'id' >/dev/null 2>&1; RC=$?; set -e
  [ "$RC" = 126 ] && echo "$NAME: ok (ro-ключ ограничен)" || echo "$NAME: ПРОВЕРКА НЕ ПРОЙДЕНА (код $RC): ro-ключ не ограничен или не добавлен"
done
```

Вместо `deploy.sh` можно использовать Ansible (`copy` для `mcp-wrapper` и сгенерированных `rules.*` в `~/.mcp-ssh/`), результат тот же.

### 9.5 Когда генерируется и как попадает на хост, как обновляется

- **Генерация правил**: в момент запуска `deploy.sh` на машине администратора, из `commands.json` и `inventory.json`. MCP-сервер ничего не генерирует и ничего не копирует.
- **Копирование**: `deploy.sh` по SSH от имени самого `ssh_user` создаёт `~/.mcp-ssh/` (0700) и кладёт туда `mcp-wrapper` (0700) и `rules.ro`/`rules.rw` (0600). Если `rw` не настроен, на хосте удаляется устаревший `rules.rw`.
- **Первый раз**: `deploy.sh <stand> --install-keys` (или добавьте строки из п. 9.3 вручную).
- **Любое изменение** `commands.json`/инвентаря/обёртки: `deploy.sh <stand>` (или для одного хоста `deploy.sh <stand> <host>`). Файлы заменяются целиком; `--version` показывает версию обёртки.
- **Обновление обёртки**: поднять `VERSION` в `wrapper/mcp-wrapper`, выполнить `deploy.sh` для каждого стенда.
- Агент `deploy.sh` не запускает: скрипт использует доступ человека к `ssh_user`, который агенту не выдаётся.

## 10. Критерии приёмки (pytest без реального SSH: подменяй `subprocess.run`)

- Невалидные `inventory.json`/`commands.json` приводят к ошибке старта; несовместимый с ERE regex параметра тоже.
- `stands_list` показывает стенды; у стенда без `rw` нет write-инструментов и `gen-rules ... rw` возвращает код 2.
- Для `dev` и `test` есть `dev_uptime`, `test_uptime` (имя с префиксом стенда).
- `dev_swarm_nodes_list` выбирает хост с тегом `swarm_manager` из dev.
- `dev_disk_free_workers` (`pick: all`) выполняет команду на всех воркерах dev и ни на каких других; заголовок на каждый хост.
- `dev_disk_free_worker` без `host` отклоняется; `host` из другого стенда или без тега `swarm_worker` отклоняется.
- Параметры с `; id`, `$(x)`, `-f`, пустые отклоняются, команда не выполняется.
- Итоговая команда содержит значения через `shlex.quote`; `shell=False`.
- Write-инструмент не вызывает `ssh`, а создаёт заявку `queue/requests/<id>.json`; `write_result` до одобрения: «ещё не выполнена». Остальные тесты одобрения: п. 15.7.
- Запуск с конфигом, доступным на запись, не стартует (без `MCP_SSH_ALLOW_WRITABLE`).
- Ключи и пути ключей в ответах инструментов отсутствуют.
- `gen-rules`: для хоста без тега команды правило не создаётся; параметры в правилах заменены на `(regex)`; спецсимволы в литералах экранированы.
- Параллельность: при 7 хостах и `max_parallel=3` одновременно запущено не больше 3 вызовов `ssh` (фейковый `subprocess.run` со счётчиком и `sleep`); вывод в порядке имён хостов; сбой одного хоста даёт `ERROR` в его блоке, остальные выполняются; `SUMMARY` верен; `max_parallel` 0, 51, `"a"`, `3.5` отклоняются при старте; число хостов больше `max_hosts` возвращает ошибку без запуска `ssh`; переопределение `max_parallel` у стенда работает.
- Общие и стендовые команды: общая команда есть у всех стендов; команда из `stands.dev` есть **только** у `dev` (нет `test_<имя>`); стенд с пустым `{}` блоком или без блока имеет только общие; совпадение имени в `common` и `stands.<s>` даёт ошибку старта; неизвестный стенд в `stands` даёт ошибку; `gen-rules` для хоста `dev` включает команды `common` и `stands.dev`, но не `stands.prod`.
- HTTP (подменяй `httpx.request`): `prod_login_form_check` собирает URL = `base_url` + `/login`; для `global` инструмент называется `global_sso_health` и существует один раз; стенд без нужного `endpoint` не получает инструмент.
- HTTP: `path` с параметром `../x`, `@evil.com`, `//evil.com`, пустой или с символами вне regex отклоняется; итоговый хост всегда равен хосту `base_url`; запрос не отправляется.
- HTTP: 301/302 не выполняется повторно, в выводе есть `Location`; `follow_redirects=False`, `verify` не равен `False`.
- HTTP: `expect` даёт PASS/FAIL по статусу, `body_contains`, `max_ms`; тело в ответе отсутствует при `return_body: false`; ошибка сети возвращает `RESULT: ERROR` без трассировки.
- HTTP: команда с `method: POST`, `auth`, `headers`, `body`, `via` отклоняется при старте.
- Стенд с именем `global` отклоняется при старте.
- Тест обёртки (bash, локально, через `SSH_ORIGINAL_COMMAND` и временный каталог в `MCP_SSH_HOME` с `rules.ro`): разрешённая команда проходит; `uptime; id`, `uptime && id`, `$(id)`, команда не из списка отклоняются с кодом 126.

## 11. План работ

1. Загрузка и валидация конфигов, проверка прав (п. 4, 5, 8.1).
2. Выбор хостов (`first/all/host`), сборка команды + юнит-тесты (п. 5, 7).
3. Функция `ssh()` и аудит (п. 7).
4. Регистрация инструментов, `hosts_list`, `stands_list` (п. 6).
5. Write-заявки и `write_result` (MCP-сторона, п. 15.2); затем исполнитель (п. 15.3–15.6).
6. CLI `check`, `list-hosts`, `gen-rules`, `key-path` (п. 9.1) + тесты.
7. `wrapper/mcp-wrapper`, `deploy.sh` (п. 9.2–9.4) + тест обёртки.
8. Реестр обработчиков (п. 5.2): вынеси SSH в `SshHandler`, добавь `HttpHandler` (п. 5.1, 7.1) + тесты; инструменты `endpoints_list`.
9. Параллельность `pick: all` (п. 4.0, 7.2) + тесты.
10. Docker (п. 14).
11. README и руководство оператора (п. 12).

Порядок важен: сначала SSH работает через реестр обработчиков, затем добавляется HTTP.

После каждого шага запускай `pytest`. Не переходи дальше, пока тесты не зелёные.

## 12. Руководство оператора (включить в README)

### 12.1 Добавить стенд

1. Сгенерируйте ключи: `ssh-keygen -t ed25519 -f ~/.ssh/mcp_<стенд>_ro -C "mcp <стенд> ro"`; при необходимости то же для `_rw`, с парольной фразой.
2. Соберите ключи хостов и **сверьте отпечатки** с известными вам: `ssh-keyscan -t ed25519 <адрес> >> conf/known_hosts.<стенд>`.
3. Добавьте стенд в `inventory.json`: блок `stands.<имя>` с `ssh_user`, `known_hosts`, `keys.ro` (и `keys.rw`, если нужен) и пустыми `hosts`.
4. Скопируйте каталог конфига в `/etc/mcp-ssh/` под root, права 0644, затем `python3 server.py check`.

Один стенд можно заполнить, не заполняя остальные: `rw` и дополнительные стенды необязательны.

### 12.2 Добавить хосты стенда

1. В `hosts` стенда добавьте запись: имя, `address`, список `tags` (роли, сколько угодно).
2. Добавьте ключ хоста в `known_hosts.<стенд>` (шаг 2 выше).
3. Выполните `wrapper/deploy.sh <стенд> <хост> --install-keys`: сгенерирует правила, скопирует обёртку и правила в `~/.mcp-ssh/` пользователя `ssh_user` и добавит ключи ro/rw в его `~/.ssh/authorized_keys` с принудительной командой. Пользователь должен уже существовать, скрипт его не создаёт.
4. Проверьте: `dev_hosts_list` показывает хост, `dev_uptime host=<хост>` отвечает.

Теги задают, какие команды доступны на хосте: например, `swarm_manager` включает `swarm_nodes_list`.

### 12.3 Добавить команду

Правьте только `commands.json` (под root). **Куда добавить:**
- команда нужна на всех стендах: в `common.read` или `common.write`;
- команда нужна только на одном стенде: в `stands.<стенд>.read` или `.write` (блок создайте, если его нет). Остальные стенды её не увидят, а на их хостах правило не появится.

Затем:

```
python3 server.py check
wrapper/deploy.sh <стенд>        # для каждого стенда, где нужна команда
# перезапустите MCP-сервер в клиенте (/mcp → reconnect)
```

Примеры:

**а) «проверить место на всех воркерах»** (одна команда, все хосты с тегом):
```json
"disk_free_workers": {"description": "Место на всех воркерах", "tags": ["swarm_worker"], "pick": "all", "cmd": "df -h /"}
```
Инструмент: `dev_disk_free_workers`.

**б) «проверить место на воркере 1»** (одна команда, конкретный хост):
```json
"disk_free_worker": {"description": "Место на одном воркере", "tags": ["swarm_worker"], "pick": "host", "cmd": "df -h /"}
```
Инструмент: `dev_disk_free_worker` с параметром `host=dev-sw-w1`. Оба варианта можно держать одновременно.

**в) команда с параметром** (значение проверяется regex, в ERE-синтаксисе):
```json
"logs": {"description": "Последние N строк журнала юнита", "tags": [], "pick": "host",
         "params": {"unit": "[a-z0-9][a-z0-9@._-]{0,63}", "n": "[0-9]{1,4}"},
         "cmd": "journalctl --no-pager -u {unit} -n {n}"}
```

**г) команда изменения:** добавляйте в блок `"write"`. Она появится как заявка, а выполнится только после вашего подтверждения на странице исполнителя (п. 15). Если команде нужен `sudo -n`, права `sudo` для `ssh_user` должен заранее выдать администратор сервера (проверка `sudo -l`); задание их не настраивает.

Не добавляйте команды, умеющие запускать другое: `find -exec`, `awk`, `less`, `vim`, `tar`, `git`, `systemctl edit`, `docker exec`. Через них можно выйти за рамки списка.

### 12.4 HTTP-проверки

**Добавить эндпоинт стенда:** в `inventory.json`, в `stands.<стенд>.endpoints`, добавьте `"app": {"base_url": "https://app.prod.example.com"}`; затем `server.py check`. Развёртывание на хосты не нужно.

**Добавить независимый эндпоинт:** то же, но в `global.endpoints`.

**Добавить проверку:** запись в `commands.json` с `"type": "http"` (см. 5.1). Правило выбора области:
- `"scope": "stand"`: инструмент появится у каждого стенда, где есть эндпоинт с таким именем (например, `prod_login_form_check`, `dev_login_form_check`);
- `"scope": "global"`: инструмент один (`global_sso_health`).

Пример «проверь, что форма авторизации на проде доступна»: `prod_login_form_check` → `RESULT: PASS status=200 body_contains "<form": ok`.

После правки: `python3 server.py check` и переподключение MCP-сервера. Скрипт `deploy.sh` не нужен: HTTP-команды хостам не интересны.

### 12.5 Как не перепутать сегмент

Имя стенда стоит в названии инструмента (`dev_...`), в начале описания и в первой строке вывода. Хост выбирается только из инвентаря этого стенда. На `prod` выдавайте только ключ `ro`, пока не нужна запись.

## 13. Первый запуск (вручную)

Сначала один стенд `dev`, один сервер, одна учётная запись, два ключа, `MCP_SSH_ALLOW_WRITABLE=1`.
Проверить: `dev_uptime` работает; хост другого стенда отклоняется; `dev_service_restart` создаёт заявку и ничего не выполняет, пока вы не введёте код на странице; `ssh -i prod_ro ... 'uptime; id'` отклонён (код 126), `logger`-записи `mcp-wrapper` (DENY/ALLOW) видны в syslog, если у пользователя есть доступ к журналу; `deploy.sh` печатает «ro-ключ ограничен».

## 14. Docker и Docker Compose

### 14.1 Важно про версию Python

Образ на **Python 3.8 использовать нельзя**: все версии пакета `mcp` (1.x и 2.x) требуют **Python 3.10 или новее**; сборка на 3.8 завершается ошибкой `No matching distribution found for mcp`. Python 3.8 к тому же снят с поддержки. В файлах ниже версия Python задаётся аргументом `PYTHON_VERSION` (по умолчанию **3.12**). Не меняй её на 3.8 и не пиши собственную реализацию протокола вместо `mcp`.

### 14.2 Файлы

Корень проекта (рядом с `server.py`):

`requirements.txt`
```
mcp>=1.27,<2
httpx>=0.27
```
Версия `mcp` ограничена веткой 1.x: код задания использует `mcp.server.fastmcp.FastMCP`. Если перейдёшь на 2.x, сначала проверь, что этот импорт и `add_tool` работают.

`Dockerfile`
```dockerfile
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
ARG MCP_UID=1000
ARG MCP_GID=1000
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g ${MCP_GID} mcp \
 && useradd -u ${MCP_UID} -g ${MCP_GID} -m -s /usr/sbin/nologin mcp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
USER mcp
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/server.py"]
```

`docker-compose.yml`
```yaml
services:
  safety-mcp-server:
    build:
      context: .
      args:
        PYTHON_VERSION: "${PYTHON_VERSION:-3.12}"
        MCP_UID: "${MCP_UID:-1000}"
        MCP_GID: "${MCP_GID:-1000}"
    image: safety-mcp-server:local
    stdin_open: true          # MCP работает по stdio
    read_only: true           # корневая ФС контейнера только для чтения
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    environment:
      MCP_SSH_CONF: /etc/mcp-ssh
    volumes:
      - ./conf:/etc/mcp-ssh:ro                          # inventory.json, commands.json, known_hosts.*
      - ./keys-ro:/keys:ro                              # приватные ключи ТОЛЬКО ro (rw-ключи у approver, п. 15)
      - ./queue/requests:/queue/requests                # заявки на запись (MCP пишет)
      - ./queue/results:/queue/results:ro               # результаты (пишет approver)
      - ./state:/home/mcp/.local/state/safety-mcp-server          # pending/ и audit.log (единственное, что пишется)
```

`.env` (рядом с compose; значения совпадают с `id -u` и `id -g` владельца ключей на хосте)
```
MCP_UID=1000
MCP_GID=1000
PYTHON_VERSION=3.12
```

### 14.3 Раскладка на хосте

```
safety-mcp-server/
  docker-compose.yml  Dockerfile  requirements.txt  server.py  .env
  conf/
    inventory.json  commands.json  known_hosts.dev  known_hosts.prod
  keys-ro/
    mcp_dev_ro  mcp_prod_ro                    # chmod 600, владелец = MCP_UID
  keys-rw/                                      # только для approver, владелец = APPROVER_UID
    mcp_dev_rw
  queue/requests  queue/results  approver-state/
  state/                                        # создаётся пустой, владелец = MCP_UID
```

Подготовка один раз: `mkdir -p state keys-ro keys-rw conf queue/requests queue/results approver-state && chmod 700 keys-ro keys-rw && chmod 600 keys-ro/* keys-rw/*`; владельцы: `state`, `keys-ro`, `queue/requests` = MCP_UID; `keys-rw`, `approver-state`, `queue/results` = APPROVER_UID.

### 14.4 Пути в `inventory.json` внутри контейнера

Пути ключей **контейнерные**, а не пути хоста:
```json
"keys": {"ro": "/keys/mcp_dev_ro", "rw": "/keys-rw/mcp_dev_rw"},
"known_hosts": "known_hosts.dev"
```
`known_hosts` и `ca_file` берутся относительно `/etc/mcp-ssh` (то есть `./conf`).

### 14.5 Запуск

Сборка: `docker compose build`.

Подключение к Claude Code (stdio, без tty):
```
claude mcp add safety-mcp-server -- docker compose -f /путь/к/safety-mcp-server/docker-compose.yml run --rm -T safety-mcp-server
```

Одобрение записи делает отдельный сервис `approver` (п. 15): страница открывается автоматически, код вводится с телефона. Команды `approve` больше нет. Настройка OTP: `docker compose run --rm approver enroll` (один раз, вручную, до подключения агента).
Каталог `state/` общий для обоих запусков, поэтому заявка видна.

Проверки без запуска MCP: `docker compose run --rm safety-mcp-server check`, `docker compose run --rm safety-mcp-server list-hosts dev`.

`deploy.sh` (п. 9.4) запускай **на хосте**, не в контейнере: ему нужен ваш обычный SSH-доступ к `ssh_user`, которого у контейнера быть не должно. Для него нужен установленный `python3` и те же `conf/` и `keys/` (переменные `MCP_SSH_CONF=$PWD/conf`).

### 14.6 Критерии приёмки для Docker

- `docker compose build` проходит на Python 3.12; на `PYTHON_VERSION=3.8` сборка падает на `pip install` (ожидаемо).
- В контейнере: `ssh -V` работает; `python -c "from mcp.server.fastmcp import FastMCP"` проходит; `id` показывает пользователя `mcp`.
- `docker compose run --rm safety-mcp-server check` проходит при `conf/` смонтированном `:ro` без `MCP_SSH_ALLOW_WRITABLE`.
- Запись в `/etc/mcp-ssh` и `/keys` из контейнера невозможна (`Read-only file system`); запись в `/app` и корень тоже.
- Ключ с правами `0644` в `keys/` приводит к понятной ошибке (ssh отклоняет), а не молчаливому провалу.

## 15. Подтверждение операций записи (approver, TOTP, без интернета)

Цель: операция `write` выполняется только после того, как **человек** ввёл 6-значный код из приложения-аутентификатора. Агент (ИИ) этот код получить не может. Интернет не нужен.

### 15.1 Принципы

- Агент умеет всё, что умеет его пользователь на машине: запускать команды, делать `curl`, читать файлы, видеть картинки и скриншоты. Поэтому **капча/картинка с кодом не годится**, а `/dev/tty` и «кнопка на странице» тоже.
- Нужен секрет, который есть только у человека (телефон) и у исполнителя. Этот секрет **не должен быть доступен** MCP-контейнеру и пользователю, под которым работает агент.
- Поэтому два сервиса: **MCP-сервер** (его видит агент; ключа `rw` нет) и **approver** (свой контейнер/пользователь, свои тома; есть ключ `rw`, секрет TOTP и веб-страница).

### 15.2 MCP-сторона

- `S_<write>` проверяет параметры, формирует итоговую команду, вычисляет `sha256`, пишет файл `queue/requests/<id>.json` (`id` = 32 hex случайных символов): `stand, host, role=rw, cmd, cmd_sha256, created, expires = +10 мин`. Возвращает агенту: `id`, текст «Подтвердите операцию на странице исполнителя (откроется автоматически)», стенд, хост и команду.
- `write_result(id)` читает `queue/results/<id>.json`: `pending | approved-ok | approved-failed | denied | expired`, вывод и код возврата.
- Права на каталоги очереди: MCP пишет только в `queue/requests/`, читает `queue/results/` (read-only mount). Approver читает `requests/`, пишет только `results/`.
- Если исполнитель не настроен (в `queue/results/_status.json` `enrolled=false` или файла нет), write-инструменты отвечают: «OTP не настроен. Выполните `docker compose run --rm approver enroll` в своём терминале», остальные инструменты (read, http) работают.
- **MCP никогда не выводит QR-код, секрет и `otpauth://`-ссылку**: всё, что MCP возвращает, видит агент.

### 15.3 Первичная настройка OTP (`approver enroll`)

Запускается человеком **один раз** в своём терминале, **до подключения агента**:

1. Если секрет уже есть, команда отказывается (код 3).
2. Генерирует 20 случайных байт (`secrets.token_bytes(20)`), кодирует в base32.
3. Формирует ссылку:
   `otpauth://totp/safety-mcp-server:<имя>?secret=<base32>&issuer=safety-mcp-server&algorithm=SHA1&digits=6&period=30`
4. Рисует **QR-код прямо в терминале** (библиотека `segno`, чистый Python, без зависимостей и без интернета: `segno.make(uri).terminal(compact=True)`) и печатает ссылку текстом на случай, если QR не сканируется.
5. Просит ввести первый код из приложения; при верном коде сохраняет секрет в `/data/otp.json` (права 0600, том `approver-state`) и пишет `queue/results/_status.json` `{"enrolled": true}`. При трёх неверных вводах секрет не сохраняется.
6. **Сброс**: `approver enroll --reset` требует действующий код текущего секрета. Без кода сбросить нельзя (иначе агент заменил бы секрет своим).

Подходящие приложения (работают без интернета и без облака): Aegis (Android), FreeOTP, 2FAS, Raivo. Не включайте облачную синхронизацию секрета (например, в учётную запись Google), если секрет не должен покидать телефон.

### 15.4 Веб-страница одобрения

Approver слушает `0.0.0.0:8765` внутри контейнера, на хосте публикуется только `127.0.0.1:8765`.

- Страница `/r/<id>` показывает: стенд (крупно, цветом), хост, роль, **точную команду**, время истечения, текущее время сервера.
- Форма: поле «6 цифр» и кнопки «Разрешить» и «Отклонить» (отклонение кода не требует).
- **Три попытки** ввода на заявку; после третьей неверной заявка получает `denied` и дальше не принимает коды.
- Глобальный лимит: 5 неверных кодов за 10 минут по всем заявкам, затем блокировка на 10 минут.
- Принятый код **одноразовый** (запоминается последний использованный шаг времени). Допуск ±1 шаг (30 с). Сравнение через `hmac.compare_digest`.
- Заявка действует 10 минут, исполняется **один раз**, только если `sha256(cmd)` совпадает с записанным и статус `pending`.
- Защита страницы: проверка заголовка `Host` (только `127.0.0.1:8765` и `localhost:8765`), CSRF-токен в форме, без CORS, `Cache-Control: no-store`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'self'`, без внешних ресурсов (всё встроено).
- После верного кода approver выполняет `ssh` ключом `rw` (та же функция `ssh()` и те же параметры, что в п. 7) и пишет результат в `queue/results/<id>.json`.

### 15.5 Автооткрытие браузера

Из контейнера хостовой браузер не открыть. Поэтому на хосте работает маленький скрипт `approver/open-new-requests.sh` (запуск через `systemd --user` или вручную):

- каждую секунду смотрит `queue/requests/` и для новой заявки вызывает `xdg-open http://127.0.0.1:8765/r/<id>` (на macOS `open`);
- запоминает уже открытые `id`, не открывает дважды;
- скрипт только открывает страницу и ничего не одобряет.

Если агент и ваш браузер на **разных** машинах, автооткрытие не работает: используйте ссылку из ответа агента (порт нужно пробросить `ssh -L 8765:127.0.0.1:8765`).

### 15.6 Работа без интернета и время

- TOTP требует, чтобы часы сервера не расходились с часами телефона больше, чем на ±30 секунд. Используйте внутренний NTP-сервер (или настройте `chrony` на локальный источник). Если код не принимается, страница показывает серверное время для сверки.
- Образы собираются там, где есть интернет, и переносятся (`docker save` / `docker load`) либо ставятся из каталога колёс (`pip install --no-index --find-links wheels/`). В `requirements.txt` добавить `segno` (зафиксировать версию).

### 15.7 Критерии приёмки

- Без `otp.json` write-инструменты возвращают «OTP не настроен», read/http работают; `_status.json` отсутствует или `enrolled=false`.
- `enroll` не запускается повторно; `enroll --reset` без верного кода отказывает; QR и ссылка не попадают ни в какой ответ MCP-инструментов и ни в какой файл в `queue/`.
- Верный код: заявка выполняется один раз; повтор того же кода отклоняется; код с дрейфом больше ±1 шага отклоняется.
- Три неверных кода: заявка `denied`; новый верный код её не оживляет; 5 неверных за 10 минут блокируют все заявки.
- Заявка с изменённым `cmd` (хэш не совпадает), истёкшая заявка, заявка со статусом не `pending`: не выполняются.
- Запрос страницы с чужим заголовком `Host` отклоняется; POST без CSRF-токена отклоняется.
- В MCP-контейнере нет файлов `keys-rw` и `otp.json` (проверка `ls`), а запись в `queue/results/` из MCP невозможна.
- `open-new-requests.sh` открывает страницу один раз на заявку.

### 15.8 Важное ограничение

Защита работает, только если у агента **нет доступа** к тому, где живёт approver: каталогу `approver-state`, ключам `keys-rw`, Docker-сокету и возможности запускать `docker compose` для `approver`. В настройках Claude Code запретите `Bash(docker:*)` и чтение этих каталогов (`permissions.deny`), а режим bypass отключите (`disableBypassPermissionsMode`).

### 15.9 Добавление в `docker-compose.yml`

```yaml
  approver:
    build:
      context: .
      dockerfile: Dockerfile.approver     # тот же python:3.12-slim + openssh-client + segno, ENTRYPOINT ["python", "/app/approver.py"]
      args:
        PYTHON_VERSION: "${PYTHON_VERSION:-3.12}"
        MCP_UID: "${APPROVER_UID:-1001}"
        MCP_GID: "${APPROVER_GID:-1001}"
    image: safety-mcp-server-approver:local
    read_only: true
    tmpfs: ["/tmp"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    ports:
      - "127.0.0.1:8765:8765"
    environment:
      MCP_SSH_CONF: /etc/mcp-ssh
    volumes:
      - ./conf:/etc/mcp-ssh:ro
      - ./keys-rw:/keys-rw:ro               # ТОЛЬКО здесь
      - ./approver-state:/data              # otp.json, аудит исполнителя
      - ./queue/requests:/queue/requests:ro
      - ./queue/results:/queue/results
```
В сервис MCP добавить тома `./queue/requests:/queue/requests` и `./queue/results:/queue/results:ro`, а ключи монтировать только `./keys-ro:/keys:ro`. Для `approver` используйте **другой UID**, чем для MCP-сервера, чтобы файлы `approver-state` и `keys-rw` были недоступны пользователю MCP.

## 16. Дополнения (реализовано в 0.1.0)

- **Имена инструментов**: `<стенд>_ro_<команда>` для чтения и `<стенд>_rw_<команда>` для изменений (вместо `S_C`).
  Хосты: `<стенд>_ro_hosts_list`. Общие инструменты: `about`, `stands_list`, `rw_approve`, `rw_result`
  (`rw_result` заменяет `write_result`).
- **Обязательная инструкция для агента**: текст правил передаётся в `instructions` MCP-сервера при подключении и
  возвращается инструментом `about` (в описании помечен как обязательный к первому вызову).
- **Код OTP через агента**: флаг стенда `"agent_otp": true` в `inventory.json` (по умолчанию `false`). Человек
  сообщает код агенту, агент вызывает `rw_approve(id, code)`. MCP пишет файл `queue/requests/<id>.otp-<n>.json`,
  approver проверяет код (те же 3 попытки, одноразовость, глобальная блокировка) и отвечает в
  `queue/results/<id>.otp-<n>.json`. Для стендов без `agent_otp` approver такие коды отклоняет.
- **Описание команды** (`description`) необязательно; в описании инструмента всегда показан шаблон команды.
- **Работа без OTP**: если approver ни разу не настраивался, `ro` работают, а `rw` и `rw_approve` возвращают
  ошибку с командой настройки (`MCP_ENROLL_HINT`). `stands_list` показывает, настроен ли OTP.
- **Пробный запуск** `dry_run` (bool, по умолчанию false) у всех ro/rw-инструментов: показывает хосты, итоговую
  команду и совпадение с правилом обёртки; SSH не выполняется, заявка не создаётся.
