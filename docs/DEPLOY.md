# Развёртывание на новом устройстве GB10

Образ Workbench — это **репозиторий + установщик**, а не дисковый образ. ОС ставится штатно, всё тяжёлое (образ vLLM ~12 ГиБ, веса 22–103 ГиБ) скачивается прямо на устройство. Повторяемость обеспечивает [`install/versions.lock`](../install/versions.lock).

## 1. Железо: любой компьютер на NVIDIA GB10

Стек не привязан к производителю: одна и та же SoC GB10 (SM121), 128 ГБ unified, DGX OS, одинаковый набор портов (4×USB-C, HDMI, RJ45 10G, 2×QSFP ConnectX-7, Wi-Fi 7). Скрипты определяют имена сетевых интерфейсов и пользователя сами.

| Устройство | NVMe | Особенности | Статус |
|---|---|---|---|
| NVIDIA DGX Spark (Founders Edition) | 4 ТБ | hostname `spark-xxxx`; пользователь из OOBE **без** sudo-NOPASSWD — нужен шаг 2.5; DGX OS в `dgx-release` может показывать 7.2.x при актуальном ядре | развёрнут 2026-10-13 |
| ASUS Ascent GX10 | 1 ТБ | OEM-слой ASUS (не используется); OOBE создаёт `admin` с sudo без пароля; hostname `gx10-xxxx` | в эксплуатации с 2026-10-04 |
| Dell Pro Max GB10, HP ZGX Nano, Lenovo ThinkStation PGX, MSI EdgeXpert, Gigabyte AI TOP Atom, Acer Veriton GN100 | 1–4 ТБ | та же платформа и DGX OS | не проверялись; ожидается то же самое |

- **Второй NVMe не нужен**: fast + deep = ~83 ГиБ; даже на 1 ТБ остаётся место под библиотеку и депо пакетов.
- В комплект (см. мануал, «Состав комплекта»): адаптер 240 Вт, RJ45-патчкорд, USB-хаб, по желанию USB-serial/USB-CAN.

## 2. Установка ОС (делает оператор, ~30 мин)

Рекомендуемый путь — **штатная DGX OS с OOBE**, без кастомных ISO (вариант с пересборкой ISO из PLAN v2 отложен: он даёт выигрыш только при массовом тиражировании, а ломается при каждом релизе DGX OS).

1. Включить устройство с монитором, пройти OOBE (мастер первого запуска):
   - язык/раскладка — любая; часовой пояс — свой;
   - **пользователь-оператор**: на ASUS OOBE предлагает `admin`, на DGX Spark — любое имя; юниты и ярлык ставятся под того, кто запускает `bootstrap.sh`;
   - **hostname**: короткий, латиницей, уникальный (`spark-field2`) — он станет mDNS-именем `spark-field2.local`;
   - Wi-Fi: подключить к сети с интернетом (нужно для скачивания). Сеть и пароль остаются в системе — в поле устройство будет пытаться к ней подключиться, это нормально и не мешает хотспоту;
   - телеметрия NVIDIA — **не включать**; Ubuntu Pro — не нужен;
   - EULA прочитать и принять.
