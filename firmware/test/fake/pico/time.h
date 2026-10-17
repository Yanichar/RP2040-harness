/*
 * Host-build stand-in for <pico/time.h>.
 *
 * Only exists so that firmware/src/protocol.c can be compiled and exercised on
 * the development machine (see firmware/test/run_crosscheck.ps1).
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef FAKE_PICO_TIME_H
#define FAKE_PICO_TIME_H

#include <stdint.h>

typedef uint64_t absolute_time_t;

uint64_t get_absolute_time(void);
uint32_t to_ms_since_boot(absolute_time_t t);
uint32_t time_us_32(void);

#endif /* FAKE_PICO_TIME_H */
