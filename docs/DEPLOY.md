# Развёртывание на новом GX10

Образ Workbench — это **репозиторий + установщик**, а не дисковый образ. ОС ставится штатно, всё тяжёлое (образ vLLM ~12 ГиБ, веса 22–103 ГиБ) скачивается прямо на устройство. Повторяемость обеспечивает [`install/versions.lock`](../install/versions.lock).

## 1. Что купить и что ставить

- **ASUS Ascent GX10** (NVIDIA GB10, 128 ГБ, NVMe ≥ 1 ТБ). Голый — с завода стоит DGX OS, OOBE не пройден. Подойдёт и NVIDIA DGX Spark / другой GB10-SKU: скрипты не привязаны к ASUS (имена интерфейсов определяются автоматически), но проверено только на GX10.
- **Второй NVMe не нужен**: fast + deep = ~83 ГиБ, на 1 ТБ остаётся место под библиотеку и депо пакетов.
- В комплект (см. мануал, «Состав комплекта»): адаптер 240 Вт, RJ45-патчкорд, USB-хаб, по желанию USB-serial/USB-CAN.

## 2. Установка ОС (делает оператор, ~30 мин)

Рекомендуемый путь — **штатная DGX OS с OOBE**, без кастомных ISO (вариант с пересборкой ISO из PLAN v2 отложен: он даёт выигрыш только при массовом тиражировании, а ломается при каждом релизе DGX OS).

1. Включить GX10 с монитором, пройти OOBE (мастер первого запуска):
   - язык/раскладка — любая; часовой пояс — свой;
   - **пользователь `admin`**, пароль записать на лист паролей. Другое имя тоже работает (юниты подставят его), но мануал написан под `admin`;
   - **hostname**: короткий, латиницей, уникальный (`gx10-field2`) — он станет mDNS-именем `gx10-field2.local`;
   - Wi-Fi: подключить к сети с интернетом (нужно для скачивания). Сеть и пароль остаются в системе — в поле GX10 будет пытаться к ней подключиться, это нормально и не мешает хотспоту;
   - телеметрия NVIDIA — **не включать**; Ubuntu Pro — не нужен;
   - EULA прочитать и принять.
