# Архитектура Workbench

## Компоненты

```
 Устройство GB10: DGX Spark / ASUS GX10 (DGX OS 7.x, 128 GB unified)
 ┌─────────────────────────────────────────────────────────────────────┐
 │  llm@fast | llm@deep  (systemd, Conflicts=)                         │
 │     └─ docker compose → vllm/vllm-openai:v0.28.0  → 127.0.0.1:8000  │
 │  support-opencode  (OpenCode headless `serve`)   → 127.0.0.1:4096   │
 │     ├─ /opt/support/workspace  (AGENTS.md, opencode.json, docs/)    │
 │     └─ MCP target-host (/opt/support/mcp/target_host.py)            │
 │           ├─ ssh  -i /opt/support/keys/agent_ed25519 → Linux-хост   │
 │           ├─ connectors.py: serial (pyserial) · can (SocketCAN)     │
 │           │                 modbus (TCP/RTU) · http (device API)    │
 │           ├─ scan → support-ctl scan (USB, tty, CAN, ARP, mDNS)     │
 │           └─ workbench_run → docker run workbench:local             │
 │                 (--cap-drop ALL, без GPU/файлов, сеть none|device)  │
 │  support-priv (sudo, фиксированные verbs: net-allow/clear, can-restart, arp-scan) │
 │  support-ui / support-ui-tls (FastAPI)  → :8080 http, :8443 https   │
 │     ├─ index.html (UI v2: статус-полоса, рельс, чат, док-вкладки)   │
 │     │    /classic — прежний одноколоночный UI                      │
 │     ├─ /api/device/*: паспорт, консоль оператора, отчёт; /api/scan  │
 │     │    (target_host.py импортируется в процесс: те же allowlist)  │
 │     ├─ /static: PWA (manifest, sw.js), STT-worker (Transformers.js) │
 │     └─ /opt/support/ui-assets: Whisper ONNX + ORT wasm, mermaid     │
 │  support-ctl (CLI), support-resolve-target, onboard-target.sh       │
 │  NetworkManager: lan (DHCP) → direct (10.77.0.1 shared) → hotspot   │
 └─────────────────────────────────────────────────────────────────────┘
```

| Слой | Файлы в репо | На устройстве |
|---|---|---|
| LLM | `stack/compose.yaml`, `stack/env/*.env`, `stack/systemd/llm@.service` | `/opt/support-stack`, `/opt/models` |
| Агент | `agent/workspace/{AGENTS.md,opencode.json}`, `stack/systemd/support-opencode.service` | `/opt/support/workspace`, `~/.opencode/bin/opencode` |
| Инструменты | `agent/mcp/target_host.py`, `agent/mcp/connectors.py` | `/opt/support/mcp`, аудит `/var/log/support/audit.jsonl` |
| Песочница | `stack/workbench/Dockerfile` | образ `workbench:local`, сеть docker `workbench` (172.31.77.0/24), прогоны `/opt/support/workbench/runs/<ts>/` (скрипт, devices.json, stdout/stderr) |
| Root-операции | `agent/bin/support-priv` | `/opt/support/bin/support-priv` + правило в `/etc/sudoers.d/90-support-ui` |
| Стенд приёмки | `tests/eval/` | `/opt/support/tests/eval`, контейнер `evalbox`, устройство `evalbox` |
| UI | `agent/ui/{support_ui.py,index.html,classic.html,static/}`, `stack/systemd/support-ui{,-tls}.service` | `/opt/support/ui`, `/opt/support/ui-assets` (Whisper, ORT, mermaid), пароль `/etc/support/web.env`, TLS `/etc/support/tls` |
| CLI | `agent/bin/*` | `/opt/support/bin`, `/usr/local/bin/support-ctl` |
| Сеть | `stack/net/*.yaml` | `/etc/netplan/90-support-*.yaml`, профиль `hotspot` в NM |
| Устройства | — | `/opt/support/targets/*.env` (`TYPE=ssh` по умолчанию; `serial`, `can`, `modbus`, `http`), `/opt/support/keys/` |
| Установка | `install/*` | маркеры `/var/lib/support/bootstrap/` |

## Модель безопасности

