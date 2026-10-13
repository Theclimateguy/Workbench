# Workbench — полевой ИИ-инженер на NVIDIA GB10

Автономный (без интернета) агент техподдержки для любого компьютера на NVIDIA GB10 (NVIDIA DGX Spark, ASUS Ascent GX10 и аналоги): локальная LLM на устройстве, подключается по кабелю к целевой машине, диагностирует и чинит/дорабатывает её — каждое изменение только после подтверждения оператора в web-интерфейсе.

*Offline field support agent for NVIDIA GB10 boxes (DGX Spark, ASUS Ascent GX10, …): local vLLM + OpenCode + MCP tools, approval-gated changes, web UI, direct-cable networking.*

| Проверено на | NVMe | Статус |
|---|---|---|
| ASUS Ascent GX10 | 1 ТБ | в эксплуатации с 2026-10-04 |
| NVIDIA DGX Spark FE | 4 ТБ | развёрнут 2026-10-13 |
| другие GB10 (Dell, HP, Lenovo, MSI, Gigabyte, Acer) | — | та же платформа, не проверялись |

| Документ | Для кого |
|---|---|
| [docs/manual/operator-manual.pdf](docs/manual/operator-manual.pdf) | оператор в поле: включить, подключить, работать с агентом |
| [docs/DEPLOY.md](docs/DEPLOY.md) | развёртывание на новом устройстве с нуля, варианты железа, обновления |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | что где лежит, модель безопасности |
| [PLAN.md](PLAN.md) | состояние проекта и план развития до «универсального инженера» |
| [docs/bench-log.md](docs/bench-log.md) | замеры моделей на GB10 |

## Быстрый старт на новом устройстве

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

Локально на Mac: правки → `make lint` → `make deploy HOST=gx10` (rsync + deploy.sh на устройстве) или на самом устройстве `support-ctl update` после `git push`. PDF мануала: `make manual` (pandoc + XeLaTeX + шрифты DejaVu).
