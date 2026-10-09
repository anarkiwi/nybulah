; 1571 track 00 test, the DOS rule (lccutil1 stepper, mfmcntrl stout), called
; through the monitor's J command from the start of the capture buffer while
; the head is homed. SN_PAIRS pairs of VIA1 PA reads, each pair 9 cycles apart
; and one pair every 33 cycles, as the DOS loop takes them on track 00. A pair
; that disagrees fails the test; otherwise the last read decides (PA0 clear:
; on track 00). Returns A = SN_TRK00 when sensed, ORed with the stepper phase
; (VIA2 PB0-1). Position independent; clobbers Y and tmp (in the zero page
; range the host saves).

        .setcpu "6502"

VIA1PA_NH = $180F               ; VIA1 port A without the CA1 (ATN) handshake
VIA2PB   = $1C00
PA_TRK00 = $01
PB_STEP  = $03
SN_TRK00 = $04
SN_PAIRS = 99
tmp      = $80

        .segment "CODE"

sense:  ldy #SN_PAIRS
sp:     lda VIA1PA_NH
        sta tmp
        nop
        eor VIA1PA_NH
        lsr
        bcs soff
        bit $00
        nop
        nop
        nop
        nop
        dey
        bne sp
        .assert >* = >sp, error, "timed branch crosses a page"
        lda tmp
        and #PA_TRK00
        bne soff
        lda #SN_TRK00
        .byte $2C               ; bit abs: skip the lda #0
soff:   lda #0
        sta tmp
        lda VIA2PB
        and #PB_STEP
        ora tmp
        rts
