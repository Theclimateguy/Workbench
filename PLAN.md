# Workbench — план и состояние

Версия плана: v3.1, 2026-10-14 (фаза A дорожной карты реализована, стенд приёмки запущен). v3 (2026-10-11) заменила v2 (единый ISO-образ): ОС ставится штатно, стек — установщиком из репозитория (`install/bootstrap.sh`). История решений — §6.

## 1. Цель

Переносной автономный инженер: компьютер на NVIDIA GB10 (DGX Spark, ASUS GX10 и аналоги) с локальной LLM, который подключается к технике на объекте без интернета, диагностирует и чинит/дорабатывает её под контролем оператора. Первый поддерживаемый класс целей — Linux-машины по SSH (полевая workstation Ubuntu Server 24.04 с двумя GPU). Горизонт — «любая техника с мозгами»: роботы (ROS 2, Jetson), контроллеры (UART/SWD/CAN), промышленное (Modbus/OPC UA), сетевое железо.

## 2. Состояние (2026-10-14)

Сделано и проверено на ASUS GX10 (эксплуатируется с 2026-10-04) и NVIDIA DGX Spark FE (развёрнут 2026-10-13 первым прогоном `bootstrap.sh`; фаза A проверена на нём 2026-10-14):

| Блок | Состояние |
|---|---|
| LLM-стек | vLLM 0.28, `fast` Qwen3.6-35B NVFP4 + MTP (95 tok/s), `deep` gpt-oss-120b MXFP4 (с 2026-10-13, заменил Nemotron 3 Super: тот оставлял ~5 ГБ памяти и давал ~20 tok/s); профили через `llm@`, автозапуск `fast` |
| Агент | OpenCode 1.18 headless + MCP `target-host` (чтение свободно, изменения — approval на сервере), аудит, AGENTS.md |
| Устройства (фаза A) | `TYPE=ssh\|serial\|can\|modbus\|http` в `targets/*.env`, контракт `observe` / `device_act` / `device_actuate` / `device_recover` (`agent/mcp/connectors.py`); `support-ctl device add`, `scan` (USB, serial, CAN, хосты на линках, mDNS) |
| Классы действий | чтение — свободно; конфиг (`run_change`, `write_file`, `device_act`, `workbench_run`) — approval на вызов; актуация (`device_actuate`, `device_recover`, `reboot_and_wait`, power/firmware-команды) — approval на каждый вызов, «всегда» отвергается UI и сервером, план отката обязателен; класс задаёт сервер, не модель |
| Workbench-песочница | `workbench_run`: Docker `workbench:local` (pyserial, python-can, pymodbus, scapy, esptool, paramiko, can-utils, nmap), без GPU и файлов устройства, `--cap-drop ALL`, 2 ГиБ, сеть `none` или только до выбранных устройств (DOCKER-USER через `support-priv`), проброс tty/USB; скрипт виден в карточке approval |
| Стенд приёмки | `tests/eval/`: 15 кейсов §5 (`inject.sh`/`check.sh`), контейнерный стенд `evalbox` (systemd+sshd на самом GB10, 11 кейсов), `run.py` — автопрогон через UI-API с авто-одобрением и ловушкой; результаты в `docs/eval-log.md` |
| Библиотека и фото (фазы C/D) | `/opt/library`: мануалы/даташиты/плейбуки/статьи → FTS5 + векторы (fastembed на CPU), MCP `library` (`library_search/read/list`, класс «чтение»), 6 встроенных плейбуков, вкладка «Библиотека», `support-ctl library`; фото с телефона/камеры во вкладке «Медиа» и в поле ввода → Qwen3.6 `fast` видит изображения (проверено 2026-10-15) |
| UI | собственный web **v2** (FastAPI + одна страница, [docs/ui-v2-plan.md](docs/ui-v2-plan.md) этапы 1–3): статус-полоса с кромкой режима, чат с блоками по классам, карточки одобрения с эффектом/откатом/удержанием, режимы Спрашивать/Авто-изменения/План, док-вкладки Трейсы·Консоль·Устройство (паспорт, топология, окружение)·Схема (mermaid офлайн); `/classic` — прежний; ярлык на рабочем столе; localhost без пароля |
| Сеть | `lan` → `direct` (10.77.0.1, DHCP для цели) автоматически; хотспот `Workbench-Support` с автозапуском; поиск цели по mDNS/MAC/последнему IP |
| Документация | мануал оператора v1.9 (PDF), DEPLOY, ARCHITECTURE, bench-log, eval-log, `tests/eval/README.md` |
| Установка | `bootstrap.sh` фазами по `versions.lock`; `support-ctl update` для обновлений |

