<div align="center">

# safety-mcp-server

**Безопасный MCP-доступ ИИ-агента к серверам по SSH: только заранее описанные команды,
изменения — только с одноразовым кодом от человека.**

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-streamable--http-6E56CF)
![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)
![Offline](https://img.shields.io/badge/internet-not%20required-success)
![License](https://img.shields.io/badge/license-Apache%202.0-blue)

</div>

---

## Содержание

- [Зачем](#зачем)
- [Возможности](#возможности)
- [Как это работает](#как-это-работает)
- [Быстрый старт](#быстрый-старт)
- [Windows](#windows)
- [Инструменты для агента](#инструменты-для-агента)
- [Подтверждение изменений](#подтверждение-изменений)
- [Конфигурация](#конфигурация)
- [Добавление команд, стендов и хостов](#добавление-команд-стендов-и-хостов)
- [Модель безопасности](#модель-безопасности)
- [Разработка](#разработка)
- [Структура проекта](#структура-проекта)
- [Ограничения и планы](#ограничения-и-планы)
- [Лицензия](#лицензия)

## Зачем

Агенту полезно смотреть на серверы: место на дисках, статусы сервисов, логи. Давать ему shell опасно:
одна ошибка или подсказка-инъекция, и он выполнит что угодно. `safety-mcp-server` даёт агенту **список
именованных команд** вместо shell и требует **6-значный код из FreeOTP** для любого изменения.

## Возможности

- 🔒 **Только описанные команды.** Агент передаёт имя инструмента и параметры; параметры проверяются по шаблону.
- 🧱 **Двойная проверка.** Помимо MCP, команду проверяет обёртка на самом сервере (forced command в
  `authorized_keys`), правила для неё генерируются из того же `commands.json`.
- 🏷️ **Стенды и теги.** `dev`, `test`, `prod`, … в одном конфиге; хост выбирается по тегам ролей
  (`swarm_manager`, `swarm_worker`, …), стенд зашит в имя инструмента.
- ⚡ **Параллельно.** Команда на всех хостах с тегом выполняется параллельно (`max_parallel`).
- ✅ **Изменения по коду OTP.** Запись выполняет отдельный сервис `approver` с отдельным ключом после ввода
  кода FreeOTP на странице или через агента (если разрешено для стенда).
- 🌐 **HTTP-проверки.** `GET`/`HEAD` к адресам из конфига: код ответа, наличие текста, время; короткий или полный
  ответ (`mode=short|all`); привязанные к стенду или общие (`global`).
- 🧪 **Пробный запуск.** `dry_run=true` показывает хосты и точную команду, ничего не отправляя на сервер.
- 📴 **Без интернета.** TOTP, QR-код и проверки работают офлайн.
- 📝 **Аудит.** Каждый вызов пишется в журнал MCP, approver и в syslog сервера.

## Как это работает

```mermaid
flowchart LR
    A[ИИ-агент] -- "&lt;стенд&gt;_ro_* (HTTP + токен)" --> M[MCP-сервер<br/>127.0.0.1:8766<br/>ключ ro]
    A -- "&lt;стенд&gt;_rw_*" --> M
    M -- "ssh, ключ ro" --> W
    M -- "заявка" --> Q[(queue/requests)]
    H((Человек<br/>FreeOTP)) -- "код на странице" --> P[approver<br/>ключ rw]
    H -. "код в чате → rw_approve" .-> A
    Q --> P
    P -- "ssh, ключ rw" --> W[safety-wrapper<br/>на сервере]
    W -- "только команды из rules.ro / rules.rw" --> S[(сервер)]
```

| Компонент | Где работает | Что делает |
|---|---|---|
| `server.py` | контейнер `safety-mcp-server` | MCP-сервер на `127.0.0.1:8766/mcp` (Bearer-токен) и CLI администратора |
| `approver.py` | контейнер `approver` | Страница подтверждения на `127.0.0.1:8765`, проверка TOTP, выполнение rw |
| `wrapper/safety-wrapper` | `~/safety-support/` на каждом хосте | Принудительная команда для ключей MCP: пропускает только команды из правил |
| `wrapper/deploy.sh` | машина администратора | Генерирует правила и раскладывает обёртку на хосты |
| `wrapper/open-new-requests.sh` | машина администратора | Открывает страницу подтверждения при новой заявке |

MCP-сервер и approver — один образ, но **разные пользователи и тома**: ключ `rw` и секрет OTP есть только
у approver.

## Быстрый старт

Основной вариант: данные (конфиг, ключи, очередь, состояние) лежат в каталоге **`local/`** внутри проекта.
Он в `.gitignore` и `.dockerignore`, поэтому не попадёт ни в git, ни в образ. Все команды `docker compose`
запускаются из корня проекта с параметрами `--project-directory local -f docker-compose.yml`:

```bash
alias dc='docker compose --project-directory local -f docker-compose.yml'   # для удобства
```

<details>
<summary>Альтернатива: данные рядом с <code>docker-compose.yml</code></summary>

Создайте те же каталоги в корне проекта (без префикса `local/`) и запускайте `docker compose` без
`--project-directory`. Либо укажите любой другой каталог: `--project-directory ~/safety-mcp-data`.
Если нужного каталога нет, compose завершится ошибкой, а не создаст пустой.
</details>

### 1. Образ

```bash
docker compose build
# или без интернета: docker load < dist/safety-mcp-server-0.2.0-<коммит>.tar.gz
```

### 2. Ключи и каталоги

Права на файлы ключей на машине с Docker не важны: контейнер при старте копирует ключи из `keys-ro`/`keys-rw`
в память с правами 600. Владелец `keys-rw` (`APPROVER_UID`) нужен только для того, чтобы вы и агент не могли
прочитать rw-ключ.

Проще всего скриптом: он создаёт `local/` из `conf.example/` и `.env.example`, генерирует ключи, токен MCP
(`local/mcp-token`) и ставит права.
Существующие файлы, ключи и секрет OTP он никогда не перезаписывает, поэтому запускать его можно повторно,
например чтобы добавить ключи нового стенда.

```bash
./init-local.sh --keys <стенд>          # можно несколько: --keys dev --keys prod
```

<details>
<summary>То же вручную</summary>

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/safety_<стенд>_ro
ssh-keygen -t ed25519 -N "" -f ~/.ssh/safety_<стенд>_rw

mkdir -p local/{conf,keys-ro,keys-rw,state,queue/requests,queue/results,approver-state}
cp ~/.ssh/safety_<стенд>_ro* local/keys-ro/
mv ~/.ssh/safety_<стенд>_rw local/keys-rw/ && cp ~/.ssh/safety_<стенд>_rw.pub local/keys-rw/
chmod 700 local/keys-ro local/keys-rw && chmod 600 local/keys-ro/safety_*_ro local/keys-rw/safety_*_rw
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > local/mcp-token)

# каталоги approver принадлежат APPROVER_UID (1001), остальные — MCP_UID (1000)
docker run --rm --user 0 --entrypoint chown -v "$PWD/local:/w" safety-mcp-server:0.2.0 \
  -R 1001:1001 /w/keys-rw /w/approver-state /w/queue/results
```

</details>

Приватный rw-ключ лежит только в `local/keys-rw/` и недоступен вашему пользователю. Так и задумано.

### 3. Конфиг

Скопируйте [`conf.example/`](conf.example/) в `local/conf/`, впишите хосты и добавьте ключи хостов:

```bash
cp conf.example/*.json local/conf/
ssh-keyscan -t ed25519 <адрес> >> local/conf/known_hosts.<стенд>   # сверьте отпечаток!
```

### 4. Обёртка на хостах

Выполняется вашим обычным доступом к `ssh_user`. Пользователей скрипт не создаёт, системные файлы не трогает.

```bash
DEPLOY_SSH_KEY=~/.ssh/<ваш_ключ> DEPLOY_KEY_DIR=~/.ssh \
  wrapper/deploy.sh <стенд> --install-keys
```

В конце скрипт проверяет, что ro-ключ ограничен: произвольная команда должна получить отказ с кодом 126.

На сервере всё лежит в одном каталоге `~/safety-support/` пользователя `ssh_user`; слова `mcp` в именах нет:

```text
~/safety-support/
├── safety-wrapper        # обёртка (принудительная команда ключей)
├── rules.ro, rules.rw    # разрешённые команды
└── backup/               # бэкапы authorized_keys перед каждым изменением
```

Вне каталога — только строки в `~/.ssh/authorized_keys` (sshd читает ключи только оттуда):
`command="/home/<user>/safety-support/safety-wrapper ro",restrict ssh-ed25519 … safety <стенд> ro`.
Если строка с тем же ключом уже есть, `--install-keys` заменяет её, а не дублирует.

### 5. FreeOTP (один раз, в своём терминале)

```bash
docker compose --project-directory local -f docker-compose.yml run --rm approver enroll --account <имя>
```

Отсканируйте QR-код в FreeOTP («+», затем сканирование) и введите показанный код.

> [!NOTE]
> Часы машины и телефона должны совпадать с точностью до `APPROVER_TOTP_SKEW` секунд (по умолчанию 50).
> Если код не принимается, `enroll` и страница подсказывают, на сколько расходятся часы. Проверка:
> `timedatectl | grep synchronized` должно показывать `yes`.

> [!IMPORTANT]
> Запускайте `enroll` **в своём терминале**, а не через агента и не через `!` в чате: кто видел QR-код,
> тот может генерировать коды.

### 6. Запуск

```bash
docker compose --project-directory local -f docker-compose.yml up -d     # MCP-сервер и approver
wrapper/open-new-requests.sh local/queue/requests &                       # автооткрытие страницы подтверждения
```

Оба сервиса работают в фоне и перезапускаются вместе с Docker. Порты открыты только на `127.0.0.1`:

| Адрес | Сервис | Кто подключается |
|---|---|---|
| `http://127.0.0.1:8766/mcp` | MCP-сервер | агент (клиент MCP) с заголовком `Authorization: Bearer <токен>` |
| `http://127.0.0.1:8765` | страница подтверждения | только человек в браузере |

Порт MCP меняется переменной `MCP_HTTP_PORT` в `local/.env`.

### 7. Подключение агента

**Токен** лежит в файле `local/mcp-token`. Его создаёт `init-local.sh` (`./init-local.sh` без параметров
создаст токен, если его нет, и не тронет остальное). Без токена MCP-сервер не запускается; запрос
без токена или с неверным токеном получает `401`.

opencode (`opencode.json` в проекте или `~/.config/opencode/opencode.json`), путь к токену абсолютный:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "safety-mcp-server": {
      "type": "remote",
      "url": "http://127.0.0.1:8766/mcp",
      "headers": { "Authorization": "Bearer {file:/path/to/safety-mcp-server/local/mcp-token}" },
      "oauth": false,
      "enabled": true
    }
  }
}
```

Проверка: `opencode mcp list` должен показать `✓ safety-mcp-server connected`.

<details>
<summary>Claude Code</summary>

```bash
claude mcp add --transport http safety-mcp-server http://127.0.0.1:8766/mcp \
  --header "Authorization: Bearer $(cat local/mcp-token)"
```

</details>

Новый токен: остановите сервер, удалите `local/mcp-token`, запустите `./init-local.sh` и
`docker compose ... up -d`, обновите токен у клиентов.

> [!WARNING]
> Не публикуйте порт 8766 на `0.0.0.0`: кто знает токен, тот выполняет ro-команды и создаёт заявки.
> Если агент работает на другой машине, подключайтесь через SSH-туннель:
> `ssh -N -L 8766:127.0.0.1:8766 <машина с MCP>`. TLS сервер не делает.

MCP работает и **без OTP**: команды `ro` доступны, `rw` возвращают ошибку с командой настройки
(текст задаётся переменной `MCP_ENROLL_HINT` в `local/.env`).

## Windows

Нужен Docker Desktop (WSL 2). Bash, ssh и настройка прав на файлы **не нужны**. Ключи — обычные файлы
в папке: при старте контейнер сам копирует их в память с правами 600. На NTFS права всегда выглядят как 0777,
и ssh иначе отказался бы от такого ключа. Секрет OTP хранится в томе Docker `safety-approver-state`, а не в папке
Windows. Том создаётся автоматически.

Все команды выполняются в PowerShell из корня проекта. Если система запрещает запуск скриптов, сначала выполните
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**1. Образ**

```powershell
docker compose build                                   # или без интернета:
docker load -i dist\safety-mcp-server-<версия>.tar.gz
```

**2. Каталог данных, токен и ключи**

```powershell
.\windows\init.ps1 -Keys vpn          # local\, токен local\mcp-token, ключи safety_vpn_ro / safety_vpn_rw
```

Появятся папки `local\keys-ro` и `local\keys-rw`. Если ключи уже есть, просто положите их туда
(`safety_<стенд>_ro` и `.pub` в `keys-ro`, `safety_<стенд>_rw` и `.pub` в `keys-rw`). Ключи должны быть без
пароля. Папки можно вынести в другое место, указав путь в `local\.env`:

```ini
SAFETY_KEYS_RO_DIR=C:\safety\keys-ro
SAFETY_KEYS_RW_DIR=C:\safety\keys-rw
```

После замены ключей перезапустите сервисы: `.\windows\compose.ps1 up -d --force-recreate`.

**3. Конфиг и ключи хостов**

Заполните `local\conf\inventory.json` и `commands.json` (см. [Конфигурация](#конфигурация)). Пути ключей указываются
как в контейнере: `/keys/safety_vpn_ro`, `/keys-rw/safety_vpn_rw`. Ключ хоста:

```powershell
.\windows\keyscan.ps1 -Stand vpn -Address 10.0.0.5      # покажет отпечаток: сверьте его с сервером
```

Не используйте `ssh-keyscan ... >> known_hosts`: в PowerShell `>>` пишет файл в UTF-16, и ssh его не прочитает.

**4. Обёртка на хостах**

```powershell
.\windows\deploy.ps1 -Stand vpn -DeployKey $HOME\.ssh\id_ed25519 -InstallKeys
```

`-DeployKey` — ваш обычный ключ доступа к `ssh_user`. Он только читается. Если у ключа есть пароль, его спросит
`ssh-add`. Скрипт запускает `wrapper/deploy.sh` в контейнере.

**5. FreeOTP** (в своём окне PowerShell, не через агента)

```powershell
.\windows\compose.ps1 run --rm approver enroll --account <имя>
```

**6. Запуск и подключение**

```powershell
.\windows\compose.ps1 up -d
Start-Process powershell -WindowStyle Minimized -ArgumentList '-File', '.\windows\open-new-requests.ps1'
```

`windows\compose.ps1` — это `docker compose` с `local\` и `docker-compose.windows.yml`; любые аргументы передаются
как есть (`logs -f`, `down`, `ps`). Подключение агента такое же, как в [п. 7](#7-подключение-агента). Путь к токену
в `opencode.json` пишется с прямыми слешами:
`"Bearer {file:C:/path/to/safety-mcp-server/local/mcp-token}"`.

> [!WARNING]
> В Windows все файлы пользователя, включая rw-ключ и токен, может прочитать любая программа этого пользователя.
> Это относится и к агенту, если ему разрешены команды или чтение вне проекта. Вынесите `keys-rw` за пределы
> рабочего каталога агента (`SAFETY_KEYS_RW_DIR`) и запретите агенту shell и Docker. Строже — запускать агента
> под другой учётной записью Windows.

## Инструменты для агента

Правила работы агент получает **при подключении** (поле `instructions` MCP) и в инструменте `about`.

| Инструмент | Что делает |
|---|---|
| `about` | Правила работы: имена, порядок подтверждения, список стендов. Вызывается первым |
| `stands_list` | Стенды, разрешена ли запись, можно ли передавать код через агента, настроен ли OTP |
| `<стенд>_ro_hosts_list` | Хосты стенда с тегами (`tag` — фильтр) |
| `<стенд>_ro_<команда>` | Чтение. Выполняется сразу (SSH или HTTP) |
| `<стенд>_ro_endpoints_list` | HTTP-эндпоинты стенда |
| `global_ro_<команда>`, `global_ro_endpoints_list` | HTTP-проверки, не привязанные к стенду |
| `<стенд>_rw_<команда>` | Изменение. Создаёт заявку, ничего не выполняя |
| `rw_approve(id, code)` | Передать код OTP, который сообщил человек (если у стенда `agent_otp`) |
| `rw_result(id)` | Результат заявки |

У всех `ro` и `rw` команд есть параметры:

| Параметр | Когда |
|---|---|
| `host` | Обязателен для команд с `pick: host`, необязателен для остальных. Несколько хостов — через запятую: `h1,h2,h3` |
| `dry_run` | `true` — показать хосты/URL и итоговую команду; ничего не отправляется |
| `mode` | Только HTTP: `short` (по умолчанию) — PASS/FAIL, код и время; `all` — полный ответ: заголовки и тело |
| параметры команды | По шаблонам из `commands.json` |

<details>
<summary>Пример ответов</summary>

```text
=== dev_ro_disk_free_opt
[stand=dev host=dev-app1 role=ro] rc=0
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1        58G  4.1G   54G   8% /

=== dev_rw_file_create {"host": "dev-app1", "path": "safety-sandbox/x.txt", "dry_run": true}
[stand=dev role=rw] ПРОБНЫЙ ЗАПУСК (dry_run): на сервер ничего не отправлено
хосты (1): dev-app1 (203.0.113.10)
команда: touch -- safety-sandbox/x.txt
правило серверной обёртки rules.rw: совпадает
заявка НЕ создана.

=== prod_ro_http_server_is_life
[stand=prod endpoint=app GET https://app.example.com/]
RESULT: PASS status=200 time=510ms size=18234B
- status in [200]: ok
```
</details>

## Подтверждение изменений

```mermaid
sequenceDiagram
    participant A as Агент
    participant M as MCP
    participant P as approver
    participant H as Человек
    A->>M: dev_rw_file_create(path)
    M->>M: проверка параметров, выбор хоста
    M-->>A: заявка id, точная команда
    A->>H: стенд, хост, команда — подтвердите
    alt на странице
        H->>P: код FreeOTP на 127.0.0.1:8765/r/id
    else через агента (agent_otp: true)
        H->>A: код
        A->>M: rw_approve(id, code)
        M->>P: код через очередь
    end
    P->>P: проверка TOTP, хэша команды, правил
    P->>P: ssh ключом rw
    P-->>A: результат (rw_approve / rw_result)
```

| Правило | Значение |
|---|---|
| Несколько хостов | Одна заявка и **один код** на все хосты (`host="h1,h2,h3"` или `pick: all`); выполняется параллельно, результат по каждому хосту |
| Попыток на заявку | 3, затем заявка отклоняется |
| Код | Одноразовый, допуск расхождения часов `APPROVER_TOTP_SKEW` (по умолчанию 50 с) |
| Срок заявки | 10 минут |
| Глобальная блокировка | 5 неверных кодов за 10 минут → 10 минут без подтверждений |
| Повторная проверка | Approver сверяет хэш команды и правила `write`, адрес хоста берёт из своего инвентаря |

> [!WARNING]
> При подтверждении через агента команду вам описывает агент. Сверяйте её с вызовом инструмента в
> интерфейсе клиента или открывайте страницу. Для prod держите `agent_otp: false`.

## Конфигурация

Каталог `local/conf/` (в контейнере `/conf`) монтируется **только для чтения**. Если конфиг доступен
на запись, сервер не стартует (для отладки: `SAFETY_ALLOW_WRITABLE=1`).

### `inventory.json`

```json
{
  "settings": {"max_parallel": 5, "max_hosts": 20},
  "stands": {
    "dev": {
      "ssh_user": "opsuser",
      "known_hosts": "known_hosts.dev",
      "keys": {"ro": "/keys/safety_dev_ro", "rw": "/keys-rw/safety_dev_rw"},
      "agent_otp": true,
      "hosts": {
        "dev-sw-m1": {"address": "10.10.0.11", "tags": ["swarm_manager", "docker"]},
        "dev-sw-w1": {"address": "10.10.0.21", "tags": ["swarm_worker", "docker"]}
      }
    }
  }
}
```

| Поле | Описание |
|---|---|
| `settings.max_parallel` | Сколько хостов одновременно при `pick: all` (1–50, по умолчанию 5); можно переопределить у стенда |
| `settings.max_hosts` | Максимум хостов в одном вызове (1–200, по умолчанию 20) |
| `ssh_user` | Существующий пользователь на хостах |
| `keys.ro` / `keys.rw` | Пути к ключам **внутри контейнера**; `rw` необязателен — без него у стенда нет rw-команд |
| `agent_otp` | Разрешить передачу кода через агента (`rw_approve`), по умолчанию `false` |
| `hosts.*.tags` | Произвольные теги ролей, количество не ограничено |

#### HTTP-эндпоинты

```json
"stands": {"prod": {"endpoints": {
  "app":      {"base_url": "https://app.example.com"},
  "internal": {"base_url": "https://intranet.example.local", "tls_verify": false}
}}},
"global": {"endpoints": {"sso": {"base_url": "https://sso.example.com"}}}
```

| Поле | Описание |
|---|---|
| `base_url` | Только `схема://хост[:порт]`, без пути и учётных данных; `https://`, `http://` — с `allow_http: true` |
| `tls_verify` | `false` — не проверять сертификат (самоподписанные и внутренние); по умолчанию `true`. В ответе помечается `tls_verify=off` |
| `ca_file` | Свой CA (файл в каталоге конфига) |

Стенд может состоять только из эндпоинтов, без хостов и ключей.

### `commands.json`

```json
{
  "common": {
    "read": {
      "disk_free_all": {"description": "Место по всем точкам монтирования", "pick": "all", "cmd": "df -h"}
    },
    "write": {
      "file_create": {
        "pick": "host",
        "params": {"path": "safety-sandbox(/[A-Za-z0-9_][A-Za-z0-9._-]{0,63}){1,4}"},
        "cmd": "touch -- {path}"
      }
    }
  },
  "stands": {
    "dev": {"read": {"swarm_nodes_list": {"tags": ["swarm_manager"], "cmd": "sudo -n docker node ls"}}}
  }
}
```

| Поле | Описание |
|---|---|
| `common` / `stands.<стенд>` | Команды для всех стендов / только для одного; блок стенда может быть пустым |
| `description` | Необязательно; агент видит описание и шаблон команды |
| `tags` | Хост должен иметь все теги; `[]` — любой |
| `pick` | `first` — первый подходящий, `all` — все параллельно, `host` — агент указывает хост |
| `params` | Имя → regex в синтаксисе POSIX ERE (`[0-9]`, не `\d`) или объект `{"pattern": "...", "description": "..."}`: описание и шаблон агент видит в схеме параметра |
| `cmd` | Шаблон с `{param}`; запрещены `; \| & $ \` < > * ' "` |

HTTP-команда (только в блоке `read`):

```json
"login_form_check": {
  "type": "http", "scope": "stand", "endpoint": "app",
  "method": "GET", "path": "/login",
  "expect": {"status": [200], "body_contains": "<form", "max_ms": 3000}
}
```

| Поле | Описание |
|---|---|
| `scope` | `stand` — инструмент у каждого стенда, где есть такой `endpoint`; `global` — один инструмент `global_ro_<имя>` |
| `method` | `GET` или `HEAD` |
| `path` | Начинается с `/`; `{param}` подставляется с URL-кодированием |
| `expect` | `status` (список кодов), `body_contains`, `max_ms`; без `expect` — достаточно любого ответа |

## Добавление команд, стендов и хостов

| Что | Где | Дальше |
|---|---|---|
| Команда для всех стендов | `local/conf/commands.json` → `common.read` / `common.write` | `wrapper/deploy.sh <стенд>` для каждого стенда |
| Команда для одного стенда | `local/conf/commands.json` → `stands.<стенд>` | `wrapper/deploy.sh <стенд>` |
| Хост | `local/conf/inventory.json` → `stands.<стенд>.hosts`, ключ хоста в `known_hosts.<стенд>` | `wrapper/deploy.sh <стенд> <хост> --install-keys` |
| Стенд | `local/conf/inventory.json` → `stands`, ключи `safety_<стенд>_ro/rw` | `wrapper/deploy.sh <стенд> --install-keys` |

После изменений переподключите MCP в клиенте (`/mcp` → reconnect). Проверка конфига:
`docker compose --project-directory local -f docker-compose.yml run --rm safety-mcp-server check`.

> [!CAUTION]
> Не добавляйте команды, умеющие запускать другие: `find -exec`, `awk`, `less`, `vim`, `tar`, `git`,
> `systemctl edit`, `docker exec`. Команды, затрагивающие `safety-support` или `.ssh`, обёртка отклоняет
> всегда, даже если правило их разрешает.

## Модель безопасности

| Угроза | Защита |
|---|---|
| Агент выполняет произвольную команду | Нет shell: только имена инструментов, параметры по regex, `shell=False` |
| Обход MCP (ошибка в коде, подмена) | Обёртка на сервере пропускает только команды из правил, коды 126 на всё остальное; `safety-support` и `.ssh` недоступны любой команде |
| Агент сам одобряет изменение | Ключ `rw` и секрет OTP только у approver; код знает только человек |
| Подмена заявки в очереди | Approver проверяет хэш команды, правила `write`, срок, берёт адрес из инвентаря |
| Агент меняет конфиг | Конфиг монтируется `:ro`, сервер отказывается стартовать с записываемым конфигом |
| Перепутан стенд | Стенд в имени инструмента, в описании и в первой строке каждого ответа |
| Подмена хоста | `StrictHostKeyChecking=yes`, свой `known_hosts` на каждый стенд; хост принимается только из инвентаря своего стенда — и в MCP, и повторно в approver |
| HTTP как прокси к чужим адресам (SSRF) | URL только из инвентаря, итоговый хост сверяется с эндпоинтом, редиректы не выполняются, ответ ограничен 200 КБ, `Set-Cookie` и `Authorization` скрываются |
| Чужой клиент подключается к MCP | Порт только на `127.0.0.1`, обязательный Bearer-токен (сравнение за постоянное время), проверка `Host` и `Origin` (DNS-rebinding) |
| Страница одобрения из браузера злоумышленника | Только `127.0.0.1`, проверка `Host` и `Origin`, CSRF, CSP, `no-store` |

> [!IMPORTANT]
> Всё это работает, только если у агента нет доступа к approver и Docker. Токен MCP не даёт права на запись:
> запись по-прежнему требует кода от человека. Запретите в `settings.json` Claude Code (в opencode — аналогично через `permission`):
>
> ```json
> {
>   "permissions": {
>     "deny": ["Bash(docker:*)", "Read(./local/keys-rw/**)", "Read(./local/approver-state/**)", "Edit(./local/conf/**)"],
>     "disableBypassPermissionsMode": "disable"
>   }
> }
> ```

## Разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

CLI администратора (без пакета `mcp`, на чистом Python):

```bash
SAFETY_CONF=local/conf python3 server.py check
SAFETY_CONF=local/conf python3 server.py list-hosts <стенд>
SAFETY_CONF=local/conf python3 server.py gen-rules <стенд> <хост> ro
```

## Структура проекта

```text
safety-mcp-server/
├── server.py                 # MCP-сервер и CLI
├── approver.py               # approver: serve / enroll
├── init-local.sh             # создание local/ из шаблонов и генерация ключей (Linux)
├── windows/                  # init, compose, keyscan, deploy, open-new-requests (PowerShell)
├── docker/                   # entry.sh (ключи в tmpfs с правами 600) и вспомогательные скрипты для Windows
├── safety_mcp/
│   ├── config.py             # загрузка и валидация конфига
│   ├── core.py               # выбор хостов, сборка команды, regex правил
│   ├── tools.py              # MCP-инструменты, about, dry_run, rw_approve
│   ├── sshexec.py            # ssh, параллельность, аудит
│   ├── httpcheck.py          # HTTP-проверки (стандартная библиотека)
│   ├── mcpserver.py          # FastMCP с отказом на лишние параметры
│   ├── httpserver.py         # HTTP-транспорт: Bearer-токен, Host/Origin
│   ├── queue.py              # очередь заявок и кодов
│   ├── approver.py           # страница подтверждения, TOTP, enroll
│   ├── rules.py              # генерация rules.ro / rules.rw
│   └── totp.py               # RFC 6238
├── wrapper/
│   ├── safety-wrapper           # обёртка на сервере
│   ├── deploy.sh             # раскладка обёртки и ключей
│   └── open-new-requests.sh  # автооткрытие страницы
├── conf.example/             # пример конфига
├── tests/                    # pytest
├── docs/TASK.md              # полное техническое задание
├── local/                    # ваши данные (не в git): conf, keys-ro, keys-rw, state, queue, approver-state, mcp-token, .env
├── Dockerfile
├── docker-compose.yml
└── docker-compose.windows.yml # Windows: секрет OTP в томе Docker
```

## Ограничения и планы

- [ ] HTTP: авторизация, заголовки, методы записи и запросы через хост стенда (`via`) — зарезервированы.
- [ ] Мультиплексирование SSH (`ControlMaster`) для ускорения повторных вызовов.
- [ ] Python 3.8/3.9 не поддерживаются: пакет `mcp` требует 3.10+.
- TOTP требует синхронизации часов машины с approver и телефона (`APPROVER_TOTP_SKEW`); без интернета нужен внутренний NTP.

## Лицензия

[Apache License 2.0](LICENSE)
