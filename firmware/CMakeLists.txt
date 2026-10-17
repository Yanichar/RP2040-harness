cmake_minimum_required(VERSION 3.13)

# ---------------------------------------------------------------------------
# RP2040 Harness — firmware (Pico SDK 2.3.1, Raspberry Pi Pico W)
#
# Build ONLY through the Pico SDK environment: the machine's PATH carries an
# arm-none-eabi-gcc from the Cypress FX3 SDK, which is not compatible with the
# RP2040. Use `pico-env.ps1` / `build.ps1`, or pass -DPICO_SDK_PATH and put the
# SDK's arm-gnu-toolchain ahead on PATH yourself.
# ---------------------------------------------------------------------------

if (NOT DEFINED PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH "$ENV{PICO_SDK_PATH}")
endif ()
if (NOT DEFINED PICO_SDK_PATH)
    # Known install on this workstation (note the spaces — handled by the SDK import).
    set(PICO_SDK_PATH "C:/Pico SDK v2.3.1/pico-sdk" CACHE PATH "Path to the Raspberry Pi Pico SDK")
endif ()

include(${CMAKE_CURRENT_LIST_DIR}/pico_sdk_import.cmake)

if (NOT DEFINED PICO_BOARD)
    set(PICO_BOARD pico_w CACHE STRING "Pico board type")
endif ()

project(rp2040_harness C CXX ASM)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# Device identity — see docs/protocol.md §6. Both are pending customer
# confirmation (TASK.md §11.1) and are overridable from the command line.
set(HARNESS_USB_VID "0x2E8A" CACHE STRING "USB vendor id")
set(HARNESS_USB_PID "0x10C0" CACHE STRING "USB product id")

pico_sdk_init()

message(STATUS "Pico SDK      : ${PICO_SDK_VERSION_STRING} (${PICO_SDK_PATH})")
message(STATUS "Board         : ${PICO_BOARD}")
message(STATUS "USB VID/PID   : ${HARNESS_USB_VID}:${HARNESS_USB_PID}")

add_executable(harness
    src/main.c
    src/crc16.c
    src/gpio_ctrl.c
    src/protocol.c
    src/usb_descriptors.c
    src/usb_vendor.c
)

target_include_directories(harness PRIVATE ${CMAKE_CURRENT_LIST_DIR}/src)

target_compile_definitions(harness PRIVATE
    HARNESS_USB_VID=${HARNESS_USB_VID}
    HARNESS_USB_PID=${HARNESS_USB_PID}
    # We drive TinyUSB directly, so the SDK's defaults for the reset interface
    # are all "off". Turn the vendor reset interface back on explicitly, and
    # keep the MS OS 2.0 descriptor support off because we supply our own
    # combined BOS/MS-OS-2.0 descriptor covering both interfaces.
    PICO_ENABLE_USB_RESET_VIA_VENDOR_INTERFACE=1
    PICO_USB_RESET_SUPPORT_MS_OS_20_DESCRIPTOR=0
    PICO_USB_RESET_INCLUDE_DEFAULT_APP_DRIVER_CB=0
)

# -Wno-unused-const-variable: pico/usb_reset_tusb.h defines a file-scope
# `static const usbd_class_driver_t usb_reset_interface_driver`. Every TU that
# needs the reset descriptor macro therefore carries an unused copy of it. The
# driver itself is genuinely used in src/usb_vendor.c.
target_compile_options(harness PRIVATE -Wall -Wextra -Wno-unused-parameter
    -Wno-unused-const-variable)

target_link_libraries(harness PRIVATE
    pico_stdlib
    pico_unique_id
    pico_usb_reset
    tinyusb_device
    hardware_gpio
    hardware_timer
    hardware_sync
)

# Debug console on UART0 (GP0/GP1) — those pins are explicitly out of scope for
# the harness lines (TASK.md §2). USB CDC stdio is NOT used: the USB device
# exposes a raw vendor interface only.
pico_enable_stdio_uart(harness 1)
pico_enable_stdio_usb(harness 0)

# .uf2 / .elf / .bin / .hex / .map / .dis
pico_add_extra_outputs(harness)
