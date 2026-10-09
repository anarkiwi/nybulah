; Resident command loop for 1541/1571 speaking an xum1541 fast protocol.
;
; The transport comes from an include selected by -D PROTO=n:
;   1  proto_s1.inc  S1: CLK/DATA only, safe with other drives on the bus
;   2  proto_s2.inc  S2: ATN-strobed, only this drive may be listening
;   3  proto_x.inc   firmware-assisted protocol, built only when present
; A transport defines open, close, tosend, torecv, getbyte (returns A) and
; sendbyte (sends A). getbyte/sendbyte may use A, X and tmp/tmp+1 but must
; preserve Y; each starts with WDRESET and spins only through WAIT. close
; performs the exit handshake only: exit releases every line afterwards.
;
; VIA1 port B ($1800): PB0 DATA in, PB1 DATA out, PB2 CLK in, PB3 CLK out,
; PB4 ATNA, PB7 ATN in. ATNA must equal ATN in or the drive pulls DATA.
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

        .export start

IEC     = $1800
VIA1PA  = $1801
T1CL    = $1804
T1CH    = $1805
T1LL    = $1806
T1LH    = $1807
ACR     = $180B
IFR     = $180D
IER     = $180E

DATA_IN  = $01
DATA_OUT = $02
CLK_IN   = $04
CLK_OUT  = $08
ATNA     = $10

ACR_T1_FREERUN = $40
IRQ_T1   = $40

CLOCK_HZ = 1000000
WD_MS    = 1000
WD_CYCLES = CLOCK_HZ / 1000 * WD_MS
WD_TICKS = WD_CYCLES / $10000 + 1
WD_PERIOD = WD_CYCLES / WD_TICKS
WD_LATCH = WD_PERIOD - 2                ; free-run period is latch + 2
WD_IDLE_MS = 10000
WD_IDLE_TICKS = CLOCK_HZ / 1000 * WD_IDLE_MS / WD_PERIOD
.assert WD_IDLE_TICKS < 256, error, "idle ticks must fit a byte"

ptr     = $30
len     = $32
tmp     = $34
zpsize  = 6

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
        bit IFR
        bvc spin
        jsr wdtick
        bne spin
done:
.endmacro

        .segment "CODE"

start:  sei
        tsx
        stx savesp
        ldx #zpsize - 1
:       lda ptr,x
        sta zpsave,x
        dex
        bpl :-
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
        lda #WD_TICKS
        sta wdload
        WDRESET
        jsr open

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

exit:   ldx savesp
        txs
        lda #$00
        sta IEC
        ldx #zpsize - 1
:       lda zpsave,x
        sta ptr,x
        dex
        bpl :-
        lda viasave+2
        sta T1LL
        lda viasave+3
        sta T1LH
        lda viasave
        sta ACR
        bit T1CL                ; clear T1 flag
        lda viasave+1
        sta IER                 ; re-enables what was enabled (bit 7 reads 1)
        lda VIA1PA              ; clear CA1 (ATN) flag
        cli
        rts

; One T1 period elapsed inside WAIT: ack it, expire after wdload ticks.
; Returns with Z clear; preserves A, X and Y.
wdtick: bit T1CL
        dec wdcnt
        beq exit
        rts

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

.if PROTO = 1
        .include "proto_s1.inc"
.elseif PROTO = 2
        .include "proto_s2.inc"
.elseif PROTO = 3
        .include "proto_x.inc"
.else
        .error "PROTO must be 1, 2 or 3"
.endif

zpsave: .res zpsize
viasave: .res 4                 ; ACR, IER, T1 latch lo/hi
regs:   .res 3
savesp: .res 1
wdcnt:  .res 1
wdload: .res 1
