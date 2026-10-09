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
; Commands are 5-byte bursts into ptr..ptr+4: addr, len (little endian), op.
;   'R'  send len bytes from addr, then s1 s2 op
;   'W'  receive len bytes into addr, then send s1 s2 op
;   'J'  jsr addr, then send A X Y
;   'Q'  exit handshake, return to DOS
; s1/s2 is the block check over the command and its data: per byte
; s1 += b + d (carry c out), s2 += s1 + c, mod 256, cleared per command; d is
; the carry the loop leaves: bit 3 of b when sent, 0 when received (ATN in,
; ATNA and CLK out read 0).

XB_BURST = 64
s1      = tmp + 1
s2      = tmp + 2
SBURST  = 60                    ; sburst .. end of its loop
RBURST  = 55                    ; rburst .. end of its loop

; Pad to the next page if the next n bytes would cross one.
.macro PAGEFIT n
        .res (>(* + n - 1) <> >*) * (256 - <*)
.endmacro

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

close:  lda #CLK_OUT                    ; leaving
        sta IEC                         ; host acknowledges with DATA
xgo:    WAIT bcs, {lda IEC}, lsr
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

loop:   ldx #WD_IDLE_TICKS
        stx wdload
        lda #ptr
        ldx #$00
        ldy #5
        jsr block
        jsr bnext
        jsr rburst                      ; command, within the idle window
        ldx #WD_TICKS
        stx wdload
        lda tmp
        sta regs + 2
        cmp #'J'
        beq cmd_jsr
        cmp #'Q'
        beq cmd_quit
        cmp #'R'
        bne :+
        jsr sendblk
        beq reply
:       cmp #'W'
        bne loop
        jsr recvblk
reply:  lda s1
        sta regs
        lda s2
        sta regs + 1
respond:
        lda #<regs
        ldx #>regs
        ldy #3
        jsr block
        jsr sendblk
        jmp loop

cmd_jsr:
        jsr jump
        sta regs
        stx regs + 1
        sty regs + 2
        jmp respond
jump:   jmp (ptr)

cmd_quit:
        jsr close
        jmp exit

; ptr = X:A, len = Y, check cleared.
block:  sta ptr
        stx ptr + 1
        sty len
        lda #$00
        sta len + 1
        sta s1
        sta s2
        rts

; Next burst: n = min(XB_BURST - ptr mod XB_BURST, len) bytes at ptr. Patches
; both loops (send base and count, receive base - 1 and count), advances ptr
; and len, returns A = n with Z clear, or Z set when len is 0.
bnext:  lda len
        ora len + 1
        beq bret
        lda ptr
        ora #<-XB_BURST
        eor #$FF
        tax
        inx
        lda len + 1
        bne :+
        cpx len
        bcc :+
        ldx len
:       stx scp + 1
        stx rcp + 1
        lda ptr
        sta sld + 1
        sec
        sbc #$01
        sta rst + 1
        lda ptr + 1
        sta sld + 2
        sbc #$00
        sta rst + 2
        txa
        clc
        adc ptr
        sta ptr
        bcc :+
        inc ptr + 1
:       lda len
        sec
        sbc scp + 1
        sta len
        bcs :+
        dec len + 1
:       txa
bret:   rts

sendblk:
        jsr bnext
        beq :+
        jsr sburst
        bcs sendblk                     ; cpy left C set
:       rts

recvblk:
        jsr bnext
        beq :+
        jsr rburst
        bcs recvblk                     ; cpy left C set
:       rts
