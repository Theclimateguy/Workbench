# Workbench — полевой ИИ-инженер на NVIDIA GB10

Автономный (без интернета) агент техподдержки для любого компьютера на NVIDIA GB10 (NVIDIA DGX Spark, ASUS Ascent GX10 и аналоги): локальная LLM на устройстве, подключается к технике на объекте — Linux-машины по SSH, контроллеры по serial/CAN/Modbus, устройства с web-API — диагностирует и чинит/дорабатывает её. Чтение свободно, изменения конфигурации — по подтверждению оператора, воздействие на оборудование (шины, питание, прошивка) — по подтверждению на каждый шаг с планом отката. Свой код агент выполняет только в Docker-песочнице без GPU и без доступа к файлам устройства.

*Offline field support agent for NVIDIA GB10 boxes (DGX Spark, ASUS Ascent GX10, …): local vLLM + OpenCode + MCP tools for SSH / serial / CAN / Modbus / HTTP devices, three approval classes (read / config / actuation), a Docker sandbox for agent scripts, web UI, direct-cable networking, an acceptance stand.*

| Проверено на | NVMe | Статус |
|---|---|---|
| ASUS Ascent GX10 | 1 ТБ | в эксплуатации с 2026-10-04 |
| NVIDIA DGX Spark FE | 4 ТБ | развёрнут 2026-10-13; коннекторы, песочница и стенд проверены 2026-10-14 |
| другие GB10 (Dell, HP, Lenovo, MSI, Gigabyte, Acer) | — | та же платформа, не проверялись |

| Документ | Для кого |
|---|---|
| [docs/manual/operator-manual.pdf](docs/manual/operator-manual.pdf) | оператор в поле: включить, подключить, работать с агентом |
| [docs/DEPLOY.md](docs/DEPLOY.md) | развёртывание на новом устройстве с нуля, варианты железа, обновления |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | что где лежит, модель безопасности |
| [PLAN.md](PLAN.md) | состояние проекта и план развития до «универсального инженера» |
| [docs/bench-log.md](docs/bench-log.md) | замеры моделей на GB10 |
| [docs/eval-log.md](docs/eval-log.md), [tests/eval/README.md](tests/eval/README.md) | стенд приёмки: кейсы с поломками, автопрогон, результаты |

## Быстрый старт на новом устройстве

```bash
git clone https://github.com/Theclimateguy/Workbench.git ~/Workbench
cd ~/Workbench && ./install/bootstrap.sh --with-deep
```

Подробно, включая установку ОС: [docs/DEPLOY.md](docs/DEPLOY.md).

## Структура

```
install/      bootstrap.sh (установка с нуля), deploy.sh (обновление софта), fetch-models.sh, fetch-ui-assets.sh, gen-certs.sh, versions.lock
agent/        bin/ (support-ctl, support-priv, resolve, onboarding), mcp/ (target-host + connectors.py), ui/ (web + PWA + офлайн-голос), workspace/ (конфиг агента), desktop/
stack/        compose.yaml + env/ (профили LLM), systemd/, net/ (netplan-профили lan/direct), workbench/ (Dockerfile песочницы)
tests/        bench.py (tok/s, TTFT, tool-call), mcp_smoke.py, connectors_smoke.py (симуляторы устройств + песочница), eval/ (стенд приёмки)
docs/         мануал (md + PDF, сборка `make manual`), DEPLOY, ARCHITECTURE, bench-log, eval-log, ui-v2-plan
build/        manual.sh
```

## Разработка

Локально на Mac: правки → `make lint` → `make deploy HOST=gx10` (rsync + deploy.sh на устройстве) или на самом устройстве `support-ctl update` после `git push`. PDF мануала: `make manual` (pandoc + XeLaTeX + шрифты DejaVu).
