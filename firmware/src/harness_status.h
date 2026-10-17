/*
 * RP2040 Harness — protocol status codes (docs/protocol.md §2.4).
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_STATUS_H
#define HARNESS_STATUS_H

#define HARNESS_STATUS_OK               0x00u
#define HARNESS_STATUS_ERR_UNKNOWN_OPCODE 0x01u
#define HARNESS_STATUS_ERR_BAD_LENGTH   0x02u
#define HARNESS_STATUS_ERR_BAD_PARAM    0x03u
#define HARNESS_STATUS_ERR_BUSY         0x04u
#define HARNESS_STATUS_ERR_CRC          0x05u
#define HARNESS_STATUS_ERR_MAGIC        0x06u

#endif /* HARNESS_STATUS_H */
