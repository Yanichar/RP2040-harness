/*
 * Host-side driver for the firmware protocol layer.
 *
 * Reads hex-encoded request frames from stdin, feeds each one through the REAL
 * firmware dispatcher (firmware/src/protocol.c), and prints the resulting reply
 * frame as hex. Drives firmware/test/crosscheck.py, which compares those bytes
 * against the Python host implementation.
 *
 * Protocol on stdin/stdout (one command per line):
 *   <hex bytes>    -> "REPLY <hex bytes>"
 *   #crc <hex>     -> "CRC <4 hex digits>"  (CRC-16/CCITT-FALSE over <hex>)
 *   #rxreset       -> "OK"                  (reset the framing accumulator)
 *   #rx <hex>      -> "RX F<len>|C<err>|..." (push bytes, report what framing sees)
 *   #safe          -> "OK"                  (reset the stubbed pin model)
 *   #quit          -> exit
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crc16.h"
#include "gpio_ctrl.h"
#include "protocol.h"

#define LINE_MAX 16384

static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int parse_hex(const char *s, uint8_t *out, size_t cap) {
    size_t n = 0;
    while (s[0] != '\0' && s[1] != '\0') {
        const int hi = hexval((unsigned char)s[0]);
        const int lo = hexval((unsigned char)s[1]);
        if (hi < 0 || lo < 0) {
            return -1;
        }
        if (n >= cap) {
            return -1;
        }
        out[n++] = (uint8_t)((hi << 4) | lo);
        s += 2;
    }
    if (s[0] != '\0') {
        return -1; /* odd number of digits */
    }
    return (int)n;
}

int main(void) {
    static char line[LINE_MAX];
    static uint8_t req[HARNESS_MAX_FRAME];
    static uint8_t scratch[HARNESS_MAX_FRAME];
    static uint8_t rep[HARNESS_MAX_FRAME];

    setvbuf(stdout, NULL, _IONBF, 0);

    harness_protocol_init();
    harness_gpio_safe_state();

    static harness_rx_t g_rx;
    harness_rx_init(&g_rx);

    while (fgets(line, sizeof(line), stdin) != NULL) {
        char *p = line;
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        size_t len = strlen(p);
        while (len > 0 && (p[len - 1] == '\n' || p[len - 1] == '\r' || p[len - 1] == ' ')) {
            p[--len] = '\0';
        }
        if (len == 0) {
            continue;
        }

        if (strcmp(p, "#quit") == 0) {
            break;
        }
        if (strcmp(p, "#safe") == 0) {
            harness_gpio_safe_state();
            printf("OK\n");
            continue;
        }
        if (strcmp(p, "#rxreset") == 0) {
            harness_rx_init(&g_rx);
            printf("OK\n");
            continue;
        }
        if (strncmp(p, "#rx", 3) == 0 && (p[3] == '\0' || p[3] == ' ')) {
            const char *arg = p + 3;
            while (*arg == ' ') {
                arg++;
            }
            const int n = parse_hex(arg, scratch, sizeof(scratch));
            if (n < 0) {
                printf("ERR badhex\n");
                continue;
            }
            harness_rx_push(&g_rx, scratch, (uint16_t)n);
            printf("RX");
            for (;;) {
                uint16_t frame_len = 0;
                const harness_rx_result_t res = harness_rx_poll(&g_rx, &frame_len);
                if (res == HARNESS_RX_NONE) {
                    break;
                }
                if (res == HARNESS_RX_CORRUPT) {
                    printf(" C%d", (int)g_rx.last_error);
                    continue;
                }
                printf(" F%d", (int)frame_len);
                harness_rx_consume(&g_rx, frame_len);
            }
            printf("\n");
            continue;
        }
        if (strncmp(p, "#crc", 4) == 0 && (p[4] == '\0' || p[4] == ' ')) {
            const char *arg = p + 4;
            while (*arg == ' ') {
                arg++;
            }
            const int n = parse_hex(arg, scratch, sizeof(scratch));
            if (n < 0) {
                printf("ERR badhex\n");
                continue;
            }
            printf("CRC %04X\n", (unsigned)harness_crc16_ccitt_false(scratch, (size_t)n));
            continue;
        }

        const int n = parse_hex(p, req, sizeof(req));
        if (n < 0) {
            printf("ERR badhex\n");
            continue;
        }

        uint16_t rep_len = 0;
        harness_protocol_handle(req, (uint16_t)n, rep, sizeof(rep), &rep_len);

        printf("REPLY ");
        for (uint16_t i = 0; i < rep_len; i++) {
            printf("%02X", rep[i]);
        }
        printf("\n");
    }

    return 0;
}
