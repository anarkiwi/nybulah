; SRQ fast-serial transport (xum1541 firmware v11), 1571 only: the 6526 CIA
; shift register clocks bytes over SRQ (CNT) and DATA (SP). No ATN; a 1541
; has no SRQ input and ignores DATA without ATN.
;
; VIA1 PA1 turns the 1571's bus drivers between the CIA and the bus: 1 drives
; SRQ/DATA from CNT/SP, 0 feeds the bus to them. Timer A free-runs with latch
; SR_LATCH, an underflow every 2 cycles; in output mode a written SDR byte
; starts at the next underflow and goes out MSB first, one bit per two
; underflows: CNT falls with the new bit on SP and rises an underflow later.
; ICR bit 3 is set after the 8th rise. In input mode a rising CNT shifts SP
; in and the 8th fills SDR and sets the same flag.
;
; send (drive -> host): the host asserts CLK ("go"); the drive writes a byte
; to SDR every SR_PERIOD cycles, open loop. A write before the shifter has
; finished would follow the previous byte without a gap, so SR_PERIOD exceeds
; the latest ICR flag after a write: 39 cycles on a 1571 (a poll at 32 always
; misses, one at 39 always hits), 30 + the start delay in the model. With an
; even period every byte starts on the same timer phase, so a byte follows
; the previous one's last rise by SR_PERIOD - 30 cycles whatever the 6526's
; start delay; the host times each byte from its first SRQ fall.
; recv (host -> drive): the host asserts DATA ("go"), the drive pulses CLK
; (SYNC t=0, released at t=6) with flags cleared; the host then clocks bytes
; at a fixed period. The drive polls ICR every 11 cycles, every 39 once
; behind, reads SDR 7 cycles after the poll that sees the flag, and leaves
; for DOS after 255 empty polls (over 30 byte periods).
;
; Commands and block bookkeeping: burst.inc. s1/s2 is the block check over
; the command and its data: per byte s1 += b (carry c out), s2 += s1 + c,
; mod 256, cleared per command (fastx.xsum).

CIA_TALO = $4004
CIA_TAHI = $4005
CIA_SDR  = $400C
CIA_ICR  = $400D
CIA_CRA  = $400E
VIA1_ORA = $180F                ; port A, no handshake
PA_FSDIR = $02
PA_2MHZ  = $20
CRA_START = $01
CRA_LOAD = $10
CRA_SPOUT = $40
ICR_SP   = $08
SR_LATCH = 1                    ; phi2/4, the 6526's fastest shift clock
SR_PERIOD = 40                  ; send: cycles from one SDR write to the next
SBURST   = 51                   ; sburst .. end of its loop
RBURST   = 58                   ; rburst .. end of its loop

; Drivers out first, then the CIA: CNT and SP reach the bus released.
.macro SEND_ENTER
        lda VIA1_ORA
        ora #PA_FSDIR
        sta VIA1_ORA
        lda #CRA_START | CRA_SPOUT
        sta CIA_CRA
.endmacro

.macro SEND_LEAVE
        jsr spin
.endmacro

        .include "burst.inc"

        .byte "NYSR"

        PAGEFIT SBURST
sburst: WDRESET
        WAIT bne, {lda IEC}, {and #CLK_IN}      ; go
        ldy #$00
        clc
sld:    ldx $FFFF,y
        stx CIA_SDR                     ; t=0
        bit CIA_ICR                     ; t=4: clears the previous byte's flag
        txa
        adc s1
        sta s1
        adc s2
        sta s2
        pha
        pla
        iny
scp:    cpy #$00
        bne sld                         ; next write at t=SR_PERIOD
.assert * - sburst = SBURST, error, "SBURST"
        lda #ICR_SP
:       bit CIA_ICR
        beq :-                          ; last byte out
        rts

        PAGEFIT RBURST
rburst: WDRESET
        jsr xgo
        bit CIA_ICR                     ; no stale flag
        lda #CLK_OUT
        sta IEC                         ; SYNC   t=0
        lda #$00
        sta IEC                         ; REL    t=6
        tay
        clc
rl:     lda #ICR_SP
        ldx #$FF
:       bit CIA_ICR
        bne :+
        dex
        bne :-
        jmp exit                        ; host gone mid-burst
:       lda CIA_SDR
        iny
rst:    sta $FFFF,y
        adc s1
        sta s1
        adc s2
        sta s2
rcp:    cpy #$00
        bne rl
.assert * - rburst = RBURST, error, "RBURST"
        rts

; Save CRA, the timer A latch (force-loaded into the stopped counter) and
; port A, then run timer A at SR_LATCH in input mode.
open:   lda CIA_CRA
        sta ciasave
        lda #CRA_LOAD
        sta CIA_CRA
        lda CIA_TALO
        sta ciasave + 1
        lda CIA_TAHI
        sta ciasave + 2
        lda VIA1_ORA
        sta ciasave + 3
        lda #SR_LATCH
        sta CIA_TALO
        lda #$00
        sta CIA_TAHI                    ; stopped: loads the counter
        jsr spin
        lda #CLK_OUT                    ; alive: CLK alone
        sta IEC
        jsr xgo                         ; host takes DATA
        lda #$00
        sta IEC
        WAIT bcc, {lda IEC}, lsr        ; host drops DATA: idle
        rts

; Serial input first (CNT/SP released), then the drivers in.
spin:   lda #CRA_START
        sta CIA_CRA
        lda VIA1_ORA
        and #<~PA_FSDIR
        sta VIA1_ORA
        rts

; Undo open (and any 2 MHz switch) on every exit.
ciarest:
        lda #$00
        sta CIA_CRA
        lda ciasave + 1
        sta CIA_TALO
        lda ciasave + 2
        sta CIA_TAHI
        lda VIA1_ORA
        eor ciasave + 3
        and #PA_FSDIR | PA_2MHZ
        eor VIA1_ORA
        sta VIA1_ORA
        lda ciasave
        sta CIA_CRA
        bit CIA_ICR                     ; DOS sees no stale serial byte
        rts
