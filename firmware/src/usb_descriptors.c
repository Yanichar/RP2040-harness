/*
 * RP2040 Harness — USB descriptors.
 *
 * Two interfaces:
 *   0  raw vendor data interface  (bulk OUT 0x01 / bulk IN 0x81)
 *   1  Pico SDK reset interface   (no endpoints; used by `picotool reboot -f -u`)
 *
 * Microsoft OS 2.0 (WCID) descriptors are supplied so Windows binds WinUSB to
 * BOTH interfaces automatically — no Zadig, no manual driver install
 * (TASK.md §3.3). The descriptor set contains one Function Subset per
 * interface; the reset interface keeps the Pico SDK's own DeviceInterfaceGUID
 * so that existing tooling recognises it.
 *
 * SPDX-License-Identifier: MIT
 */

#include "usb_descriptors.h"

#include <string.h>

#include "pico/unique_id.h"
#include "pico/usb_reset.h" /* TUD_RPI_RESET_DESC_LEN / TUD_RPI_RESET_DESCRIPTOR */
#include "tusb.h"

#include "harness_config.h"
#include "usb_vendor.h"

/* -------------------------------------------------------------------------- */
/* String indices                                                             */
/* -------------------------------------------------------------------------- */

#define USBD_STR_0       0x00u
#define USBD_STR_MANUF   0x01u
#define USBD_STR_PRODUCT 0x02u
#define USBD_STR_SERIAL  0x03u
#define USBD_STR_RESET   0x05u

/* -------------------------------------------------------------------------- */
/* Configuration descriptor                                                   */
/* -------------------------------------------------------------------------- */

/* Interface descriptor + two full-speed bulk endpoints. */
#define HARNESS_DATA_DESC_LEN (9 + 7 + 7)

#define TUD_HARNESS_DESCRIPTOR(_itfnum, _stridx, _epout, _epin, _epsize)          \
    /* Interface */                                                              \
    9, TUSB_DESC_INTERFACE, _itfnum, 0, 2, TUSB_CLASS_VENDOR_SPECIFIC,            \
        HARNESS_ITF_SUBCLASS, HARNESS_ITF_PROTOCOL, _stridx,                      \
        /* Endpoint Out */                                                        \
        7, TUSB_DESC_ENDPOINT, _epout, TUSB_XFER_BULK, U16_TO_U8S_LE(_epsize), 0, \
        /* Endpoint In */                                                         \
        7, TUSB_DESC_ENDPOINT, _epin, TUSB_XFER_BULK, U16_TO_U8S_LE(_epsize), 0

#define HARNESS_DESC_LEN \
    (TUD_CONFIG_DESC_LEN + HARNESS_DATA_DESC_LEN + TUD_RPI_RESET_DESC_LEN)

static const tusb_desc_device_t usbd_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    /* bcdUSB >= 0x0210 is required for the Microsoft OS 2.0 descriptor set. */
    .bcdUSB = 0x0210,
    .bDeviceClass = TUSB_CLASS_UNSPECIFIED,
    .bDeviceSubClass = 0,
    .bDeviceProtocol = 0,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = HARNESS_USB_VID,
    .idProduct = HARNESS_USB_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = USBD_STR_MANUF,
    .iProduct = USBD_STR_PRODUCT,
    .iSerialNumber = USBD_STR_SERIAL,
    .bNumConfigurations = 1,
};

static const uint8_t usbd_desc_cfg[HARNESS_DESC_LEN] = {
    TUD_CONFIG_DESCRIPTOR(1, HARNESS_ITF_COUNT, USBD_STR_0, HARNESS_DESC_LEN, 0, 250),

    TUD_HARNESS_DESCRIPTOR(HARNESS_ITF_DATA, HARNESS_STR_DATA, HARNESS_EP_DATA_OUT,
                           HARNESS_EP_DATA_IN, HARNESS_EP_SIZE),

    TUD_RPI_RESET_DESCRIPTOR(HARNESS_ITF_RESET, USBD_STR_RESET),
};

/* -------------------------------------------------------------------------- */
/* String descriptors                                                         */
/* -------------------------------------------------------------------------- */

#ifndef USBD_DESC_STR_MAX
#define USBD_DESC_STR_MAX 20
#endif

static char usbd_serial_str[PICO_UNIQUE_BOARD_ID_SIZE_BYTES * 2 + 1];