2. Если образ с завода старее **DGX OS 7.6.0** (`cat /etc/dgx-release`) — обновить, стек проверен на 7.6.0 (драйвер 580.178, CUDA 13.0):
   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```
   Старые версии: vLLM 0.28 на драйвере 580.142 не проверялся. Новые версии DGX OS — вероятно, работают; при проблемах с vLLM см. §5.
3. Оставить GNOME (нужен для ярлыка и браузера). Автоблокировку экрана выключить: Настройки → Питание → «Выключать экран: никогда», «Автоматический переход в режим ожидания: выкл». Установщик дополнительно маскирует `sleep.target`.
4. Включить SSH, если выключен: `sudo systemctl enable --now ssh`. Проверить: `ssh <пользователь>@spark-field2.local` с другой машины.
5. **sudo без пароля** для пользователя-оператора — обязательное условие: `bootstrap.sh`, `support-ctl` и web-UI вызывают `sudo` из скриптов и сервисов. На ASUS OOBE уже делает это для `admin`; на DGX Spark FE — нет, выполнить один раз (подставив имя):
   ```bash
   echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/90-operator >/dev/null && sudo chmod 440 /etc/sudoers.d/90-operator
   ```

## 3. Установка Workbench (~30 мин + загрузка)

На устройстве, в терминале пользователя-оператора:

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
| 4 | deploy | `agent/` → `/opt/support`, `stack/` → `/opt/support-stack`, юниты, sudo-правила (`systemctl start llm@*`, `support-priv`), netplan-профили `lan`/`direct` |
| 5 | vllm-image | `docker pull` по digest, проверка GPU в контейнере |
| 5b | workbench-image | `support-ctl workbench build` — образ песочницы `workbench:local` (~355 МиБ) и сеть docker `workbench`; при отсутствии интернета — предупреждение, собрать позже |
| 6 | ui-assets | Transformers.js + Whisper ONNX (~0.35 ГиБ) в `/opt/support/ui-assets` для голосового ввода в браузере |
| 6a | library-model | модель эмбеддингов библиотеки (~0.25 ГиБ, `install/fetch-library-model.sh`) в `/opt/library/models`; без неё поиск только по ключевым словам |
| 6 | models | `hf download` по ревизиям из lock в `/opt/models` |
| 7 | services | ключ агента, `llm@fast` enable + старт, web-пароль, локальный CA + сертификат, `support-opencode`, `support-ui` (:8080), `support-ui-tls` (:8443) |
| 8 | desktop | ярлык «Spark Support», мануал на рабочий стол, хотспот auto |
| 9 | verify | `bench.py`, цель `self`, `mcp_smoke.py`, `connectors_smoke.py` (симуляторы serial/CAN/Modbus/HTTP + песочница) |

В конце — `support-ctl status` с адресами. Первый прогон на DGX Spark FE (2026-10-13) выявил и закрыл в установщике: `sudo -v` просит пароль даже при NOPASSWD (теперь `sudo -n true`), лог загрузки весов до появления `/opt` (путь настраиваемый), проверка GPU в контейнере через entrypoint образа (теперь `--entrypoint nvidia-smi`), отсутствие nvidia-runtime в docker на FE (регистрируется автоматически), кратковременный отказ DNS после `netplan apply` (ожидание с перезапуском resolved). Загрузки на узком канале: pip и `docker pull` конкурируют с `hf download` — при `--with-deep` на медленной сети лучше сначала `--skip-models`, потом `fetch-models.sh`. Дальше по мануалу: `target add`, документация, `docs reload`.

## 3a. Веса без интернета на устройстве

Если у Spark нет быстрого канала (в поле, или интернет только через ноутбук), веса можно скачать на любой машине с `python3` и перенести по кабелю или на USB-SSD:

```bash
pip install huggingface_hub            # один раз, на машине с интернетом
MODELS_DIR=/Volumes/SSD/models HF=hf ./install/fetch-models.sh deep   # из клона репозитория, те же ревизии из versions.lock
rsync -a /Volumes/SSD/models/ admin@10.77.0.1:/opt/models/            # потом на GX10 (или с USB-SSD прямо на нём)
```

Маркер `.workbench-rev` в папке модели переносится вместе с файлами, поэтому `bootstrap.sh`/`fetch-models.sh` на Spark увидят модель как готовую. `support-ctl model` и тумблер в интерфейсе отказываются переключаться на профиль, чьих весов на диске нет.

## 4. Обновления

- **Софт** (агент, UI, скрипты, конфиги): на Spark `support-ctl update` — `git pull` + `deploy.sh`. Веса, цели, ключи, документация, прогоны песочницы не трогаются.
- **Песочница на уже развёрнутом устройстве** (обновление с версии до 2026-10-14): после `support-ctl update` один раз `support-ctl workbench build` (нужен интернет, ~2 мин); проверка — `/opt/support/venv/bin/python /opt/support/tests/connectors_smoke.py --ssh-target self`.
- **Стенд приёмки** (не обязателен в поле): `tests/eval/stand.sh up` и `tests/eval/run.py` — см. [tests/eval/README.md](../tests/eval/README.md); контейнер `evalbox` можно снести `stand.sh down`.
- **Python-зависимости на устройстве без интернета** (так было с GX10 в поле): на машине с интернетом собрать пакеты под linux-aarch64 / Python 3.12 — `uv pip compile` + `uv pip install --target pydeps --python-version 3.12 --python-platform aarch64-manylinux_2_28 -r req.txt`, скопировать в `/opt/support/pydeps-offline/` и подключить строкой `/opt/support/pydeps-offline` в `/opt/support/venv/lib/python3.12/site-packages/zz-offline-deps.pth` (пакеты venv остаются главнее). `deploy.sh` эту папку не трогает. Модель поиска библиотеки — скопировать `/opt/library/models/` с другой машины, затем `support-ctl library embed`.
- **Библиотека**: `install/fetch-library-model.sh` (интернет один раз, ~0,25 ГиБ) включает смысловой поиск; без него — поиск по ключевым словам. Документы: вкладка «Библиотека» → «+ Загрузить» или `support-ctl library add FILE... --kind manual|datasheet|playbook|paper|note`.
- **Голосовой ввод / PWA на уже развёрнутом Spark**: `support-ctl update`, затем `install/fetch-ui-assets.sh` (нужен интернет) и `support-ctl web` (выпустит сертификат и включит `support-ui-tls`).
- **Третий профиль `nemotron`** (Nemotron 3 Super + MTPv2): `install/fetch-models.sh nemotron` (~81 ГиБ), затем `support-ctl model nemotron`; отдаётся под именем `deep`, конфиг агента не меняется.
- **Свой профиль модели на одном устройстве** (например, Nemotron как `deep` там, где нет весов gpt-oss): положить env-файл в `/etc/support/deep.env` (образец — `stack/env/nemotron.env`); `deploy.sh`/`support-ctl update` будут подставлять его вместо репозиторного `env/deep.env`. Убрать override — удалить файл и повторить `deploy.sh`.
- **Версии** (OpenCode, vLLM, модели): правка `install/versions.lock` в репозитории → `support-ctl update` → для образа/моделей вручную `bootstrap.sh` после удаления соответствующего маркера (`sudo rm /var/lib/support/bootstrap/vllm-image.done`).
- **ОС**: `sudo apt full-upgrade` — отдельно, перед выездом, с последующим `support-ctl bench` (минимум 60 tok/s на fast — норма 95).
- Что ещё посмотреть: `support-ctl version`.

## 5. Если что-то пошло не так

| Симптом | Причина / что делать |
|---|---|
| preflight: `DGX OS … older than` | OTA-обновление (§2 п.2) или `FORCE_OLD_OS=1 ./install/bootstrap.sh` на свой риск |
| `GPU not visible in container` | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`, повторить |
| `deep` (gpt-oss) отвечает 500, в логе `HarmonyError: error downloading or loading vocab file` | в `/opt/models/gpt-oss-120b/tiktoken/` нет словарей tiktoken (старая загрузка) | `install/fetch-models.sh deep` (докачает только словари), `support-ctl model deep` |
| `hf download` обрывается | повторить `bootstrap.sh` — докачает; при лимитах HF задать `HF_TOKEN=…` |
| `llm@fast failed` | `journalctl -u llm@fast -n 100`; частое — не хватило памяти из-за page cache: `sudo systemctl restart llm@fast` (юнит сам сбрасывает кэш) |
| vLLM не стартует на новой DGX OS | откатить образ: в `versions.lock` другая версия → `rm vllm-image.done` → `bootstrap.sh`; известные рабочие пары — `docs/bench-log.md` |
| `verify`: `mcp_smoke` падает | `support-ctl target test self`; если нет sshd — `sudo systemctl enable --now ssh` |
| `connectors_smoke`: `skip workbench` / `workbench image missing` | `support-ctl workbench build` (интернет); `docker network ls` должен показывать `workbench` |
| `workbench_run`: `cannot restrict the sandbox network` | `sudo -n /opt/support/bin/support-priv net-clear test` должно работать без пароля — проверить `/etc/sudoers.d/90-support-ui` (ставит `deploy.sh`) |
| CAN-адаптер есть, `observe` → `cannot open can0` | `sudo /opt/support/bin/support-priv can-restart can0 500000` (или `device_recover` из агента); для USB-CAN gs_usb — `lsmod \| grep gs_usb` |

## 6. Клонирование уже настроенного Spark

Проще bootstrap: веса и образ можно скопировать по сети (`rsync -a /opt/models/ new:/opt/models/`, `docker save | docker load`), затем `bootstrap.sh --skip-models` — он пропустит загрузку, но пройдёт все остальные фазы. Секреты (`/etc/support/*.env`, `/opt/support/keys/`) **не копировать** — у каждого устройства свои.