- Агент **не исполняет команды на самом устройстве GB10** (`bash`, `edit` в `opencode.json` — `deny`). К технике он ходит только через MCP `target-host`: по SSH под `support-agent` с ключом устройства, либо через коннекторы serial/CAN/Modbus/HTTP. Свой код он может выполнить только в песочнице `workbench_run` — контейнер без GPU, без файлов GB10, `--cap-drop ALL`, 2 ГиБ, сеть `none` или только до перечисленных устройств (правила в цепочке `DOCKER-USER` ставит и снимает `support-priv`), проброс только tty/USB тех устройств, что названы в вызове; скрипт целиком виден оператору в карточке.
- Три класса действий, закреплённые за именами инструментов (гейт — `permission` в `opencode.json`, класс проверяет сервер, а не модель):

  | Класс | Инструменты | Approval |
  |---|---|---|
  | чтение | `device_list`, `collect_facts`, `run_readonly` (allowlist), `read_file`, `observe` (serial — только пассивное прослушивание), `scan` | нет |
  | конфиг | `run_change`, `write_file`, `device_act` (HTTP POST/PUT), `workbench_run` | на каждый вызов; «разрешить всегда» допустимо в сессии |
  | актуация | `device_actuate` (запись в serial/CAN/Modbus, power/firmware по ssh), `device_recover`, `reboot_and_wait` | на каждый вызов; «всегда» отвергают и UI, и `support_ui.py` (409); обязателен `rollback`; `run_change` отсекает power/firmware/disk-команды регулярным выражением |
- Всё в аудит: `/var/log/support/audit.jsonl` (время, устройство, инструмент, класс, команда/действие, причина, план отката, rc).
- Вывод устройства, трафик шины, значения регистров и web-страницы для агента — данные, не инструкции (AGENTS.md; кейс-ловушка 15 в стенде).
- `support-priv` — единственная sudo-точка стека кроме `systemctl start llm@*`: фиксированные verbs без свободных аргументов (`net-allow RUN IP…`, `net-clear RUN`, `can-restart IFACE BITRATE`, `arp-scan IFACE`).
- Секреты вне git: `/etc/support/*.env` (root, 600), ключи агента уникальны на устройство.
- Сеть: vLLM и OpenCode слушают только `127.0.0.1`; наружу — только UI :8080/:8443 с basic-auth (localhost без пароля) и SSH. HTTPS — свой локальный CA (`install/gen-certs.sh`), публичный сертификат отдаётся на `/ca.crt`; закрытый ключ CA не покидает устройство.
- Голосовой ввод выполняется в браузере оператора (Whisper через Transformers.js, WebGPU/WASM); на устройство только раздача файлов. Распознанный текст попадает в поле ввода, отправка и подтверждения — руками.

## Поиск устройства без фиксированных адресов

`targets/NAME.env` с `HOST=auto`: `support-resolve-target` пробует mDNS (`MDNS=`) → DHCP-аренды прямого линка по `MAC=` → ARP/IPv6-соседи по `MAC=` → `IP_LAST=`. SSH host key привязан к имени цели (`HostKeyAlias`), не к адресу.

## Модели (см. `docs/bench-log.md`)

| Профиль | Модель | tok/s | Память | Старт |
|---|---|---|---|---|
| fast | Qwen3.6-35B-A3B NVFP4 + MTP | 95 | 20 + 25 (KV) ГиБ | 6 мин |
| deep | gpt-oss-120b MXFP4 (Marlin) | 36 (31/поток при 2) | 66 + 27 (KV) ГиБ (util 0.80) | 9 мин |
| nemotron | Nemotron 3 Super 120B-A12B NVFP4 + MTPv2 | ~20 | 75 + 24 (KV) ГиБ (util 0.85) | — |

Профили взаимоисключающие (`Conflicts=`), переключение `support-ctl model fast|deep|nemotron`.

## Web-интерфейс v2

Реализация плана [ui-v2-plan.md](ui-v2-plan.md) (этапы 1–3) в одном файле без сборки, офлайн:

