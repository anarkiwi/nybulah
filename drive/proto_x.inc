; X transport: drive-timed, two bits per bus write on CLK+DATA, no ATN.
;
; Idle: both lines released. Per byte the host asserts DATA ("go"); the drive
; answers with a CLK assertion (SYNC) and then runs a fixed, branch-free
; schedule of port accesses timed from the SYNC write. The adapter detects the
; SYNC edge and samples (drive -> host) or drives (host -> drive) the pairs at
; offsets derived from these cycle counts; see docs/protocol.md.
;
; Pair encoding, (DATA, CLK) per bus slot:
;   send: P0 (b1,b3)  P1 (b5,b7)  P2 (b0,b2)  P3 (b4,b6)
;   recv: P0 (b0,b2)  P1 (b1,b3)  P2 (b4,b6)  P3 (b5,b7)
;
; Writes are never allowed to set ATNA: with ATN released it would pull DATA.
; Timed sections touch no page-crossing operands so every cycle is fixed.
;
; sendblk/recvblk move len bytes at (ptr) and return the block check in A
; (s1) and X (s2): per byte s1 += b with carry c, then s2 += s1 + c, mod 256.
; xread/xwrite load ptr/len from xparm first so 'J' can call them; the host
; finds them through the "NYBX" tag.

open:   lda #CLK_OUT                    ; alive: CLK alone
        sta IEC
        jsr xgo                         ; host takes DATA
        lda #$00
        sta IEC
        WAIT bcc, {lda IEC}, lsr        ; host drops DATA: idle
        rts

close:  lda #CLK_OUT                    ; leaving
        sta IEC                         ; host acknowledges with DATA
xgo:    WAIT bcs, {lda IEC}, lsr
tosend:
torecv: rts

sendbyte:
        sta tmp
        WDRESET
        jsr xgo
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        ldx tmp
        nop
        txa
        and #$AA
        sta IEC                         ; P0     t=13
        lsr
        lsr
        lsr
        lsr
        sta IEC                         ; P1     t=25
        txa
        and #$55
        asl
        sta IEC                         ; P2     t=35
        lsr
        lsr
        lsr
        lsr
        sta IEC                         ; P3     t=47
        nop
        nop
        lda #$00
        sta IEC                         ; REL    t=57
        rts

getbyte:
        WDRESET
        jsr xgo                         ; go: host has a byte
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        lda #$00
        sta IEC                         ; REL    t=6
        nop
        lda IEC                         ; P0     t=12
        sta tmp
        nop
        lda IEC                         ; P1     t=21
        asl
        ora tmp
        and #$0F
        tax
        lda IEC                         ; P2     t=34
        sta tmp
        nop
        lda IEC                         ; P3     t=43
        asl
        ora tmp
        asl
        asl
        asl
        asl
        stx tmp
        ora tmp
        rts

; y = ptr low with ptr page aligned, len = -len, check cleared; Z if empty.
xblkinit:
        ldy ptr
        lda #$00
        sta ptr
        sta tmp+1
        sta xsum2
        sec
        sbc len
        sta len
        lda #$00
        sbc len+1
        sta len+1
        ora len
        rts

sendblk:
        jsr xblkinit
        beq xblkdone
xsbyte: WDRESET
        WAIT bcs, {lda IEC}, lsr
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        lda (ptr),y
        tax
        and #$AA
        sta IEC                         ; P0     t=13
        lsr
        lsr
        lsr
        lsr
        sta IEC                         ; P1     t=25
        txa
        and #$55
        asl
        sta IEC                         ; P2     t=35
        lsr
        lsr
        lsr
        lsr
        sta IEC                         ; P3     t=47
        txa
        clc
        adc tmp+1
        sta tmp+1
        adc xsum2
        sta xsum2
        lda #$00
        sta IEC                         ; REL    t=69
        iny
        bne xsnext
        inc ptr+1
xsnext: inc len
        bne xsbyte
        inc len+1
        bne xsbyte
xblkdone:
        lda tmp+1
        ldx xsum2
        rts

recvblk:
        jsr xblkinit
        beq xblkdone
xrbyte: WDRESET
        WAIT bcs, {lda IEC}, lsr
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        lda #$00
        sta IEC                         ; REL    t=6
        nop
        lda IEC                         ; P0     t=12
        sta tmp
        nop
        lda IEC                         ; P1     t=21
        asl
        ora tmp
        and #$0F
        tax
        lda IEC                         ; P2     t=34
        sta tmp
        nop
        lda IEC                         ; P3     t=43
        asl
        ora tmp
        asl
        asl
        asl
        asl
        stx tmp
        ora tmp
        sta (ptr),y
        clc
        adc tmp+1
        sta tmp+1
        adc xsum2
        sta xsum2
        iny
        bne xrnext
        inc ptr+1
xrnext: inc len
        bne xrbyte
        inc len+1
        bne xrbyte
        jmp xblkdone

xparms: ldx #$03
:       lda xparm,x
        sta ptr,x
        dex
        bpl :-
        rts

        .byte "NYBX"
xparm:  .res 4                          ; addr, len (little endian)
xread:  jsr xparms
        jmp sendblk
xwrite: jsr xparms
        jmp recvblk

xsum2:  .res 1
