; Command loop and block bookkeeping shared by the burst transports
; (proto_xb.inc, proto_srq.inc).
;
; Commands are 5-byte bursts into ptr..ptr+4: addr, len (little endian), op.
;   'R'  send len bytes from addr, then s1 s2 op
;   'W'  receive len bytes into addr, then send s1 s2 op
;   'J'  jsr addr, then send A X Y
;   'Q'  exit handshake, return to DOS
; A truncated command burst leaves op 0, which is ignored.
;
; The transport supplies sburst/rburst (one burst of bnext's patched length,
; leaving C set), their patched operands scp, rcp (count), sld (send base)
; and rst (receive base - 1), and the macros SEND_ENTER/SEND_LEAVE run around
; every sent block.

XB_BURST = 64
s1      = tmp + 1
s2      = tmp + 2

; Pad to the next page if the next n bytes would cross one.
.macro PAGEFIT n
        .res (>(* + n - 1) <> >*) * (256 - <*)
.endmacro

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
        jmp reply
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
        SEND_ENTER
:       jsr bnext
        beq :+
        jsr sburst
        bcs :-                          ; bursts leave C set
:       SEND_LEAVE
        rts

recvblk:
        jsr bnext
        beq :+
        jsr rburst
        bcs recvblk
:       rts

close:  lda #CLK_OUT                    ; leaving
        sta IEC                         ; host acknowledges with DATA
xgo:    WAIT bcs, {lda IEC}, lsr
        rts
