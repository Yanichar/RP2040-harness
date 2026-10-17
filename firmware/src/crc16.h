/*
 * CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, xorout 0x0000).
 *
 * The incremental form exists because a protocol frame's checksum covers
 * non-contiguous regions of its byte buffer: the header fields
 * `opcode, seq, len` at offsets 1..6 are followed by the payload at offset 8,
 * with the CRC field itself sitting in between at 6..8.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_CRC16_H
#define HARNESS_CRC16_H

#include <stddef.h>
#include <stdint.h>

#define HARNESS_CRC16_INIT 0xFFFFu

/* Continue a CRC-16/CCITT-FALSE computation with another block of bytes. */
uint16_t harness_crc16_update(uint16_t crc, const uint8_t *data, size_t len);

/* One-shot form. Control vector: harness_crc16_ccitt_false("123456789", 9) == 0x29B1 */
uint16_t harness_crc16_ccitt_false(const uint8_t *data, size_t len);

#endif /* HARNESS_CRC16_H */
