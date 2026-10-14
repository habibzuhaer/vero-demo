# VERO

Автономное веб-приложение: бэктесты стратегий TRADE на тикерах XTB, история, топ, обучение (кластеры → правила риска).

Все runtime-данные живут в `data/` внутри проекта. MTB нужен только для одноразового bootstrap.

## Быстрый старт

```bash
cd .
chmod +x start.sh

# 1) Подготовить data/ (каталог + symlink свечей)
./start.sh bootstrap

# 2) Проверка модулей
./start.sh preflight

# 3) Сервер + UI
./start.sh
```

Открыть: **http://127.0.0.1:8900**

## Режимы на главной

| Режим | Назначение |
|-------|------------|
| **Smoke** | seed-тикеры, 1h, 2 стратегии (~минуты) |
| **Daily** | full universe, анти-бан, лимит 4–8 ч/день |
| **Full catalog** | все локальные тикеры × 3 TF × 7 стратегий |
| **Qualified / Top** | подмножества из скринера |
| **Learn** | кластеры и risk rules |
| **Preflight** | health-check модулей |
| **Bootstrap data** | скопировать каталог / связать свечи |

CLI-аналоги: `./start.sh smoke`, `./start.sh preflight`, `./start.sh bootstrap`.

## Данные (автономно)

| Путь | Содержимое |
|------|------------|
| `data/catalog/xtb_symbols_cache.json` | каталог XTB |
| `data/catalog/xtb_qualified_latest.json` | qualified |
| `data/catalog/xtb_top_pairs.json` | top-пары |
| `data/candles/vero_candles.db` | свечи 30m/1h/4h (symlink или копия) |
| `data/vero.db` | прогоны, результаты, learn |

Bootstrap-источник (только первый раз): `VERO_BOOTSTRAP_FROM` → MTB `data/`.

## Переменные окружения

См. `.env.example`. Основные:

- `VERO_YAHOO_FALLBACK=false` — без Yahoo (рекомендуется)
- `VERO_REQUIRE_LOCAL=true` — только тикеры с локальными свечами
- `VERO_BOOTSTRAP_COPY=1` — полная копия БД свечей вместо symlink

## Тесты

```bash
./start.sh test
python -m agents.control   # preflight
```
