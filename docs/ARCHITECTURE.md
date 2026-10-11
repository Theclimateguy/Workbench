# Архитектура Workbench

## Компоненты

```
 GX10 (DGX OS 7.6, GB10, 128 GB unified)
 ┌─────────────────────────────────────────────────────────────────────┐
 │  llm@fast | llm@deep  (systemd, Conflicts=)                         │
 │     └─ docker compose → vllm/vllm-openai:v0.28.0  → 127.0.0.1:8000  │
 │  support-opencode  (OpenCode headless `serve`)   → 127.0.0.1:4096   │
 │     ├─ /opt/support/workspace  (AGENTS.md, opencode.json, docs/)    │
 │     └─ MCP target-host (/opt/support/mcp/target_host.py)            │
 │           └─ ssh -i /opt/support/keys/agent_ed25519 → устройство    │
 │  support-ui  (FastAPI, /opt/support/ui)          → 0.0.0.0:8080     │
 │  support-ctl (CLI), support-resolve-target, onboard-target.sh       │
 │  NetworkManager: lan (DHCP) → direct (10.77.0.1 shared) → hotspot   │
 └─────────────────────────────────────────────────────────────────────┘
```

| Слой | Файлы в репо | На устройстве |
|---|---|---|
| LLM | `stack/compose.yaml`, `stack/env/*.env`, `stack/systemd/llm@.service` | `/opt/support-stack`, `/opt/models` |
| Агент | `agent/workspace/{AGENTS.md,opencode.json}`, `stack/systemd/support-opencode.service` | `/opt/support/workspace`, `~/.opencode/bin/opencode` |
| Инструменты | `agent/mcp/target_host.py` | `/opt/support/mcp`, аудит `/var/log/support/audit.jsonl` |
| UI | `agent/ui/{support_ui.py,index.html}`, `stack/systemd/support-ui.service` | `/opt/support/ui`, пароль `/etc/support/web.env` |
| CLI | `agent/bin/*` | `/opt/support/bin`, `/usr/local/bin/support-ctl` |
| Сеть | `stack/net/*.yaml` | `/etc/netplan/90-support-*.yaml`, профиль `hotspot` в NM |
| Цели | — | `/opt/support/targets/*.env`, `/opt/support/keys/` |
| Установка | `install/*` | маркеры `/var/lib/support/bootstrap/` |

## Модель безопасности

- Агент **не исполняет команды на самом GX10** (`bash`, `edit` в `opencode.json` — `deny`). Единственный путь к устройству — MCP `target-host` по SSH под `support-agent` с ключом GX10.
- Классы инструментов: чтение (`collect_facts`, `run_readonly` с allowlist, `read_file`) — без вопросов; изменение (`run_change`, `write_file`, `reboot_and_wait`) — approval в UI на каждый вызов; проверка — на стороне MCP-сервера, не модели.
- Всё в аудит: `/var/log/support/audit.jsonl` (время, цель, команда, причина, rc).
- Вывод устройства для агента — данные, не инструкции (AGENTS.md).
- Секреты вне git: `/etc/support/*.env` (root, 600), ключи агента уникальны на устройство.
- Сеть: vLLM и OpenCode слушают только `127.0.0.1`; наружу — только UI :8080 с basic-auth (localhost без пароля) и SSH.

## Поиск устройства без фиксированных адресов

`targets/NAME.env` с `HOST=auto`: `support-resolve-target` пробует mDNS (`MDNS=`) → DHCP-аренды прямого линка по `MAC=` → ARP/IPv6-соседи по `MAC=` → `IP_LAST=`. SSH host key привязан к имени цели (`HostKeyAlias`), не к адресу.

## Модели (см. `docs/bench-log.md`)

| Профиль | Модель | tok/s | Память | Старт |
|---|---|---|---|---|
| fast | Qwen3.6-35B-A3B NVFP4 + MTP | 95 | 20 + 25 (KV) ГиБ | 6 мин |
| deep | Nemotron 3 Super 120B NVFP4 + MTPv2 | 24 | 75 + 24 ГиБ | 12 мин |

Профили взаимоисключающие (`Conflicts=`), переключение `support-ctl model`.

## Направление: универсальный полевой инженер

Текущая схема — один тип цели (Linux по SSH). План расширения в [PLAN.md](../PLAN.md): устройство с набором коннекторов (ssh, serial, can, swd, modbus, http, ros2), workbench-песочница для скриптов агента, approval по классу действия, офлайн-библиотека мануалов с поиском, зрение через фото с телефона.
