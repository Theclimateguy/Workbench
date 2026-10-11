# Workbench — полевой ИИ-инженер на ASUS Ascent GX10

Автономный (без интернета) агент техподдержки: локальная LLM на GX10, подключается по кабелю к целевой машине, диагностирует и чинит/дорабатывает её — каждое изменение только после подтверждения оператора в web-интерфейсе.

*Offline field support agent on NVIDIA GB10: local vLLM + OpenCode + MCP tools, approval-gated changes, web UI, direct-cable networking.*

| Документ | Для кого |
|---|---|
| [docs/manual/GX10-operator-manual.pdf](docs/manual/GX10-operator-manual.pdf) | оператор в поле: включить, подключить, работать с агентом |
| [docs/DEPLOY.md](docs/DEPLOY.md) | развёртывание на новом GX10 с нуля, обновления |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | что где лежит, модель безопасности |
| [PLAN.md](PLAN.md) | состояние проекта и план развития до «универсального инженера» |
| [docs/bench-log.md](docs/bench-log.md) | замеры моделей на GX10 |

## Быстрый старт на новом GX10

```bash
git clone https://github.com/Theclimateguy/Workbench.git ~/Workbench
cd ~/Workbench && ./install/bootstrap.sh --with-deep
```

Подробно, включая установку ОС: [docs/DEPLOY.md](docs/DEPLOY.md).

## Структура

```
install/      bootstrap.sh (установка с нуля), deploy.sh (обновление софта), fetch-models.sh, fetch-ui-assets.sh, gen-certs.sh, versions.lock
agent/        bin/ (support-ctl, resolve, onboarding), mcp/ (target-host), ui/ (web + PWA + офлайн-голос), workspace/ (конфиг агента), desktop/
stack/        compose.yaml + env/ (профили LLM), systemd/, net/ (netplan-профили lan/direct)
tests/        bench.py (tok/s, TTFT, tool-call), mcp_smoke.py
docs/         мануал (md + PDF, сборка `make manual`), DEPLOY, ARCHITECTURE, bench-log
build/        manual.sh
```

## Разработка

Локально на Mac: правки → `make lint` → `make deploy HOST=gx10` (rsync + deploy.sh на устройстве) или на самом GX10 `support-ctl update` после `git push`. PDF мануала: `make manual` (pandoc + XeLaTeX + шрифты DejaVu).
