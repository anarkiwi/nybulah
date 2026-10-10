; Resident command loop for 1541/1571 speaking an xum1541 fast protocol.
;
; The transport comes from an include selected by -D PROTO=n:
;   1  proto_s1.inc  S1: CLK/DATA only, safe with other drives on the bus
;   2  proto_s2.inc  S2: ATN-strobed, only this drive may be listening
;   3  proto_x.inc   firmware-assisted protocol, built only when present
;   4  proto_xb.inc  its burst form (firmware v10); supplies its own command
;                    loop (5-byte command bursts, checked blocks, burst.inc)
;   5  proto_srq.inc 1571 CIA shift register on SRQ/DATA (firmware v11), the
;                    same command loop; built as monitor_s4
; A transport defines open, close, tosend, torecv, getbyte (returns A) and
; sendbyte (sends A). getbyte/sendbyte may use A, X and tmp/tmp+1 but must
; preserve Y; each starts with WDRESET and spins only through WAIT. close
; performs the exit handshake only: exit releases every line afterwards.
;
; VIA1 port B ($1800): PB0 DATA in, PB1 DATA out, PB2 CLK in, PB3 CLK out,
; PB4 ATNA, PB7 ATN in. ATNA must equal ATN in or the drive pulls DATA.
; -D M1581=1 builds for a 1581: the same bits are 8520 CIA port B ($4001),
; whose ATN acknowledge gate pulls DATA only for ATNA = 1 with ATN asserted
; (schematic 252380 sheet 3, U7), and PB5 turns the fast serial drivers.
;
; Commands (host -> drive): opcode, then 16-bit little endian operands.
;   'R' addr len  send len bytes from addr
;   'W' addr len  receive len bytes into addr
;   'J' addr      jsr addr, then send A, X, Y
;   'Q'           release the bus and return to DOS
;
; Watchdog: VIA1 T1 free-runs with IRQs masked, polled through IFR bit 6 only
; inside WAIT spin loops. WD_MS without a byte of progress inside a command,
; or WD_IDLE_MS waiting for the next command, makes the drive release the
; bus, restore zero page and VIA1, and return to DOS exactly like 'Q'. The
; 1571 in 2 MHz mode halves both.
; On a 1581 (2 MHz) the CIA does it: timer A runs at latch 1 (continuous)
; and timer B counts its underflows from $FFFF, a wrap every TB_PERIOD
; cycles; WAIT polls ICR bit 7, which the DOS's mask (dskint: $9A) and the
; $82 written on entry raise for timer B. Any enabled flag (ATN edge, shift
; register byte) also counts a tick. Entry saves CRA, CRB and both timer
; latches; every exit restores them and reads ICR.

        .export start

DATA_IN  = $01
DATA_OUT = $02
CLK_IN   = $04
CLK_OUT  = $08
ATNA     = $10

.ifdef M1581
IEC     = $4001
CIA_TALO = $4004
CIA_TAHI = $4005
CIA_TBLO = $4006
CIA_TBHI = $4007
CIA_ICR  = $400D
CIA_CRA  = $400E
CIA_CRB  = $400F
CRA_START = $01
CRX_LOAD = $10
CRB_TA   = $40                  ; CRB bits 6-5 = 10: count timer A underflows
ICR_SET  = $80
ICR_TB   = $02
FSDIR    = $20
ACK_HELD = $00                  ; ATNA keeping DATA released under ATN
CLOCK_HZ = 2000000
TB_PERIOD = 2 * $10000          ; timer A latch 1: an underflow every 2 cycles
WD_MS    = 1000
WD_TICKS = (CLOCK_HZ / 1000 * WD_MS + TB_PERIOD - 1) / TB_PERIOD
WD_IDLE_MS = 10000
WD_IDLE_TICKS = CLOCK_HZ / 1000 * WD_IDLE_MS / TB_PERIOD
.else
IEC     = $1800
VIA1PA  = $1801
T1CL    = $1804
T1CH    = $1805
T1LL    = $1806
T1LH    = $1807
ACR     = $180B
IFR     = $180D
IER     = $180E

