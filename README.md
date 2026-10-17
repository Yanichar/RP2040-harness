# RP2040 Harness

Прибор для управления **24 цифровыми линиями** Raspberry Pi Pico W (RP2040) с ПК:

* **прошивка на C** (Pico SDK 2.3.1) — USB raw vendor, произвольный доступ к каждой
  линии, полный набор режимов площадки;
* **ПК-часть на Python** — программный API (`rp2040_harness`) и **MCP-сервер** для
  нейросети (DSH, stdio);
* **стенд и тесты** — логический анализатор gusmanb (24 канала) и скрипт
  параллельного счётчика.

Техническое задание: [`TASK.md`](TASK.md).
Протокол обмена: [`docs/protocol.md`](docs/protocol.md).

---

## Статус: что проверено, а что нет

Плата **Pico W прошита и проверена вживую** (прошивка 1.0.2, serial
`395E66CB37A863E6`). Логический анализатор всё ещё не поднят — он виден как
`Cypress FX2LP (unconfigured)` без COM-порта, поэтому измерения «через
анализатор» (карта пинов, межобновляемый джиттер) остаются в работе.

| Проверено на железе | Как именно |
|---|---|
| USB-перечисление, WinUSB через WCID (Zadig не нужен), оба интерфейса | диспетчер устройств: `RP2040 Harness Data` + `Reset`, вендорный драйвер сам |
| Полный набор операций на всех 24 линиях | `tests/bringup_check.py` — **37/37 проверок PASS** |
| Атомарные маски, батчи, ошибки, безопасное состояние | там же |
| Стабильность повторных подключений | 20 сеансов open/close подряд без сбоев |
| Фактическая скорость обновлений и латентность (ТЗ §8.4) | **148 600 обновлений/с**, латентность p95 **0.88 мс** — [`docs/measurements.md`](docs/measurements.md) |
| Прошивка без кнопки через reset-интерфейс (ТЗ §8.8) | `picotool`/BOOTSEL-цикл выполнен программно, без BOOTSEL-кнопки |

| Проверено без железа | Как именно |
|---|---|
| Прошивка собирается без единого предупреждения | `firmware/build.ps1`, `-Wall -Wextra` |
| **Протокол прошивки побайтово совпадает с хостом** | `firmware/test/crosscheck.py` — 16 проверок: код прошивки компилируется нативно и сверяется с python-кодеком |
| Python-пакет | `pytest` — 153 теста |
| Стендовые инструменты (анализатор, замеры, pinmap) | `pytest` — 100 тестов |
| Сценарий счётчика | `pytest` — 23 теста |
| MCP-сервер зарегистрирован и отвечает вживую | 10 инструментов в `.dsh/mcp.json`, живой вызов через мост |

| **Ещё НЕ проверено — нужен анализатор** | Почему |
|---|---|
| Эмпирическая карта пинов GP ↔ канал анализатора (ТЗ §8.7) | `docs/pinmap.md` ждёт реального захвата |
| Межобновляемый джиттер по захвату (ТЗ §3.2) | инструмент готов (`tests/counter.py --capture`), захвата нет |

Замечание: тождественность логического индекса и физического вывода проверена
**самосогласованностью** (драйвер по индексу и таблица трансляции дают одно и то
же на всех 24 линиях), но не внешним прибором — для этого и нужен анализатор.

Подробности и методика — [`docs/measurements.md`](docs/measurements.md).

---

## Состав репозитория

```
firmware/       прошивка (CMake + Pico SDK), build.ps1, нативные cross-check тесты
host/           python-пакет rp2040_harness: API, транспорт, блокировка, CLI, MCP-сервер
tests/          стенд: драйвер анализатора, .lac/.tcs, pinmap, замеры, счётчик
docs/           protocol.md, pinmap.md, measurements.md, quickstart.md
.dsh/mcp.json   регистрация MCP-сервера для DSH
pytest.ini      общая конфигурация тестов (tests/ + host/tests/)
```

---

## Быстрый старт

Полная версия — [`docs/quickstart.md`](docs/quickstart.md).

### 0. Окружение

```powershell
# Python-часть
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e host
.\.venv\Scripts\python.exe -m pip install pytest mcp
```

Сборка прошивки идёт **только** через Pico Setup: в системном `PATH` лежит
`arm-none-eabi-gcc` от Cypress FX3 SDK, несовместимый с RP2040.

### 1. Сборка прошивки

```powershell
cd firmware
.\build.ps1                 # → build\harness.uf2, build\harness.elf
```

### 2. Прошивка

Первый раз — с зажатой **BOOTSEL** (плата пустая):

```powershell
C:\Pico SDK v2.3.1\pico-sdk-tools\picotool\picotool.exe load -x firmware\build\harness.uf2
```

