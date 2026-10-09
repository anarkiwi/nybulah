; X burst transport (xum1541 firmware v10): CLK+DATA only, no ATN.
;
; Data moves in bursts of at most XB_BURST bytes that never cross an
; XB_BURST-aligned address. Per burst the host asserts DATA ("go"), the drive
; answers with a CLK assertion (SYNC) and runs a branch-free loop with a fixed
; cycle count per byte; the adapter follows that schedule open-loop from the
; SYNC edge to the end of the burst, so synchronisation and the watchdog
; restart are paid once per burst. See docs/protocol.md.
;
; send (drive -> host), cycles after the SYNC write, byte i adds 67 i:
;   P0 14 (b1,b3)  P1 26 (b5,b7)  P2 50 (b0,b2)  P3 62 (b4,b6)  REL 74 (last)
; recv (host -> drive), CLK released at 6, byte i adds 52 i:
;   reads 12 20 30 38 of pairs (b5,b7) (b4,b6) (b1,b3) (b0,b2)
; Pairs are (DATA, CLK). recv rebuilds b = r0<<5 ^ r1<<4 ^ r2<<1 ^ r3 from raw
; port reads; the device-number inputs PB5/PB6 add the constant K ^ K<<1
; (K = PB & $60), cancelled by the eor #kk that open patches in.
;
; Commands and block bookkeeping: burst.inc. s1/s2 is the block check over
; the command and its data: per byte s1 += b + d (carry c out),
; s2 += s1 + c, mod 256, cleared per command; d is the carry the loop leaves:
; bit 3 of b when sent, 0 when received (ATN in, ATNA and CLK out read 0).

SBURST  = 60                    ; sburst .. end of its loop
RBURST  = 55                    ; rburst .. end of its loop

.macro SEND_ENTER
.endmacro
.macro SEND_LEAVE
.endmacro

        .include "burst.inc"

        .byte "NYXB"

        PAGEFIT SBURST
sburst: WDRESET
        jsr xgo
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        ldy #$00
sld:    lda $FFFF,y
        tax
        and #$AA
        sta IEC                         ; P0     t=14
        lsr
        lsr
        lsr
        lsr
        sta IEC                         ; P1     t=26
        txa
        adc s1
        sta s1
        adc s2
        sta s2
        txa
        and #$55
        asl
        sta IEC                         ; P2     t=50
        lsr
        lsr
        lsr
        lsr
        sta IEC                         ; P3     t=62
        iny
scp:    cpy #$00
        bne sld
.assert * - sburst = SBURST, error, "SBURST"
        lda #$00
        sta IEC                         ; REL    t=74
        rts

open:   lda IEC
        and #$60
        sta tmp
        asl
        eor tmp
        sta rkk + 1
        lda #CLK_OUT                    ; alive: CLK alone
        sta IEC
        jsr xgo                         ; host takes DATA
        lda #$00
        sta IEC
        WAIT bcc, {lda IEC}, lsr        ; host drops DATA: idle
        rts

        PAGEFIT RBURST
rburst: WDRESET
        jsr xgo
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        lda #$00
        sta IEC                         ; REL    t=6
        tay
rl:     lda IEC                         ; r0     t=12
        asl
        iny
        eor IEC                         ; r1     t=20
        asl
        asl
        asl
        eor IEC                         ; r2     t=30
        asl
rkk:    eor #$00
        eor IEC                         ; r3     t=38
rst:    sta $FFFF,y
        adc s1
        sta s1
        adc s2
        sta s2
rcp:    cpy #$00
        bne rl
.assert * - rburst = RBURST, error, "RBURST"
        rts
