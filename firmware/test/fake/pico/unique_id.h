/*
 * Host-build stand-in for <pico/unique_id.h>.
 *
 * Only exists so that firmware/src/protocol.c can be compiled and exercised on
 * the development machine (see firmware/test/run_crosscheck.ps1).
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef FAKE_PICO_UNIQUE_ID_H
#define FAKE_PICO_UNIQUE_ID_H

#include <stdint.h>

#define PICO_UNIQUE_BOARD_ID_SIZE_BYTES 8

typedef struct {
    uint8_t id[PICO_UNIQUE_BOARD_ID_SIZE_BYTES];
} pico_unique_board_id_t;

void pico_get_unique_board_id(pico_unique_board_id_t *id);

#endif /* FAKE_PICO_UNIQUE_ID_H */
