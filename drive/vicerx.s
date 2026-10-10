; C128 receiver for 1581 fast serial streams (drive/mfmstream.s) under VICE,
; standing in for the xum1541 v12 adapter (nybulah.vice). Loaded at $1300
; and entered with interrupts free to be masked.
;
; Host go: bus CLK asserted (CIA2 PA4) until the first byte arrives. Every
; byte the drive's 8520 shifts out lands in CIA1's SDR (input mode, fast
; serial direction in: MMU $D505 bit 3 clear); each is stored at DATA + n
; and, at LINES + n, the AND of every CIA2 port A read since the byte
; before it (bit 6: CLK in, 0 while the line is asserted). VICE delivers a
; byte once its eight bits have shifted, about 36 drive cycles after the SDR
; write, when the drive has already released CLK (asserted 4 cycles before
; the write to 16 or more after): the CLK seen between two deliveries
; belongs to the second. The port reads also bring the emulated drive up to
; the C128's clock. The 8502 runs at 2 MHz, the drive's rate, with the VIC's
; cycles given up ($D030): a poll is 11 cycles, a byte taken 23.
;
; n is X plus 256 times the pages done; the page counters sit in the store
; instructions, whose data page address the word at $1303 gives. Stores stop
; when DATA reaches LINES (the loop then spins on full).

        .setcpu "6502"

CIA1_SDR = $DC0C
CIA1_ICR = $DC0D
CIA1_CRA = $DC0E
CIA1_CRB = $DC0F
CIA2_PRA = $DD00
CIA2_ICR = $DD0D
VIC_CLKR = $D030
MMU_CR   = $D505
PA_CLKOUT = $10
MMU_FSDIR = $08
ICR_CLEAR = $7F
DATA     = $2000
LINES    = $7000

        .org $1300

        jmp start
        .word sdata + 2

start:  sei
        lda #0
        sta CIA1_CRA                    ; timers stopped, serial port input
        sta CIA1_CRB
        lda #ICR_CLEAR
        sta CIA1_ICR
        sta CIA2_ICR
        lda CIA1_ICR
        lda CIA2_ICR
        lda MMU_CR
        and #<~MMU_FSDIR
        sta MMU_CR
        lda #1
        sta VIC_CLKR
        ldx #0
        lda CIA2_PRA
        ora #PA_CLKOUT
        sta CIA2_PRA                    ; go
go:     lda CIA2_PRA
        ldy CIA1_ICR
        beq go
        lda CIA2_PRA
        and #<~PA_CLKOUT
        sta CIA2_PRA                    ; go released
        lda #0                          ; START: go held CLK
        beq take

next:   lda #$FF
loop:   and CIA2_PRA                    ; 4
        ldy CIA1_ICR                    ; 4
        beq loop                        ; 3
take:   ldy CIA1_SDR                    ; 4
sline:  sta LINES,x                     ; 5
        tya                             ; 2
sdata:  sta DATA,x                      ; 5
        inx                             ; 2
        bne next                        ; 3
        inc sline + 2
        inc sdata + 2
        lda sdata + 2
        cmp #>LINES
        bne next
full:   jmp full
