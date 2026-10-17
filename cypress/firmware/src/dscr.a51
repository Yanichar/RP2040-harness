; USB descriptors for the FX2LP 24-line harness firmware (harness_fx2.c).
;
; Layout follows fx2lib's fw/dscr.a51 so that lib/setupdat.c finds the symbols
; it expects (_dev_dscr, _dev_qual_dscr, _highspd_dscr, _fullspd_dscr,
; _dev_strings, _dev_strings_end).  The objects live in DSCR_AREA, which the
; linker places absolutely at 0x3E00 (see build.ps1) because the EZ-USB core
; reads the descriptor set from that fixed address after renumeration.
;
; Identification:
;   idVendor  = 0x04B4 (Cypress)
;   idProduct = 0x8614 (the "application" PID our cyfx2lp.inf binds to WinUSB)
;   bDeviceClass = 0xFF (class is declared per interface)
;
; The single interface is vendor-specific and has NO endpoints: everything the
; harness does is a control transfer on EP0 (see ../../README.md, "Chunked EP0
; transport").  bInterfaceSubClass/bInterfaceProtocol mirror docs/protocol.md
; section 1 (0x48 'H' / 0x01) so a host can tell a harness interface apart.

.module HARNESS_FX2_DSCR

DSCR_DEVICE_TYPE=1
DSCR_CONFIG_TYPE=2
DSCR_STRING_TYPE=3
DSCR_INTERFACE_TYPE=4
DSCR_ENDPOINT_TYPE=5
DSCR_DEVQUAL_TYPE=6

DSCR_INTERFACE_LEN=9

	.globl	_dev_dscr, _dev_qual_dscr, _highspd_dscr, _fullspd_dscr, _dev_strings, _dev_strings_end

	.area	DSCR_AREA	(CODE)

_dev_dscr:
	.db	dev_dscr_end-_dev_dscr    ; bLength = 18
	.db	DSCR_DEVICE_TYPE          ; bDescriptorType
	.dw	0x0002                    ; bcdUSB 2.00 (little endian on the wire)
	.db	0xff                      ; bDeviceClass      (vendor specific)
	.db	0xff                      ; bDeviceSubClass
	.db	0xff                      ; bDeviceProtocol
	.db	64                        ; bMaxPacketSize0
	.dw	0xB404                    ; idVendor  = 0x04B4
	.dw	0x1486                    ; idProduct = 0x8614
	.dw	0x0100                    ; bcdDevice
	.db	1                         ; iManufacturer
	.db	2                         ; iProduct
	.db	0                         ; iSerialNumber (none: see README, board_id)
	.db	1                         ; bNumConfigurations
dev_dscr_end:

_dev_qual_dscr:
	.db	dev_qualdscr_end-_dev_qual_dscr
	.db	DSCR_DEVQUAL_TYPE
	.dw	0x0002
	.db	0xff
	.db	0xff
	.db	0xff
	.db	64
	.db	1
	.db	0
dev_qualdscr_end:

_highspd_dscr:
	.db	highspd_dscr_end-_highspd_dscr
	.db	DSCR_CONFIG_TYPE
	.db	(highspd_dscr_realend-_highspd_dscr) % 256
	.db	(highspd_dscr_realend-_highspd_dscr) / 256
	.db	1                         ; bNumInterfaces
	.db	1                         ; bConfigurationValue
	.db	0                         ; iConfiguration
	.db	0x80                      ; bmAttributes: bus powered
	.db	0x32                      ; bMaxPower: 100 mA
highspd_dscr_end:

	.db	DSCR_INTERFACE_LEN
	.db	DSCR_INTERFACE_TYPE
	.db	0                         ; bInterfaceNumber
	.db	0                         ; bAlternateSetting
	.db	0                         ; bNumEndpoints: EP0 only
	.db	0xff                      ; bInterfaceClass: vendor specific
	.db	0x48                      ; bInterfaceSubClass: 'H' (protocol.md 1)
	.db	0x01                      ; bInterfaceProtocol
	.db	0                         ; iInterface
highspd_dscr_realend:

.even
_fullspd_dscr:
	.db	fullspd_dscr_end-_fullspd_dscr
	.db	DSCR_CONFIG_TYPE
	.db	(fullspd_dscr_realend-_fullspd_dscr) % 256
	.db	(fullspd_dscr_realend-_fullspd_dscr) / 256
	.db	1
	.db	1
	.db	0
	.db	0x80
	.db	0x32
fullspd_dscr_end:

	.db	DSCR_INTERFACE_LEN
	.db	DSCR_INTERFACE_TYPE
	.db	0
	.db	0
	.db	0                         ; bNumEndpoints: EP0 only
	.db	0xff
	.db	0x48
	.db	0x01
	.db	0
fullspd_dscr_realend:

.even
_dev_strings:
; string 0: supported language ids (0x0409 = US English)
_string0:
	.db	string0end-_string0
	.db	DSCR_STRING_TYPE
	.db	0x09, 0x04
string0end:
; string 1: manufacturer, UTF-16LE "Cypress"
_string1:
	.db	string1end-_string1
	.db	DSCR_STRING_TYPE
	.db	0x43,0x00,0x79,0x00,0x70,0x00,0x72,0x00,0x65,0x00,0x73,0x00,0x73,0x00
string1end:
; string 2: product, UTF-16LE "FX2LP Harness"
_string2:
	.db	string2end-_string2
	.db	DSCR_STRING_TYPE
	.db	0x46,0x00,0x58,0x00,0x32,0x00,0x4C,0x00,0x50,0x00,0x20,0x00,0x48,0x00,0x61,0x00,0x72,0x00,0x6E,0x00,0x65,0x00,0x73,0x00,0x73,0x00
string2end:

_dev_strings_end:
	.dw	0x0000