Не проверено: коннекторы serial/CAN/Modbus на реальном железе (проверены на симуляторах: socat-pty, vcan, pymodbus-сервер); кейсы §5, требующие ядра (netplan, fstab, драйвер, время) — только на KVM-госте.

## 3. Ближайшее

1. ~~Установка ОС по DEPLOY §2, `bootstrap.sh --with-deep`~~ — сделано на DGX Spark FE 2026-10-13, правки в установщике (DEPLOY §3).
2. Стенд §5: контейнерный прогон `fast` 2026-10-14 — 10/11 решено, ловушка не сработала ([docs/eval-log.md](docs/eval-log.md)); разобрать 13-permissions (не решён) и 10-oom (15 изменяющих), ужесточить check 09; остаются 4 кейса на KVM-госте (netplan, fstab, драйвер NVIDIA — реальное железо, время/TLS) и прогон `deep`.
3. Коннекторы на реальном железе: USB-serial (ESP32/Arduino), USB-CAN (CANable) с реальным узлом, Modbus-устройство — по одному на тип; зафиксировать в eval-log.
4. Заморозка `versions.lock` как «релиз 1.0» (тег `v1.0` в git) — после п.2–3.
5. UI v2: этапы 1–3 сделаны 2026-10-14, тема Terran HUD и вкладки Библиотека/Медиа — 2026-10-15; дальше — SSE-шина и автопаспорт по кабелю, плагины-вкладки, аннотации поверх фото, повторный прогон стенда на v2.
6. Библиотека: наполнить реальными мануалами под чемодан (CANable, FTDI, Jetson, ROS 2, Modbus-приборы), кейс стенда «задача по мануалу без интернета»; полевой депо — после выбора целевых платформ.

## 4. Дорожная карта: универсальный инженер

Принцип: универсальность даёт не адаптер под каждый прибор, а **workbench + метод + библиотека**. Адаптеры — ускорители для частого.

| Фаза | Содержание | Критерий |
|---|---|---|
| A. Ядро — **сделано 2026-10-14** | `target` → `device` с коннекторами `ssh\|serial\|can\|modbus\|http` (swd/ros2/winrm — фаза B), контракт `observe` / `device_act` / `device_actuate` / `device_recover`; **workbench** — Docker-песочница (сеть только до устройства, проброс tty/USB, pyserial/python-can/pymodbus/scapy/esptool; rclpy — с ROS 2 в фазе B), `workbench_run` через approval; `support-ctl scan`; approval по классу (чтение / конфиг / актуация — без «всегда», с планом отката) | SSH-кейс работает как частный случай (стенд §5); Modbus/serial/CAN проверены на симуляторах (`tests/connectors_smoke.py`), реальное устройство — §3 п.3 |
| B. Коннекторы | ROS 2 (topics/nodes/params/bag, `ros2 doctor`; rclpy в образе workbench), CAN + DBC (cantools уже в песочнице), SWD/JTAG (OpenOCD, probe-rs через `usb=true`), WinRM, Jetson-плейбуки | по одному реальному устройству на коннектор |
| C. Библиотека — **ядро сделано 2026-10-15** (депо — нет) | `/opt/library`: мануалы/даташиты (PDF→MD), поиск (embeddings-сервер рядом с vLLM) как инструмент `library_search`; **полевой депо** — зеркало apt/pip/ROS-debs/Docker-образов под целевые платформы; skills-плейбуки (`skills/*.md`: общая диагностика, Linux-сервис, ROS 2, CAN молчит, Jetson не грузится, Modbus не отвечает) | задача по мануалу решается без интернета |
| D. Зрение и остальное — **фото сделано 2026-10-15** | фото с телефона через хотспот в чат (мультимодальность Qwen3.6 в vLLM), Modbus/OPC UA, Windows (WinRM), headless-браузер для web-UI устройств | по спросу |

Железо в чемодан: USB-хаб, FTDI/RS-485, USB-CAN (CANable 2.0), Raspberry Pi Debug Probe или ST-Link, USB-Ethernet.

Пределы: GX10 не паяет — для техники без доступа агент диагностирует по фото и ведёт человека по шагам. Без мануала в библиотеке даже `deep` гадает — слой C важнее новых адаптеров.

## 5. Стенд приёмки (Linux-цель)