2. Если образ с завода старее **DGX OS 7.6.0** (`cat /etc/dgx-release`) — обновить, стек проверен на 7.6.0 (драйвер 580.178, CUDA 13.0):
   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```
   Старые версии: vLLM 0.28 на драйвере 580.142 не проверялся. Новые версии DGX OS — вероятно, работают; при проблемах с vLLM см. §5.
3. Оставить GNOME (нужен для ярлыка и браузера). Автоблокировку экрана выключить: Настройки → Питание → «Выключать экран: никогда», «Автоматический переход в режим ожидания: выкл». Установщик дополнительно маскирует `sleep.target`.
4. Включить SSH, если выключен: `sudo systemctl enable --now ssh`. Проверить: `ssh admin@gx10-field2.local` с другой машины.

## 3. Установка Workbench (~30 мин + загрузка)

На GX10, в терминале под `admin`:

```bash
git clone https://github.com/Theclimateguy/Workbench.git ~/Workbench
cd ~/Workbench
./install/bootstrap.sh --with-deep --password 'ПАРОЛЬ_WEB'
```

Флаги: `--with-deep` — качать и gpt-oss-120b (+61 ГиБ, ~1 ч на 200 Мбит); без него только `fast`, deep можно добить потом (`install/fetch-models.sh deep`). `--skip-models` — только софт (для обновления копии, где веса уже есть). Без `--password` пароль web генерируется и печатается.

Фазы (маркеры в `/var/lib/support/bootstrap/`, повторный запуск продолжает с места обрыва):

| # | Фаза | Что делает |
|---|---|---|
| 0 | preflight | arm64, DGX OS ≥ 7.6.0, GPU, docker, место, интернет |
| 1 | system | пакеты из `install/apt-packages.txt`, группа docker, запрет сна, выключение OOBE-хотспота |
| 2 | venv | `/opt/support/venv` по `install/requirements.txt` |
| 3 | opencode | движок агента, версия из lock |
| 4 | deploy | `agent/` → `/opt/support`, `stack/` → `/opt/support-stack`, юниты, netplan-профили `lan`/`direct` |
| 5 | vllm-image | `docker pull` по digest, проверка GPU в контейнере |
| 6 | ui-assets | Transformers.js + Whisper ONNX (~0.35 ГиБ) в `/opt/support/ui-assets` для голосового ввода в браузере |
| 6 | models | `hf download` по ревизиям из lock в `/opt/models` |
| 7 | services | ключ агента, `llm@fast` enable + старт, web-пароль, локальный CA + сертификат, `support-opencode`, `support-ui` (:8080), `support-ui-tls` (:8443) |
| 8 | desktop | ярлык «Spark Support», мануал на рабочий стол, хотспот auto |
| 9 | verify | `bench.py`, цель `self`, `mcp_smoke.py` |

В конце — `support-ctl status` с адресами. Дальше по мануалу: `target add`, документация, `docs reload`.

## 3a. Веса без интернета на устройстве

Если у GX10 нет быстрого канала (в поле, или интернет только через ноутбук), веса можно скачать на любой машине с `python3` и перенести по кабелю или на USB-SSD:

```bash
pip install huggingface_hub            # один раз, на машине с интернетом
MODELS_DIR=/Volumes/SSD/models HF=hf ./install/fetch-models.sh deep   # из клона репозитория, те же ревизии из versions.lock
rsync -a /Volumes/SSD/models/ admin@10.77.0.1:/opt/models/            # потом на GX10 (или с USB-SSD прямо на нём)
```

Маркер `.workbench-rev` в папке модели переносится вместе с файлами, поэтому `bootstrap.sh`/`fetch-models.sh` на GX10 увидят модель как готовую. `support-ctl model` и тумблер в интерфейсе отказываются переключаться на профиль, чьих весов на диске нет.

## 4. Обновления

- **Софт** (агент, UI, скрипты, конфиги): на GX10 `support-ctl update` — `git pull` + `deploy.sh`. Веса, цели, ключи, документация не трогаются.
- **Голосовой ввод / PWA на уже развёрнутом GX10**: `support-ctl update`, затем `install/fetch-ui-assets.sh` (нужен интернет) и `support-ctl web` (выпустит сертификат и включит `support-ui-tls`).
- **Версии** (OpenCode, vLLM, модели): правка `install/versions.lock` в репозитории → `support-ctl update` → для образа/моделей вручную `bootstrap.sh` после удаления соответствующего маркера (`sudo rm /var/lib/support/bootstrap/vllm-image.done`).
- **ОС**: `sudo apt full-upgrade` — отдельно, перед выездом, с последующим `support-ctl bench` (минимум 60 tok/s на fast — норма 95).
- Что ещё посмотреть: `support-ctl version`.

## 5. Если что-то пошло не так

| Симптом | Причина / что делать |
|---|---|
| preflight: `DGX OS … older than` | OTA-обновление (§2 п.2) или `FORCE_OLD_OS=1 ./install/bootstrap.sh` на свой риск |
| `GPU not visible in container` | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`, повторить |
| `hf download` обрывается | повторить `bootstrap.sh` — докачает; при лимитах HF задать `HF_TOKEN=…` |
| `llm@fast failed` | `journalctl -u llm@fast -n 100`; частое — не хватило памяти из-за page cache: `sudo systemctl restart llm@fast` (юнит сам сбрасывает кэш) |
| vLLM не стартует на новой DGX OS | откатить образ: в `versions.lock` другая версия → `rm vllm-image.done` → `bootstrap.sh`; известные рабочие пары — `docs/bench-log.md` |
| `verify`: `mcp_smoke` падает | `support-ctl target test self`; если нет sshd — `sudo systemctl enable --now ssh` |

## 6. Клонирование уже настроенного GX10

Проще bootstrap: веса и образ можно скопировать по сети (`rsync -a /opt/models/ new:/opt/models/`, `docker save | docker load`), затем `bootstrap.sh --skip-models` — он пропустит загрузку, но пройдёт все остальные фазы. Секреты (`/etc/support/*.env`, `/opt/support/keys/`) **не копировать** — у каждого устройства свои.
