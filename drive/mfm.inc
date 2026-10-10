; 1581 hardware for drive/mfm.s and drive/mfmstream.s (loaded at $0300 and
; called through a monitor_*_1581 J command, which has set up the CIA timers).
;
; 8520 CIA $4000 (1581 service manual memory map; DOS iodef.src):
;   PA0 side select out (0: header side 0, the drive's head 1), PA1 /RDY in,
;   PA2 /MOTOR out (0: on), PA5 power LED, PA6 activity LED (1: lit),
;   PA7 /DISK CHANGE in; PB0-PB3 DATA/CLK in/out, PB4 ATN ack, PB5 fast serial
;   drivers out, PB6 /WPRT in, PB7 ATN in.
; Timer A free-runs at latch 1; timer B counts its underflows from $FFFF: it
; drops by one every microsecond and sets ICR bit 1 when it wraps.
; WD177x $6000 (status/command, track, sector, data). Every instruction that
; touches it sits at an address whose low two bits are not 00, as the DOS
; places its own (mfmmacro.src WDTEST).

        .setcpu "6502"

CIA_PA   = $4000
CIA_PB   = $4001
CIA_TBLO = $4006
CIA_TBHI = $4007
CIA_SDR  = $400C
CIA_ICR  = $400D
CIA_CRA  = $400E
WDCMD    = $6000
WDSTAT   = $6000
WDTRK    = $6001
WDSEC    = $6002
WDDAT    = $6003

PA_SIDE  = $01
PA_MOTOR = $04
PA_LED   = $40
PB_DATA  = $02
PB_CLK   = $08
PB_FSDIR = $20
PB_ATN   = $80
ICR_TB   = $02
ICR_SP   = $08
CRA_START = $01
CRA_SPOUT = $40

; WD177x status (datasheet status register summary).
ST_BUSY  = $01
ST_DRQ   = $02                  ; type II/III
ST_IP    = $02                  ; type I
ST_T0    = $04                  ; type I
ST_LD    = $04                  ; type II/III
ST_CRC   = $08
ST_RNF   = $10
ST_RT    = $20
ST_WP    = $40

; Programmed I/O delays (datasheet status register table, MFM column: the
; 1581 grounds DDEN, schematic sheet 2) in CPU cycles: Y1's 16 MHz through
; U10 clocks the WD at 8 MHz, the datasheet's CLK, and the CPU at 2 MHz.
CPU_MHZ  = 2
BUSY_VALID = 24 * CPU_MHZ       ; command write -> status bit 0 (busy)
STATUS_VALID = 32 * CPU_MHZ     ; command write -> status bits 1-7
FORCE_GAP = 16 * CPU_MHZ        ; force interrupt -> next command (type IV)
.assert BUSY_VALID <= STATUS_VALID && FORCE_GAP <= STATUS_VALID, error, "delays"

; Commands as the DOS issues them (mrout.src reset_ctl), h = 1 (no spin-up
; wait; MO is not wired, CIA PA2 runs the motor), with the step rate r1r0 =
; 01: 12 ms on both the WD1770 and the WD1772.
WD_RATE  = $01
WD_RESTORE = $08 | WD_RATE
WD_SEEK  = $18 | WD_RATE
WD_READSEC = $88
WD_WRITESEC = $A8               ; | precompensation bit, | deleted mark
WD_READADDR = $C8
WD_READTRK = $E8
WD_WRITETRK = $F8               ; | precompensation bit
WD_FORCE = $D0
WD_NOPRECOMP = $02              ; set from PRECOMP_CYL on (msub.src precmp)
WD_DELETED = $01
PRECOMP_CYL = 43 + 1            ; precmptrk = 43 after subtracting pstartsec
MAX_CYL  = 80                   ; DOS pmaxtrk 79; Wheels writes cylinder 80
STEP_US  = 12000                ; r1r0 = 01

; Zero page the monitor saves and leaves free during J.
ptr      = $30
len      = $32
tmp      = $34

; Pad with a nop when the next instruction would start at an address whose
; low two bits are 00 (untimed code).
.macro WDTEST
        .if (* & 3) = 0
        nop
        .endif
.endmacro

; After an unconditional jump: pad with an unreachable byte so the WD access
; at the next label keeps the rule without changing any timing.
.macro WDALIGN
        .if (* & 3) = 0
        .byte $EA
        .endif
.endmacro

; After an unconditional jump: pad with unreachable bytes to an address
; that is k modulo 4, so a timed block's WD accesses keep the rule.
.macro ALIGN4 k
        .res (k - *) & 3, $EA
.endmacro

; Assert the rule instead (timed code).
.macro WDOK
        .assert (* & 3) <> 0, error, "WD access at an address ending in 00"
.endmacro

; A branch whose cycle count is part of a timing table: no page crossing.
.macro BR op, target
        op target
        .assert >* = >(target), error, "timed branch crosses a page"
.endmacro

; Write command A, then read the status into A once every bit of it is valid:
; the read's last cycle comes 5 n + 5 >= STATUS_VALID cycles after the
; write's (ldx, n loop passes of 5 less the last branch's 1, lda), more when
; a branch crosses a page or WDTEST pads. Every command and force interrupt
; goes through it, so no status read precedes the delay and no command
; follows a force interrupt sooner than FORCE_GAP.
WD_LOOPS = (STATUS_VALID - 1) / 5
.assert 5 * WD_LOOPS + 5 >= STATUS_VALID, error, "WD_LOOPS"
.macro WDISSUE
        WDTEST
        sta WDCMD
        ldx #WD_LOOPS
:       dex
        bne :-
        WDTEST
        lda WDSTAT
.endmacro

; After a force interrupt (status in A, read through WDISSUE): poll until busy
; reads clear, as the DOS does after $D0 (msub.src wdabort: delay, then
; wdunbusy). A $D0 written while idle can leave busy set, with the type I
; bits cleared, past the datasheet's delays; type I status (T0, IP) is valid
; only once busy reads clear. Gives up after a full timer B wrap (two ICR
; flags), busy still set in A. Y is clobbered.
.macro WDIDLE
        .local poll, read, done
        ldy #2
poll:   lsr
        bcc done
        bit CIA_ICR
        bpl read
        dey
        beq done
read:   WDTEST
        lda WDSTAT
        bcs poll
done:   WDTEST
        lda WDSTAT
.endmacro
