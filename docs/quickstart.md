# Быстрый старт

Пошаговая инструкция от пустого окружения до работающего прибора.
Полный контекст — [`../README.md`](../README.md), требования — [`../TASK.md`](../TASK.md).

---

## 0. Что понадобится

* Pico W (RP2040), кабель USB **data** (не только зарядка).
* Pico Setup 2.3.1 в `C:\Pico SDK v2.3.1` (SDK, ARM-тулчейн, ninja, picotool).
* Python 3.11+.
* Для стенда: логический анализатор gusmanb, 24 канала.

> ⚠️ В системном `PATH` этой машины лежит `arm-none-eabi-gcc` от **Cypress FX3
> SDK** — он несовместим с RP2040. Сборка всегда идёт через `pico-env`, а
> `firmware/build.ps1` делает это сам.

---

## 1. Python-окружение

```powershell
cd C:\Users\Yanichar\work\RP2040-harness
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e host
.\.venv\Scripts\python.exe -m pip install pytest mcp
```

Проверка:

```powershell
.\.venv\Scripts\python.exe -c "import rp2040_harness, usb, libusb_package; print('ok')"
```

`libusb-package` приносит `libusb-1.0.dll`, поэтому отдельно ставить libusb не
нужно.

---

## 2. Сборка прошивки

```powershell
cd firmware
.\build.ps1
```

Ожидаемый вывод в конце:

```
Build OK
  UF2 : ...\firmware\build\harness.uf2
  ELF : ...\firmware\build\harness.elf
```

Проверить артефакт, не подключая плату:

```powershell
C:\Pico SDK v2.3.1\pico-sdk-tools\picotool\picotool.exe info firmware\build\harness.uf2
# Program Information
#  name:          harness
#  features:      UART stdin / stdout
```

---

## 3. Первая заливка (единственный раз с кнопкой)

1. Отключить Pico от USB.
2. Зажать **BOOTSEL**, подключить USB, отпустить.
3. Появится накопитель `RP2-RP2040`.
4. Залить:

```powershell
C:\Pico SDK v2.3.1\pico-sdk-tools\picotool\picotool.exe load -x firmware\build\harness.uf2
```

Плата перезагрузится и запустит прошивку. В диспетчере устройств появится
`RP2040 Harness` с **двумя** интерфейсами; драйвер WinUSB подставится
автоматически (WCID), Zadig не нужен.

### Все последующие заливки — без кнопки

```powershell
cd firmware
.\build.ps1 -Flash
```

`picotool reboot -f -u` уходит в BOOTSEL через reset-интерфейс прошивки
(интерфейс 1, class `0xFF`, subclass `0x00`, protocol `0x01`), затем
заливается свежий `.uf2`.

---

## 4. Проверка связи

```powershell
# информация об устройстве
.\.venv\Scripts\python.exe -m rp2040_harness info

# состояние всех линий (после boot — все INPUT без подтяжек)
.\.venv\Scripts\python.exe -m rp2040_harness pins

# прочитать 24 линии
.\.venv\Scripts\python.exe -m rp2040_harness read
```

Если устройство не найдено — сообщение назовёт VID:PID и подскажет проверить
кабель, прошивку и привязку WinUSB.

### Отладочная консоль

`printf` прошивки идёт на **UART0: TX = GP0, RX = GP1, 115200 8N1**. Эти линии
вне карты рабочих пинов. USB CDC для отладки не используется.

---

## 5. Первый сценарий: помигать линией

```python
from rp2040_harness import Harness
import time

with Harness.open() as h:
    print(h.get_info().firmware_version, h.get_info().serial)
    h.configure(0, direction="out", drive="4mA", slew="slow", level=0)
    for _ in range(10):
        h.set_pin(0, 1); time.sleep(0.05)
        h.set_pin(0, 0); time.sleep(0.05)
    h.stop()          # все линии → INPUT без подтяжек
```

Атомарная установка сразу нескольких линий:

```python
with Harness.open() as h:
    h.configure_many([{"index": i, "direction": "out"} for i in range(8)])
    h.write_mask(mask=0x0000FF, value=0x0000A5)   # одна запись в SIO
    print(f"{h.read_all():06X}")
```

---

## 6. Стенд: карта пинов

Анализатор не знает, какой его канал подключён к какому GP. Карта определяется
эмпирически:

