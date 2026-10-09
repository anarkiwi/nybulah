; 6526 shift register timing probe for a 1571 (or, with -D M1581=1, the 1581's
; 8520, whose drivers turn with port B bit 5), called through the J command
; of a monitor that leaves SRQ alone (s1, s3): the adapter of an s4 session
; would take the probe's SRQ pulses for its reply. It saves CRA, the timer A
; latch and the driver port, runs timer A at latch 1 (an underflow every 2
; cycles) with the shift register in output mode, and restores all three.
; Shifting $FF keeps DATA released; only SRQ pulses.
;
; For each read offset k = K0 .. K0 + NK - 1 and each write phase p (0, 1:
; the SDR write one cycle later against timer A), the probe writes SDR in
; output mode and reads ICR exactly k cycles after the write, storing
; ICR & SP at res + 2 * (k - K0) + p. It returns the first k whose read saw
; the flag for phase 0 in A and phase 1 in X ($FF: never within the range).
; The host reads res back (tools/xprobe.py --cia).
;
; Timed path (t = 0 is the stx write cycle): var is nop; nop (4 cycles) or,
; patched to $24, bit $EA (3); jmp 3; n nops of the slide 2n; the lda reads
; ICR in its 4th cycle: k = 10 + 2n + (var is two nops).

        .setcpu "6502"

CIA_TALO = $4004
CIA_TAHI = $4005
CIA_SDR  = $400C
CIA_ICR  = $400D
CIA_CRA  = $400E
.ifdef M1581
FSDIR_PORT = $4001              ; 8520 port B, PB5 (1581)
FSDIR    = $20
.else
FSDIR_PORT = $180F              ; VIA1 port A without handshake, PA1 (1571)
FSDIR    = $02
.endif
CRA_START = $01
CRA_LOAD = $10
CRA_SPOUT = $40
ICR_SP   = $08
OP_NOP   = $EA
OP_BITZP = $24
K0       = 26
NK       = 18
NOPS     = (K0 + NK - 10) / 2
SETTLE   = 20                   ; x 5 cycles: past any byte's end

        .segment "CODE"

probe:  lda CIA_CRA
        sta save
        lda #CRA_LOAD
        sta CIA_CRA                     ; stopped, latch in the counter
        lda CIA_TALO
        sta save + 1
        lda CIA_TAHI
        sta save + 2
        lda FSDIR_PORT
        sta save + 3
        lda #1
        sta CIA_TALO
        lda #0
        sta CIA_TAHI
        lda save + 3
        ora #FSDIR
        sta FSDIR_PORT
        lda #CRA_START | CRA_SPOUT
        sta CIA_CRA
        lda #2 * NK - 1
        sta idx
mloop:  lda idx
        and #1
        sta phase
        lda idx
        lsr
        clc
        adc #K0 - 10            ; A = 2n + odd part of k - 10
        lsr                     ; C set: k - 10 odd, two nops
        tay
        lda #OP_NOP
        bcs :+
        lda #OP_BITZP
:       sta var
        tya
        eor #$FF
        sec
        adc #<(slide + NOPS)    ; slide entry: NOPS - n nops skipped
        sta jmpv + 1
        lda #>(slide + NOPS)
        sbc #0
        sta jmpv + 2
        ldx #$FF
        bit CIA_ICR
        lda CIA_TALO
        eor phase
        lsr
        bcs :+
:       stx CIA_SDR             ; t = 0
var:    nop
        nop
jmpv:   jmp slide
slide:  .res NOPS, OP_NOP
        lda CIA_ICR             ; t = k
        and #ICR_SP
        ldy idx
        sta res,y
        ldy #SETTLE
:       dey
        bne :-
        dec idx
        bpl mloop
        lda #0
        sta CIA_CRA
        lda save + 3
        sta FSDIR_PORT
        lda save + 1
        sta CIA_TALO
        lda save + 2
        sta CIA_TAHI
        lda save
        sta CIA_CRA
        bit CIA_ICR
        ldx #1
first:  txa
        tay
:       lda res,y
        bne :+
        iny
        iny
        cpy #2 * NK
        bcc :-
        lda #$FF
        bne :++
:       tya
        lsr
        clc
        adc #K0
:       pha
        dex
        bpl first
        pla
        sta phase
        pla
        tax
        lda phase
        rts

save:   .res 4                          ; CRA, timer A latch, VIA1 port A
phase:  .res 1
idx:    .res 1
res:    .res 2 * NK