static const char *const usbd_desc_str[] = {
    [USBD_STR_MANUF] = HARNESS_USB_MANUFACTURER,
    [USBD_STR_PRODUCT] = HARNESS_USB_PRODUCT,
    [USBD_STR_SERIAL] = usbd_serial_str,
    [HARNESS_STR_DATA] = "RP2040 Harness Data",
    [USBD_STR_RESET] = "Reset",
};

const uint8_t *tud_descriptor_device_cb(void) {
    return (const uint8_t *)&usbd_desc_device;
}

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return usbd_desc_cfg;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;

    static uint16_t desc_str[USBD_DESC_STR_MAX];

    if (!usbd_serial_str[0]) {
        pico_get_unique_board_id_string(usbd_serial_str, sizeof(usbd_serial_str));
    }

    uint8_t len;
    if (index == 0u) {
        desc_str[1] = 0x0409; /* English (US) */
        len = 1;
    } else {
        if (index >= sizeof(usbd_desc_str) / sizeof(usbd_desc_str[0])) {
            return NULL;
        }
        const char *str = usbd_desc_str[index];
        if (!str) {
            return NULL;
        }
        for (len = 0; len < USBD_DESC_STR_MAX - 1 && str[len]; ++len) {
            desc_str[1 + len] = (uint16_t)(uint8_t)str[len];
        }
    }

    desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * len + 2));
    return desc_str;
}

/* -------------------------------------------------------------------------- */
/* Microsoft OS 2.0 descriptor set (WCID)                                     */
/* -------------------------------------------------------------------------- */

/*
 * Registry property descriptor: "DeviceInterfaceGUID" (REG_SZ) -> GUID.
 * 2 + 2 + 2 + 2 + 40 (name) + 2 + 78 (GUID) = 128 = 0x80 bytes.
 */
#define HARNESS_DEVICE_INTERFACE_GUID_NAME_UTF16   \
    'D', 0, 'e', 0, 'v', 0, 'i', 0, 'c', 0, 'e', 0, 'I', 0, 'n', 0, 't', 0, 'e', 0, \
    'r', 0, 'f', 0, 'a', 0, 'c', 0, 'e', 0, 'G', 0, 'U', 0, 'I', 0, 'D', 0, 0, 0

/* {7f0d2b1a-4c6e-4a2f-9d31-5b7c8e0f1a22} — the harness data interface. */
#define HARNESS_GUID_DATA_UTF16                                     \
    '{', 0, '7', 0, 'f', 0, '0', 0, 'd', 0, '2', 0, 'b', 0, '1', 0, 'a', 0, '-', 0, \
    '4', 0, 'c', 0, '6', 0, 'e', 0, '-', 0, '4', 0, 'a', 0, '2', 0, 'f', 0, '-', 0, \
    '9', 0, 'd', 0, '3', 0, '1', 0, '-', 0, '5', 0, 'b', 0, '7', 0, 'c', 0, '8', 0, \
    'e', 0, '0', 0, 'f', 0, '1', 0, 'a', 0, '2', 0, '2', 0, '}', 0, 0, 0

/* {bc7398c1-73cd-4cb7-98b8-913a8fca7bf6} — identical to the Pico SDK's reset
 * interface GUID, so picotool keeps recognising the interface. */
#define HARNESS_GUID_RESET_UTF16                                    \
    '{', 0, 'b', 0, 'c', 0, '7', 0, '3', 0, '9', 0, '8', 0, 'c', 0, '1', 0, '-', 0, \
    '7', 0, '3', 0, 'c', 0, 'd', 0, '-', 0, '4', 0, 'c', 0, 'b', 0, '7', 0, '-', 0, \
    '9', 0, '8', 0, 'b', 0, '8', 0, '-', 0, '9', 0, '1', 0, '3', 0, 'a', 0, '8', 0, \
    'f', 0, 'c', 0, 'a', 0, '7', 0, 'b', 0, 'f', 0, '6', 0, '}', 0, 0, 0

#define HARNESS_MS_OS_20_SUBSET_LEN (0x08 + 0x14 + 0x80)

