; S1 transport: CLK/DATA only.
;
; Idle: host holds CLK, drive holds DATA. Ready is signalled with CLK alone,
; which a DOS-idle drive never asserts; host DATA, drive drops CLK, host takes
; CLK and drops DATA, drive takes DATA.
open:   lda #CLK_OUT
        sta IEC
        WAIT bcs, {lda IEC}, lsr
        lda #$00
        sta IEC
        WAIT bcc, {lda IEC}, lsr
        lda #DATA_OUT
        sta IEC
        rts

close:
tosend:
torecv: rts

; Receive A, MSB first.
getbyte:
        WDRESET
        ldx #$08
s1_gbit:
        lda #CLK_IN
        WAIT beq, {bit IEC}             ; host releases CLK: DATA holds the bit
        lda #$00
        sta IEC
        lda IEC
        and #DATA_IN
        sta tmp+1
        lsr
        rol tmp
        lda #CLK_OUT                    ; ack
        sta IEC
        WAIT bne, {lda IEC}, {and #DATA_IN}, {cmp tmp+1} ; host inverts DATA
        lda #$00
        sta IEC
        lda #CLK_IN
        WAIT bne, {bit IEC}             ; host reasserts CLK
        lda #DATA_OUT
        sta IEC
        dex
        bne s1_gbit
        lda tmp
        rts

; Send A, LSB first, on CLK.
sendbyte:
        sta tmp
        WDRESET
        ldx #$08
s1_sbit:
        lda #CLK_IN
        WAIT bne, {bit IEC}             ; host holds CLK
        lda #$00
        lsr tmp
        bcc :+
        lda #CLK_OUT
:       sta IEC                         ; DATA released: bit valid on CLK
        eor #CLK_OUT
        sta tmp+1
        WAIT bcs, {lda IEC}, lsr        ; host acks with DATA
        lda tmp+1                       ; flip CLK
        sta IEC
        WAIT bcc, {lda IEC}, lsr        ; host releases DATA
        lda #DATA_OUT
        sta IEC
        dex
        bne s1_sbit
        rts
