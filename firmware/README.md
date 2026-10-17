# RP2040 Harness — прошивка

Прошивка для **Raspberry Pi Pico W (RP2040)** на **Pico SDK 2.3.1**.
Устройство предоставляет ПК доступ к 24 цифровым линиям по USB raw vendor
протоколу (TinyUSB).

Описание протокола — [`../docs/protocol.md`](../docs/protocol.md).
Требования — [`../TASK.md`](../TASK.md).

## Что внутри

| Файл | Назначение |
|---|---|
| `src/main.c` | точка входа, безопасное состояние, цикл `tud_task()`, реакция на отключение USB |
| `src/harness_config.h` | карта линий (индекс ↔ GPIO), версии, идентификаторы USB |
| `src/gpio_ctrl.c/.h` | конфигурация пинов и атомарные операции с SIO |
| `src/protocol.c/.h` | кадрирование, CRC16, разбор запросов, сборка ответов |
| `src/crc16.c/.h` | CRC-16/CCITT-FALSE |
| `src/usb_descriptors.c` | дескрипторы устройства, BOS + Microsoft OS 2.0 (WCID) |
| `src/usb_vendor.c` | собственный class-driver TinyUSB для интерфейса 0 |
| `src/tusb_config.h` | конфигурация TinyUSB |
| `src/harness_status.h` | коды статуса протокола |
| `build.ps1` | сборка (и опционально прошивка) |

## Требования к окружению

* **Pico SDK 2.3.1** — `C:\Pico SDK v2.3.1\pico-sdk`.
* ARM-тулчейн из состава Pico Setup — `C:\Pico SDK v2.3.1\arm-gnu-toolchain`.
* `ninja`, `cmake` — из состава Pico Setup.

> ⚠️ **Важно.** В системном `PATH` этой машины лежит `arm-none-eabi-gcc` от
> **Cypress FX3 SDK** — он несовместим с RP2040. Сборка всегда выполняется
> внутри окружения Pico Setup (`pico-env.cmd`), которое выставляет корректный
> тулчейн первым в `PATH`. `build.ps1` делает это сам; при ручной сборке
> сначала выполните `C:\Pico SDK v2.3.1\pico-env.ps1`.

## Сборка

```powershell
cd firmware
.\build.ps1                     # Release
.\build.ps1 -Clean -Config Debug
```

Скрипт печатает точные пути к артефактам. После успешной сборки:

```
build/harness.uf2      # для заливки
build/harness.elf      # для отладки / disassembly
build/harness.bin
build/harness.hex
build/harness.dis
build/harness.elf.map
```

Ручная сборка (без скрипта):

```powershell
C:\Pico SDK v2.3.1\pico-env.cmd
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release `
      -DPICO_SDK_PATH="C:/Pico SDK v2.3.1/pico-sdk" -DPICO_BOARD=pico_w
cmake --build build --parallel
```

Смена идентификаторов USB (см. `docs/protocol.md` §6 — значения ожидают
подтверждения заказчика):

```powershell
cmake -S . -B build -G Ninja -DHARNESS_USB_VID=0x2E8A -DHARNESS_USB_PID=0x10C0 ...
```

## Прошивка

`picotool` лежит в `C:\Pico SDK v2.3.1\pico-sdk-tools\picotool\picotool.exe`.

**Первая заливка** (плата пустая, прошивки ещё нет) — единственный раз, когда
нужна кнопка:

1. Зажать **BOOTSEL**, подключить USB, отпустить.
2. Появится накопитель `RP2-RP2040`.
3. `picotool load -x build\harness.uf2`

**Все последующие заливки** — без кнопки, через reset-интерфейс прошивки:

```powershell
.\build.ps1 -Flash
```

или вручную:

```powershell
picotool reboot -f -u                       # устройство уходит в BOOTSEL
picotool load -x build\harness.uf2
```

Reset-интерфейс — это второй USB-интерфейс устройства
(`bInterfaceClass=0xFF, bInterfaceSubClass=0x00, bInterfaceProtocol=0x01`),
ровно тот, который ожидает `picotool`.

## Отладка

Консоль `printf` выведена на **UART0: TX = GP0, RX = GP1** (115200 8N1).
Эти линии вне карты 24 рабочих пинов (TASK.md §2). USB CDC для отладки
**не используется** — USB-устройство отдаёт только raw vendor интерфейс.

## Состояние и безопасность

* После boot/reset **все 24 линии — INPUT без подтяжек**. Прошивка приводит их
  в это состояние до инициализации USB.
* При **отключении USB** (`tud_umount_cb`) поток останавливается, все линии
  возвращаются в INPUT без подтяжек.
* USB **suspend** намеренно **не** сбрасывает линии — короткие паузы шины не
  должны рвать состояние (`docs/protocol.md` §5.2).
* 5 В на линии не подавать. Open-drain — только с внешними подтяжками.

## Проверка сборки без железа

```powershell
# артефакты на месте
Get-ChildItem build\harness.* 
# семейство и точка входа
picotool info build\harness.uf2
```
