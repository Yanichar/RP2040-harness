/*
 * CRC-16/CCITT-FALSE.
 *
 * Control vector: harness_crc16_ccitt_false("123456789", 9) == 0x29B1
 *
 * SPDX-License-Identifier: MIT
 */

#include "crc16.h"

uint16_t harness_crc16_update(uint16_t crc, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x8000u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

uint16_t harness_crc16_ccitt_false(const uint8_t *data, size_t len) {
    return harness_crc16_update(HARNESS_CRC16_INIT, data, len);
}