ACR_T1_FREERUN = $40
IRQ_T1   = $40
LSNADR   = $77                  ; DOS's listen and talk addresses: a session's
TLKADR   = $78                  ; zero page covers them

ACK_HELD = ATNA
CLOCK_HZ = 1000000
WD_MS    = 1000
WD_CYCLES = CLOCK_HZ / 1000 * WD_MS
WD_TICKS = WD_CYCLES / $10000 + 1
WD_PERIOD = WD_CYCLES / WD_TICKS
WD_LATCH = WD_PERIOD - 2                ; free-run period is latch + 2
WD_IDLE_MS = 10000
WD_IDLE_TICKS = CLOCK_HZ / 1000 * WD_IDLE_MS / WD_PERIOD
.endif
.assert WD_IDLE_TICKS < 256, error, "idle ticks must fit a byte"

ptr     = $30
len     = $32
tmp     = $34
.if PROTO >= 4
zpsize  = 7
.else
zpsize  = 6
.endif

; Restart the no-progress budget (wdload ticks). Clobbers A.
.macro WDRESET
        lda wdload
        sta wdcnt
.endmacro

; Spin until `ready` (a branch mnemonic) is taken after the test sequence
; t1..t3, polling the watchdog on every miss. Preserves A, X and Y.
.macro WAIT ready, t1, t2, t3
        .local spin, done
spin:   t1
.ifnblank t2
        t2
.endif
.ifnblank t3
        t3
.endif
        ready done
.ifdef M1581
        bit CIA_ICR
        bpl spin
.else
        bit IFR
        bvc spin
.endif
        jsr wdtick
        bne spin
done:
.endmacro

        .segment "CODE"
.if PROTO >= 4
        .org $0500                      ; absolute, so loops can be page-fitted
.endif

start:  sei
.ifndef M1581
        lda LSNADR                      ; under savesp, back at every exit
        pha
        lda TLKADR
        pha
.endif
        tsx
        stx savesp
        ldx #zpsize - 1
:       lda ptr,x
        sta zpsave,x
        dex
        bpl :-
.ifdef M1581
        jsr ciasave
.else
        lda ACR
        sta viasave
        lda IER
        sta viasave+1
        lda T1LL
        sta viasave+2
        lda T1LH
        sta viasave+3
        lda #IRQ_T1
        sta IER
        lda viasave
        and #$3F
        ora #ACR_T1_FREERUN
        sta ACR
        lda #<WD_LATCH
        sta T1CL
        lda #>WD_LATCH
        sta T1CH
.endif
        lda #WD_TICKS
        sta wdload
        WDRESET
        jsr open
.if PROTO >= 4
        jmp loop
.else

loop:   ldx #WD_IDLE_TICKS
        stx wdload
        jsr getbyte
        ldx #WD_TICKS
        stx wdload
        cmp #'R'
        beq cmd_read
        cmp #'W'
        beq cmd_write
        cmp #'J'
        beq cmd_jsr
        cmp #'Q'
        bne loop
        jsr close
.endif

exit:   ldx savesp
        txs
.ifdef M1581
        lda #CRA_START                  ; serial input before the drivers turn in
        sta CIA_CRA
.endif
        lda #$00
        sta IEC
        ldx #zpsize - 1
:       lda zpsave,x
        sta ptr,x
        dex
        bpl :-
.ifdef M1581
        jsr ciarest
.else
        lda viasave+2
        sta T1LL
        lda viasave+3
        sta T1LH
        lda viasave
        sta ACR
        bit T1CL                ; clear T1 flag
.if PROTO = 5
        jsr ciarest
.endif
        lda viasave+1
        sta IER                 ; re-enables what was enabled (bit 7 reads 1)
        lda VIA1PA              ; clear CA1 (ATN) flag
        pla
        sta TLKADR
        pla
        sta LSNADR
.endif
        cli
        rts