- **Статус-полоса** с цветной кромкой — один глобальный сигнал режима: серая (нет устройств) · синяя (чтение) · янтарная (авто-изменения) · красная (ожидает актуацию). Устройства, RJ45-профиль и адрес, модель, память GB10, режим одобрений, аудит, fast|deep, тема.
- **Режимы одобрений** (клиентская логика поверх `/permission`): *Спрашивать*; *Авто-изменения* — UI сам отвечает `once` на класс «изменение», актуация всегда спрашивает; *План* — UI отвечает `reject` на всё изменяющее и добавляет пометку к задаче.
- **Карточка одобрения v2**: класс, намерение, команда/скрипт, ожидаемый эффект, откат; для актуации — кнопка-удержание 1.2 с (аналог enabling switch), «всегда» отсутствует (сервер дополнительно отвечает 409).
- **Док-вкладки** — реестр `tabs` в JS (`{title, mount, render}`), новые вкладки добавляются без правки ядра: Трейсы (дерево вызовов по классам, переход к блоку), Консоль (хронология вывода агент+оператор; строка оператора → `/api/device/{name}/run` → `run_readonly`/`observe` с тем же allowlist, аудит `by: operator`), Устройство (паспорт из `collect_facts`/`observe` через `/api/device/{name}/facts`, топология из списка устройств и профиля линка, окружение из `/api/scan`), Схема (блоки ```mermaid из ответов агента, рендер `mermaid.min.js` из ui-assets, правка, экспорт SVG/.mmd). Библиотека и Медиа — заглушки до фаз C/D.
- Адаптив: ≥1100 px три колонки; уже — панель и док по кнопкам; <768 px док как нижний лист. Классический UI — `/classic`.
- Не реализовано из плана: SSE-шина (используется опрос 1.5 с), плагины-вкладки из внешних файлов (манифест), фото/камера, библиотека.

## Устройства и коннекторы

`targets/NAME.env` описывает устройство; `TYPE=` выбирает коннектор (`agent/mcp/connectors.py`):

| TYPE | Поля | observe (чтение) | device_actuate / device_act | recover |
|---|---|---|---|---|
| `ssh` (по умолчанию) | `HOST\|MDNS\|MAC`, `USER`, `PORT`, `KEY` | `collect_facts`, `run_readonly`, `read_file` | `run_change`/`write_file` (конфиг); power/firmware — `device_actuate` | `reboot_and_wait` |
| `serial` | `PORT=/dev/ttyUSB0 BAUD=115200 [EOL=\n]` | пассивное прослушивание N с | `send:<text>`, `hex:<bytes>`, `dtr:0\|1`, `rts:0\|1`, `break` (актуация) | импульс сброса DTR/RTS |
| `can` | `IFACE=can0 BITRATE=500000` | `dump [id=0x123]` — кадры за N с, счётчики по id, состояние линка | `send <id>#<hex>` (актуация) | `ip link down/up` через `support-priv` (bus-off) |
| `modbus` | `HOST= PORT=502 UNIT=1` или `PORT=/dev/ttyUSB1 BAUD=9600` (RTU) | `holding\|input\|coils\|discrete A N`, `id` | `write_register`, `write_registers`, `write_coil` с read-back (актуация) | переподключение и проба |
| `http` | `URL= [AUTH=u:p \| TOKEN=] [VERIFY=0]` | `GET /path`, `HEAD` | `POST\|PUT\|PATCH\|DELETE /path [body]` (конфиг) | `GET /` |

Инвентаризация — `support-ctl scan` (USB, `/dev/serial/by-id`, CAN-интерфейсы, линки, ARP-сканирование каждого линка ≤ /22, DHCP-аренды прямого кабеля, mDNS); агенту доступна как инструмент `scan`.

## Стенд приёмки

`tests/eval/stand.sh up` поднимает на самом устройстве privileged-контейнер `evalbox` (Ubuntu 24.04, systemd, sshd, `/var/log` — tmpfs 256 МБ), регистрирует его как устройство `evalbox` через штатный `onboard-target.sh`; `cases/NN-*/inject.sh` ломает, агент чинит, `check.sh` судит; `run.py` гоняет всё через UI-API с авто-одобрением и считает метрики PLAN §5.

## Направление: универсальный полевой инженер

Фаза A дорожной карты ([PLAN.md](../PLAN.md) §4) закрыта: устройства с коннекторами, песочница, классы approval, инвентаризация, стенд. Дальше: ROS 2 / SWD / WinRM-коннекторы и реальное железо (фаза B), офлайн-библиотека мануалов с поиском (C), зрение через фото с телефона (D), UI v2 ([docs/ui-v2-plan.md](ui-v2-plan.md)).