#define HARNESS_MS_OS_20_FUNCTION_SUBSET(_itfnum, _guid)                        \
    /* Function Subset header */                                                \
    U16_TO_U8S_LE(0x0008), U16_TO_U8S_LE(MS_OS_20_SUBSET_HEADER_FUNCTION),      \
        _itfnum, 0, U16_TO_U8S_LE(HARNESS_MS_OS_20_SUBSET_LEN),                 \
        /* MS OS 2.0 Compatible ID descriptor: "WINUSB" */                      \
        U16_TO_U8S_LE(0x0014), U16_TO_U8S_LE(MS_OS_20_FEATURE_COMPATBLE_ID),    \
        'W', 'I', 'N', 'U', 'S', 'B', 0x00, 0x00,                               \
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,                         \
        /* MS OS 2.0 Registry property descriptor: DeviceInterfaceGUID */       \
        U16_TO_U8S_LE(0x0080), U16_TO_U8S_LE(MS_OS_20_FEATURE_REG_PROPERTY),    \
        U16_TO_U8S_LE(0x0001), U16_TO_U8S_LE(0x0028),                           \
        HARNESS_DEVICE_INTERFACE_GUID_NAME_UTF16,                               \
        U16_TO_U8S_LE(0x004E), _guid

#define HARNESS_MS_OS_20_DESC_LEN \
    (0x0A + HARNESS_MS_OS_20_SUBSET_LEN + HARNESS_MS_OS_20_SUBSET_LEN)

static const uint8_t desc_ms_os_20[] = {
    /* Set header */
    U16_TO_U8S_LE(0x000A), U16_TO_U8S_LE(MS_OS_20_SET_HEADER_DESCRIPTOR),
    U32_TO_U8S_LE(0x06030000), U16_TO_U8S_LE(HARNESS_MS_OS_20_DESC_LEN),

    HARNESS_MS_OS_20_FUNCTION_SUBSET(HARNESS_ITF_DATA, HARNESS_GUID_DATA_UTF16),
    HARNESS_MS_OS_20_FUNCTION_SUBSET(HARNESS_ITF_RESET, HARNESS_GUID_RESET_UTF16),
};

TU_VERIFY_STATIC(sizeof(desc_ms_os_20) == HARNESS_MS_OS_20_DESC_LEN,
                 "MS OS 2.0 descriptor set size mismatch");

#define BOS_TOTAL_LEN (TUD_BOS_DESC_LEN + TUD_BOS_MICROSOFT_OS_DESC_LEN)

static const uint8_t desc_bos[] = {
    TUD_BOS_DESCRIPTOR(BOS_TOTAL_LEN, 1),
    TUD_BOS_MS_OS_20_DESCRIPTOR(HARNESS_MS_OS_20_DESC_LEN, 1),
};

TU_VERIFY_STATIC(sizeof(desc_bos) == BOS_TOTAL_LEN, "BOS descriptor size mismatch");

const uint8_t *tud_descriptor_bos_cb(void) {
    return desc_bos;
}

/*
 * This is the TinyUSB "vendor request" hook. It is the only place a
 * GET_MS_DESCRIPTOR request can be answered, so it owns the whole MS OS 2.0
 * surface for the device.
 *
 * Note that TinyUSB routes *every* vendor-type control request here, for any
 * recipient and interface — a class-type request (bmRequestType & 0x60 ==
 * 0x20) is what reaches `usb_reset_interface_control_xfer_cb` for picotool.
 */
bool tud_vendor_control_xfer_cb(uint8_t rhport, uint8_t stage,
                                tusb_control_request_t const *request) {
    if (stage != CONTROL_STAGE_SETUP) {
        return true;
    }

    if (request->bRequest == 1u && request->wIndex == 7u) {
        return tud_control_xfer(rhport, request, (void *)(uintptr_t)desc_ms_os_20,
                                sizeof(desc_ms_os_20));
    }

    /*
     * Harness diagnostics. These are the recovery channel for a device whose
     * bulk endpoints have stopped responding: control transfers keep working.
     */
    if (request->bRequest == HARNESS_CTRL_GET_STATE) {
        /*
         * STATIC, not a local: tud_control_xfer() only starts the transfer — the
         * stack reads the buffer as the host polls EP0 across later
         * tud_task() iterations, long after this stack frame is gone.
         *
         * The reply is clamped to wLength so a host that asks for more than we
         * have gets a short packet instead of a stalled control pipe.
         */
        static harness_usb_state_t state;
        harness_usb_vendor_state(&state);
        const uint16_t len = tu_min16((uint16_t)sizeof(state), request->wLength);
        return tud_control_xfer(rhport, request, &state, len);
    }
    if (request->bRequest == HARNESS_CTRL_REARM) {
        harness_usb_vendor_rearm();
        return tud_control_xfer(rhport, request, NULL, 0u);
    }

    return false;
}