; One T1 period elapsed inside WAIT: ack it, expire after wdload ticks.
; Returns with Z clear; preserves A, X and Y.
wdtick:
.ifndef M1581
        bit T1CL
.endif
        dec wdcnt
        beq exit
        rts

.if PROTO < 4

cmd_read:
        jsr getargs
        jsr tosend
        ldy #$00
:       lda (ptr),y
        jsr sendbyte
        iny
        bne :+
        inc ptr+1
:       jsr declen
        bne :--
        jsr torecv
        jmp loop

cmd_write:
        jsr getargs
        ldy #$00
:       jsr getbyte
        sta (ptr),y
        iny
        bne :+
        inc ptr+1
:       jsr declen
        bne :--
        jmp loop

cmd_jsr:
        jsr getbyte
        sta jaddr+1
        jsr getbyte
        sta jaddr+2
jaddr:  jsr $ffff
        sta regs
        stx regs+1
        sty regs+2
        jsr tosend
        lda regs
        jsr sendbyte
        lda regs+1
        jsr sendbyte
        lda regs+2
        jsr sendbyte
        jsr torecv
        jmp loop

getargs:
        jsr getbyte
        sta ptr
        jsr getbyte
        sta ptr+1
        jsr getbyte
        sta len
        jsr getbyte
        sta len+1
        rts

; Decrement 16-bit len, Z set when it reaches zero.
declen: lda len
        bne :+
        dec len+1
:       dec len
        lda len
        ora len+1
        rts
.endif

.ifdef M1581
; Save CRA, CRB and both timer latches (each force-loaded into its stopped
; counter and read back), then timer A at latch 1 in continuous mode with
; serial input, timer B counting its underflows from $FFFF, and timer B in
; the ICR mask.
ciasave:
        lda CIA_CRA
        sta ciasave_
        lda CIA_CRB
        sta ciasave_ + 1
        lda #CRX_LOAD
        sta CIA_CRA
        sta CIA_CRB
        ldx #3
:       lda CIA_TALO,x
        sta ciasave_ + 2,x
        dex
        bpl :-
        lda #1
        sta CIA_TALO
        stx CIA_TBLO                    ; X = $FF
        stx CIA_TBHI
        inx
        stx CIA_TAHI
        lda #CRA_START | CRX_LOAD
        sta CIA_CRA
        lda #CRA_START | CRX_LOAD | CRB_TA
        sta CIA_CRB
        lda #ICR_SET | ICR_TB
        sta CIA_ICR
        rts

; Stop both timers, put the latches back (the stopped counters load them),
; restore CRA and CRB, then read ICR so DOS sees no stale flag.
ciarest:
        lda #0
        sta CIA_CRA
        sta CIA_CRB
        tax
:       lda ciasave_ + 2,x              ; low byte first: the high write loads
        sta CIA_TALO,x
        inx
        cpx #4
        bne :-
        lda ciasave_
        sta CIA_CRA
        lda ciasave_ + 1
        sta CIA_CRB
        bit CIA_ICR
        rts
.endif

.if PROTO = 1
        .include "proto_s1.inc"
.elseif PROTO = 2
        .include "proto_s2.inc"
.elseif PROTO = 3
        .include "proto_x.inc"
.elseif PROTO = 4
        .include "proto_xb.inc"
.elseif PROTO = 5
        .include "proto_srq.inc"
.else
        .error "PROTO must be 1 to 5"
.endif

zpsave: .res zpsize
viasave: .res 4                 ; ACR, IER, T1 latch lo/hi
regs:   .res 3
.if PROTO >= 4
.assert regs & (XB_BURST - 1) <= XB_BURST - 3, error, "regs crosses a burst"
.endif
savesp: .res 1
wdcnt:  .res 1
wdload: .res 1
.ifdef M1581
ciasave_: .res 6                ; CRA, CRB, timer A and B latches
.elseif PROTO = 5
ciasave: .res 4                 ; CRA, timer A latch lo/hi, VIA1 port A
.endif