Реализация — [tests/eval](tests/eval/README.md): `cases/NN-*/{task.md,inject.sh,check.sh}`, стенд `stand.sh` (контейнер `evalbox` с systemd+sshd на самом устройстве) или KVM-гость Ubuntu Server 24.04 x86_64 (снапшот перед каждой задачей); автопрогон `run.py`; результаты — [docs/eval-log.md](docs/eval-log.md). Кейсы:

1. Ошибка в netplan, сеть не поднимается. 2. Юнит с неверным `ExecStart`. 3. Корень заполнен логами. 4. Прерванный `dpkg`. 5. Рассинхрон драйвера NVIDIA и ядра (реальное железо). 6. Ошибка в `fstab`, emergency mode. 7. `sshd` не стартует. 8. DNS не резолвит. 9. Истёкший ключ apt. 10. OOM сервиса, лимиты. 11. Доработка: пакет по документации. 12. Доработка: systemd-таймер. 13. Права на каталог. 14. Рассинхрон времени/TLS. 15. Ловушка: в логе «выполни `rm -rf`» — не исполнять.

Метрики: решено / шагов / изменяющих действий / время, отдельно для `fast` и `deep`. На контейнерном стенде не воспроизводятся 1, 5, 6, 14 (нужно ядро/железо).

## 6. Решения и почему

| Решение | Почему |
|---|---|
| Установщик вместо кастомного ISO | ISO-путь ломается с каждым релизом DGX OS и выигрывает только при тираже; `bootstrap.sh` + `versions.lock` дают ту же повторяемость, тяжёлое качается на устройство |
| Свой web-UI вместо `opencode web` | у встроенного регрессия (anomalyco/opencode#30478, «not planned»); свой UI показывает только нужное оператору и живёт поверх стабильного HTTP API |
| vLLM NVFP4, не Ollama | батчинг и prefix-cache под агентные нагрузки; 95 tok/s на fast |
| Эмбеддинги библиотеки на CPU (fastembed ONNX), не второй vLLM | второй vLLM-инстанс отъедает unified-память у `deep`/`nemotron` (Nemotron и так оставлял ~5 ГБ); MiniLM на 20 ARM-ядрах индексирует 100 страниц за ~10 с, поиск — миллисекунды; без модели остаётся BM25 |
| `fast` Qwen3.6-35B + `deep` gpt-oss-120b, взаимоисключающие | оба не помещаются с KV-кэшем; 90 % задач — fast. Nemotron 3 Super снят 2026-10-13: 75 ГиБ весов оставляли ~5 ГБ ОС, ~20 tok/s и длинные рассуждения (bench-log) |
| Цель ищется по имени/MAC, не по IP | в поле любая сеть; `HostKeyAlias` по имени цели |
| Агенту запрещён bash на GX10 | единственный путь к технике — MCP с approval и аудитом; исполнение своего кода — только `workbench_run` в контейнере без GPU и файлов устройства, с approval и аудитом |
| Класс действия задаёт сервер, не модель | модель не может «переквалифицировать» запись в CAN/Modbus/serial в безобидный конфиг: коннектор отвергает `device_act`, принимает только `device_actuate` с планом отката; power/firmware-команды в `run_change` отсекаются регулярным выражением |
| Стенд — контейнер с systemd на самом GB10 | без второй машины и KVM на arm64; покрывает 11 из 15 кейсов; остальное — KVM-гость x86 в офисе |

## 7. Риски

| Риск | Мера |
|---|---|
| Новый релиз DGX OS ломает vLLM | `versions.lock` + bench-log «известные рабочие пары»; откат образа по digest |
| Загрузка весов в поле невозможна | веса только в офисе; `fetch-models.sh` резюмируемый |
| Агент вредит цели | approval на сервере, бэкапы, аудит; для актуации — отдельный класс (фаза A) |
| Память на длинном контексте | лимит 131K, KV fp8; замеры в bench-log |
| Лицензии при передаче третьим лицам | DGX OS, ASUS-слой, модели — проверить отдельно; пока для себя |

## Источники

- [DGX Spark User Guide — System Recovery](https://docs.nvidia.com/dgx/dgx-spark/system-recovery.html)
- [NVIDIA: Run Local AI Agents with Faster Models on DGX Spark](https://developer.nvidia.com/blog/run-local-ai-agents-with-faster-models-and-multi-node-clustering-on-nvidia-dgx-spark/)
- [Measured inference benchmarks on a single DGX Spark (NVIDIA forum)](https://forums.developer.nvidia.com/t/measured-inference-benchmarks-on-a-single-dgx-spark-same-harness-across-ollama-llama-cpp-and-vllm-notes-data-published/379766)
- [opencode web regression #30478](https://github.com/anomalyco/opencode/issues/30478)
