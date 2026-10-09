; Resident command loop for 1541/1571 speaking an xum1541 fast protocol.
;
; Assemble with -D PROTO=1 (S1: CLK/DATA only, safe with other drives on the
; bus) or -D PROTO=2 (S2: ATN-strobed, only this drive may be listening).
; VIA1 port B ($1800): PB0 DATA in, PB1 DATA out, PB2 CLK in, PB3 CLK out,
; PB4 ATNA, PB7 ATN in. ATNA must equal ATN in or the drive pulls DATA.
;
; Commands (host -> drive): opcode, then 16-bit little endian operands.
;   'R' addr len  send len bytes from addr
;   'W' addr len  receive len bytes into addr
;   'J' addr      jsr addr, then send A, X, Y
;   'Q'           release the bus and return to DOS

        .export start

IEC     = $1800
VIA1PA  = $1801

DATA_IN  = $01
DATA_OUT = $02
CLK_IN   = $04
CLK_OUT  = $08
ATNA     = $10

ptr     = $30
len     = $32
tmp     = $34
zpsize  = 6

        .segment "CODE"

start:  sei
        ldx #zpsize - 1
:       lda ptr,x
        sta zpsave,x
        dex
        bpl :-
        jsr open

loop:   jsr getbyte
        cmp #'R'
        beq cmd_read
        cmp #'W'
        beq cmd_write
        cmp #'J'
        beq cmd_jsr
        cmp #'Q'
        bne loop
        ldx #zpsize - 1
:       lda zpsave,x
        sta ptr,x
        dex
        bpl :-
        jsr close
        lda VIA1PA              ; clear CA1 (ATN) flag
        cli
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

; Idle: host holds CLK, drive holds DATA. Ready is signalled with CLK alone,
; which a DOS-idle drive never asserts; host DATA, drive drops CLK, host takes
; CLK and drops DATA, drive takes DATA.
open:   lda #CLK_OUT
        sta IEC
:       lda IEC
        lsr
        bcc :-
        lda #$00
        sta IEC
:       lda IEC
        lsr
        bcs :-
        lda #DATA_OUT
        sta IEC
        rts

close:  lda #$00
        sta IEC
        rts

tosend:
torecv: rts

; Receive A, MSB first.
getbyte:
        ldx #$08
@bit:   lda #CLK_IN
:       bit IEC                 ; host releases CLK: DATA holds the bit
        bne :-
        lda #$00
        sta IEC
        lda IEC
        and #DATA_IN
        sta tmp+1
        lsr
        rol tmp
        lda #CLK_OUT            ; ack
        sta IEC
:       lda IEC                 ; host inverts DATA
        and #DATA_IN
        cmp tmp+1
        beq :-
        lda #$00
        sta IEC
        lda #CLK_IN
:       bit IEC                 ; host reasserts CLK
        beq :-
        lda #DATA_OUT
        sta IEC
        dex
        bne @bit
        lda tmp
        rts

; Send A, LSB first, on CLK.
sendbyte:
        sta tmp
        ldx #$08
@bit:   lda #CLK_IN
:       bit IEC                 ; host holds CLK
        beq :-
        lda #$00
        lsr tmp
        bcc :+
        lda #CLK_OUT
:       sta IEC                 ; DATA released: bit valid on CLK
        eor #CLK_OUT
        sta tmp+1
:       lda IEC                 ; host acks with DATA
        lsr
        bcc :-
        lda tmp+1               ; flip CLK
        sta IEC
:       lda IEC                 ; host releases DATA
        lsr
        bcs :-
        lda #DATA_OUT
        sta IEC
        dex
        bne @bit
        rts

.elseif PROTO = 2

; S2 bit cell: host strobes ATN, drive answers on CLK, payload on DATA, LSB
; first. Receive idle: ATN and CLK asserted. Send idle: CLK released.
open:   lda #CLK_OUT            ; ready: CLK alone (DOS idle never does this)
        sta IEC
:       bit IEC                 ; wait for host ATN
        bpl :-
        lda #CLK_OUT | ATNA
        sta IEC
        rts

close:
:       bit IEC                 ; wait for host to release ATN
        bmi :-
        lda #$00
        sta IEC
        rts

tosend: lda #ATNA
        sta IEC
        rts

torecv:
:       bit IEC
        bpl :-
        lda #CLK_OUT | ATNA
        sta IEC
        rts

getbyte:
        ldx #$04
@bit:
:       bit IEC                 ; ATN released: DATA holds even bit
        bmi :-
        lda #CLK_OUT
        sta IEC
        lda IEC
        lsr
        ror tmp
        lda #$00                ; release CLK: ack
        sta IEC
:       bit IEC                 ; ATN asserted: DATA holds odd bit
        bpl :-
        lda #ATNA
        sta IEC
        lda IEC
        lsr
        ror tmp
        lda #CLK_OUT | ATNA     ; assert CLK: ack
        sta IEC
        dex
        bne @bit
        lda tmp
        rts

sendbyte:
        sta tmp
        ldx #$04
@bit:   lda #$00
        lsr tmp
        rol
        asl
        ora #CLK_OUT | ATNA
:       bit IEC
        bpl :-
        sta IEC
        lda #$00
        lsr tmp
        rol
        asl
:       bit IEC
        bmi :-
        sta IEC
        dex
        bne @bit
        rts

.endif

zpsave: .res zpsize
regs:   .res 3