```powershell
# 1. узнать, что видит анализатор (своего CLI у analyzer.py нет — он библиотека)
.\.venv\Scripts\python.exe -c "import sys,json; sys.path.insert(0,'tests'); import analyzer; print('ports:', analyzer.available_ports()); print(json.dumps(analyzer.probe('COM14'), indent=2))"

# 2. посмотреть, что будет сделано (ничего не трогая)
.\.venv\Scripts\python.exe tests\pinmap_scan.py --port COM14 --dry-run

# 3. реальный проход: по очереди поднимает каждую линию и снимает захват
.\.venv\Scripts\python.exe tests\pinmap_scan.py --port COM14
```

Успешный проход пишет [`pinmap.md`](pinmap.md) с подтверждённым соответствием.

> Анализатор нужно сначала вывести из состояния `unconfigured` (драйвер FX2LP) и
> освободить его COM-порт: закрыть `LogicAnalyzer.exe`, если он запущен.

---

## 7. Стенд: счётчик и замер скорости

```powershell
# только скорость через Python API, без анализатора
.\.venv\Scripts\python.exe tests\counter.py --values 50000

# паттерн + захват (.lac + .vcd) + отчёт о частоте/джиттере
.\.venv\Scripts\python.exe tests\counter.py --capture --port COM14 `
    --values 100000 --trigger edge:1 --frequency 20000000

# интерактивный путь (set_pin/read_pins) вместо пакетного
.\.venv\Scripts\python.exe tests\counter.py --mode manual --values 500
```

Артефакты складываются в `tests/captures/counter_<timestamp>/`:
`counter.tcs`, `counter.lac`, `counter.vcd`, `counter.summary.json`,
`counter.measure.md`, `counter.measure.json`.

Отдельный пересчёт по уже снятому захвату:

```powershell
# текст
.\.venv\Scripts\python.exe tests\measure.py tests\captures\...\counter.lac
# JSON, только каналы 1-8, плюс markdown-отчёт
.\.venv\Scripts\python.exe tests\measure.py tests\captures\...\counter.lac `
    --channels 1-8 --json --markdown report.md
# .csv требует явной частоты дискретизации
.\.venv\Scripts\python.exe tests\measure.py tests\captures\...\counter.csv --frequency 20000000
```

> ⚠️ `TerminalCapture capture` **блокируется до срабатывания триггера и не имеет
> таймаута**. Инструменты в `tests/analyzer.py` запускают его фоном с жёстким
> дедлайном и после kill обязательно выполняют abort/reopen, потому что убитый
> захват оставляет устройство «заряженным».

---

## 8. MCP-сервер

Уже зарегистрирован в [`.dsh/mcp.json`](../.dsh/mcp.json). Проверить
конфигурацию:

```powershell
node $env:USERPROFILE\.dsh\skills\mcp-register\validate.mjs
```

Запустить вручную (обычно не нужно — DSH поднимает его сам):

```powershell
.\.venv\Scripts\python.exe -m rp2040_harness.mcp_server
```

Инструменты появляются в сессии как `mcp__rp2040-harness__*` в течение ~1 с,
перезапуск не нужен.

---

## 9. Тесты

```powershell
.\.venv\Scripts\python.exe -m pytest -q                  # все 276
.\.venv\Scripts\python.exe -m pytest host\tests -q       # 153 — python-пакет
.\.venv\Scripts\python.exe -m pytest tests -q            # 123 — стенд и счётчик
.\.venv\Scripts\python.exe firmware\test\crosscheck.py   # 16 — протокол C ↔ Python
```

`crosscheck.py` — самая содержательная проверка при отсутствии железа: он
компилирует **настоящий** `firmware/src/protocol.c` под эту машину (mingw gcc),
прогоняет через него кадры, закодированные python-пакетом, и декодирует ответы
тем же пакетом. Любое расхождение CRC, кадрирования или раскладки полей валит
прогон.

---

## 10. Частые проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| `no USB device with VID:PID 2E8A:10C0` | плата не подключена / прошивка не запущена / WinUSB не привязан | проверить кабель и диспетчер устройств; перепрошить |
| `DeviceBusyError` | устройство держит другой процесс (MCP-сервер, CLI) | закрыть его или подождать 30 с простоя MCP |
| Сборка падает на `arm-none-eabi-gcc` | в `PATH` попал тулчейн Cypress FX3 | собирать только через `build.ps1` / `pico-env` |
| `tusb_config.h: No such file` | сборка не из `firmware/` | собирать из `firmware/` |
| Захват «завис» | триггер не сработал, таймаута у `TerminalCapture` нет | инструменты убьют его по дедлайну и выполнят abort/reopen |
| Порт анализатора занят | запущен `LogicAnalyzer.exe` | закрыть его |
| Декодеры в GUI не работают | несовместимость ABI Python 3.14 | декодировать из `.vcd` в PulseView/`sigrok-cli` |
