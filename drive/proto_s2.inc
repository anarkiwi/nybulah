; S2 transport: host strobes ATN, drive answers on CLK, payload on DATA, LSB
; first. Receive idle: ATN and CLK asserted. Send idle: CLK released.
; ACK_HELD is the ATNA value that keeps DATA released while ATN is asserted.
open:   lda #CLK_OUT                    ; ready: CLK alone (DOS idle never does this)
        sta IEC
        WAIT bmi, {bit IEC}             ; host asserts ATN
        lda #CLK_OUT | ACK_HELD
        sta IEC
        rts

close:  WAIT bpl, {bit IEC}             ; host releases ATN
        rts

tosend: lda #ACK_HELD
        sta IEC
        rts

torecv: WAIT bmi, {bit IEC}
        lda #CLK_OUT | ACK_HELD
        sta IEC
        rts

getbyte:
        WDRESET
        ldx #$04
s2_gbit:
        WAIT bpl, {bit IEC}             ; ATN released: DATA holds even bit
        lda #CLK_OUT
        sta IEC
        lda IEC
        lsr
        ror tmp
        lda #$00                        ; release CLK: ack
        sta IEC
        WAIT bmi, {bit IEC}             ; ATN asserted: DATA holds odd bit
        lda #ACK_HELD
        sta IEC
        lda IEC
        lsr
        ror tmp
        lda #CLK_OUT | ACK_HELD         ; assert CLK: ack
        sta IEC
        dex
        bne s2_gbit
        lda tmp
        rts

sendbyte:
        sta tmp
        WDRESET
        ldx #$04
s2_sbit:
        lda #$00
        lsr tmp
        rol
        asl
        ora #CLK_OUT | ACK_HELD
        WAIT bmi, {bit IEC}
        sta IEC
        lda #$00
        lsr tmp
        rol
        asl
        WAIT bpl, {bit IEC}
        sta IEC
        dex
        bne s2_sbit
        rts
