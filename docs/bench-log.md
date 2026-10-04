# Замеры на ASUS Ascent GX10 (GB10, 121 ГиБ)

Среда: DGX OS 7.5.0 + OTA 7.6.0, ядро 7.0.0-1019-nvidia, драйвер 580.178.04, Docker 29.6.2, toolkit 1.20.1.
Скрипт: `tests/bench.py` (400 токенов на ответ, temperature 0, прогрев отброшен).

| Дата | Профиль | Модель / рантайм | Single tok/s | TTFT | 8 потоков агрегат | Prefix-cache | Tool-call | Память |
|---|---|---|---|---|---|---|---|---|
| 2026-10-04 | fast | Qwen3.6-35B-A3B-NVFP4, vllm/vllm-openai:v0.28.0, flashinfer+marlin, fp8 KV, ctx 131K, util 0.45 | 79.2 | 71 мс | 311 (39/поток) | 5.2K промпт: 66 → 75 tok/s | OK | веса 20.4 ГиБ, KV 27.3 ГиБ (2.59M токенов), старт 5 мин |