Дальше — **без кнопки**, через reset-интерфейс прошивки:

```powershell
cd firmware; .\build.ps1 -Flash
```

### 3. Проверка связи

```powershell
.\.venv\Scripts\python.exe -m rp2040_harness info
.\.venv\Scripts\python.exe -m rp2040_harness read
```

### 4. Использование из Python

```python
from rp2040_harness import Harness

with Harness.open() as h:
    h.configure(3, direction="out", drive="8mA", slew="fast",
                schmitt=True, pull=None, open_drain=False)
    h.write_mask(mask=0x0000FF, value=0x0000A5)   # атомарно, 24 бита
    print(h.read_all())                           # 24 бита входов
    h.batch([("set", 3, 1), ("set", 4, 0)])       # пакет в одной транзакции
```

### 5. MCP-сервер для нейросети

Зарегистрирован в [`.dsh/mcp.json`](.dsh/mcp.json) и уже доступен в DSH как
инструменты `mcp__rp2040-harness__*`:

| Инструмент | Назначение |
|---|---|
| `get_device_info` | версия протокола/прошивки, возможности, серийный номер |
| `list_pins` | конфигурация и уровни всех 24 линий |
| `configure_pin` | направление, подтяжка, drive, slew, Schmitt, open-drain, уровень |
| `set_pin` | установить уровень одной выходной линии |
| `set_pins_atomic` | атомарная 24-битная маска |
| `read_pins` | прочитать все 24 линии |
| `stop` | вернуть все линии в безопасное состояние (INPUT, без подтяжек) |
| `status` | счётчики кадров, ошибок CRC, uptime |
| `run_batch` | много операций в минимуме USB-транзакций |
| `stream` | выдать последовательность на максимальной скорости и измерить частоту |

Сервер держит устройство **эксклюзивно**: пока он открыт, CLI и API его не
получат. Через 30 с без вызовов он сам отпускает устройство
(`RP2040_HARNESS_IDLE_TIMEOUT`, `0` — не отпускать).

### 6. Тесты

```powershell
# всё сразу
.\.venv\Scripts\python.exe -m pytest -q

# отдельно
.\.venv\Scripts\python.exe -m pytest host\tests -q      # python-пакет
.\.venv\Scripts\python.exe -m pytest tests -q           # стенд и счётчик

# протокол: код прошивки компилируется нативно и сверяется с хостом
.\.venv\Scripts\python.exe firmware\test\crosscheck.py
```

### 7. Стенд

```powershell
# карта пинов (нужен подключённый анализатор)
.\.venv\Scripts\python.exe tests\pinmap_scan.py --port COM14 --dry-run

# счётчик: паттерн + захват (.lac + .vcd) + отчёт о скорости
.\.venv\Scripts\python.exe tests\counter.py --capture --port COM14 --values 100000

# только скорость, без анализатора
.\.venv\Scripts\python.exe tests\counter.py --values 50000
```

---

## Требования к железу

| Что | Значение |
|---|---|
| МК | Raspberry Pi Pico W (RP2040) |
| Линии | GP2–GP22 (индекс 0–20) + GP26–GP28 (индекс 21–23) = **24** |
| Свободны | GP0/GP1 — UART-отладка (115200 8N1), GP23/24/25/29 — заняты платой |
| Уровни | 3.3 В. **5 В на линии не подавать.** Open-drain — только с внешними подтяжками |
| Связь | USB Full-Speed, raw vendor, WinUSB через WCID (Zadig не нужен) |
| Анализатор | gusmanb 24 канала, `C:\Users\Yanichar\Downloads\all-in-one_6.0.0.1-win-x64` |

## Безопасность

* После boot/reset все 24 линии — **INPUT без подтяжек**; прошивка приводит их в
  это состояние до инициализации USB.
* При **отключении USB** поток останавливается, все линии возвращаются в INPUT.
* `stop` / `Harness.stop()` / `all_inputs()` — то же самое по команде.
* USB **suspend** намеренно не сбрасывает линии (короткие паузы шины не рвут
  состояние).

## Идентификация устройства

| Параметр | Значение | Как изменить |
|---|---|---|
| VID/PID | `0x2E8A:0x10C0` ❓ | `cmake -DHARNESS_USB_VID=... -DHARNESS_USB_PID=...` |
| bcdUSB | `0x0210` | нужен для MS OS 2.0 (WCID) |
| Serial | 16 hex-символов flash unique id | — |

VID/PID выбраны исполнителем и **ожидают подтверждения заказчика**
(ТЗ §11.1). GUID интерфейса данных: `{7f0d2b1a-4c6e-4a2f-9d31-5b7c8e0f1a22}`.

## Лицензия

MIT.
