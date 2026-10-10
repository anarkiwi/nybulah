; Shift register gap probe for a 1571 (6526) or, with -D M1581=1, a 1581
; (8520): lone bytes after a programmed idle, sent exactly as the streams
; send metadata (drive/stream.s meta, drive/mfmstream.s send), with the
; host in the firmware v12 stream receive. Called through J of monitor_s4,
; loaded at $0300 (one block). It enters output mode as the 1581 stream
; does (drivers out, CRA output, input, output), sends START, then every
; table entry after its idle, then END, and leaves the CIA in input mode
; with the drivers in. Around each write the CIA registers go to a log.
;
; Table T (the "NYSG" tag + 4), entries of four bytes, flags $FF ends it:
;   gap lo, hi  idle before the write, GAP_UNIT cycles a unit, ATN polled
;   value       the byte
;   flags       bit 0: CLK asserted around the write (metadata)
;               bit 1: read ICR before and after the write (clearing its
;                      flags, as a stream polling ICR does); else never
;               bit 2: write CRA (output mode, timer A running) before it
; Log, LOG_LEN bytes an entry from LOG, START first, the END last:
;   0 port (IEC)        1 CRA           2 timer A low   3 ICR before (bit 1)
;   4 ICR after (bit 1) 5 timer B high  6 timer B low   7 timer B high
;   8 value             9 flags        10 port after
; ICR after is read ICR_AT or more cycles after the SDR write: the shifter
; flags a byte within 39 (ciaprobe), so bit 3 there says it was shifted out.
; Returns A = M_END (table done), M_END_ATN (ATN seen in an idle) or
; ST_NOGO (the host never asserted CLK); X = entries logged; Y = the port.

        .setcpu "6502"

.ifdef M1581
IEC      = $4001                ; 8520 port B: PB2 CLK in, PB3 CLK out, PB5 drivers, PB7 ATN in
FSPORT   = $4001
FSDIR    = $20
CLK_IDLE = FSDIR                ; port value with CLK released
LOG      = $0C00                ; the DOS track cache
.else
IEC      = $1800                ; VIA1 port B: PB2 CLK in, PB3 CLK out, PB7 ATN in
FSPORT   = $180F                ; VIA1 port A without handshake: PA1 drivers
FSDIR    = $02
CLK_IDLE = 0
LOG      = $6000                ; expansion RAM
.endif
CLK_IN   = $04
CLK_OUT  = $08
CIA_TALO = $4004
CIA_TBLO = $4006
CIA_TBHI = $4007
CIA_SDR  = $400C
CIA_ICR  = $400D
CIA_CRA  = $400E
CRA_START = $01
CRA_SPOUT = $40
ICR_SP   = $08
M_START  = $04
M_END    = $40
M_END_ATN = $48
ST_NOGO  = $FF
F_META   = $01
F_ICR    = $02
F_CRA    = $04
ENTRIES  = 32
GAP_UNIT = 100                  ; cycles an idle unit (the loop below)
LOG_LEN  = 16
LOG_PAGES = 3                   ; pages cleared at entry: (ENTRIES + 2) * LOG_LEN
ICR_AT   = 48                   ; SDR write -> the ICR read after it (48 or 49)
GO_OUTER = 2                    ; x 65536 go polls of 13 cycles: 0.85 s

gaplo   = $30
gaphi   = $31
idx     = $32
cnt     = $33
lp      = $34                   ; log pointer
flags   = $36
val     = $37

        .org $0300

        jmp probe
        .byte "NYSG"
T:      .res 4 * ENTRIES + 4

probe:  lda #0
        sta idx
        sta cnt
        sta lp
        lda #>LOG
        sta lp + 1
        tay
        lda #0
:       sta (lp),y                      ; the log cleared: a lost run is read
        iny                             ; back over DOS M-R, no row stale
        bne :-
        inc lp + 1
        ldx lp + 1
        cpx #>LOG + LOG_PAGES
        bne :-
        lda #<LOG
        sta lp
        lda #>LOG
        sta lp + 1
        lda #GO_OUTER
        sta gaphi
        ldx #0
        ldy #0
go:     lda IEC                         ; host go: CLK
        and #CLK_IN
        bne going
        dey
        bne go
        dex
        bne go
        dec gaphi
        bne go
        ldx cnt
        ldy IEC
        lda #ST_NOGO
        rts
going:  lda FSPORT
        ora #FSDIR
        sta FSPORT
        lda #CRA_START | CRA_SPOUT      ; output, input, output (spout_patch)
        sta CIA_CRA
        lda #CRA_START
        sta CIA_CRA
        lda #CRA_START | CRA_SPOUT
        sta CIA_CRA
        lda #M_START
        ldx #F_META
        jsr emit
loop:   ldy idx
        lda T + 3,y
        cmp #$FF
        beq done
        sta flags
        lda T,y
        sta gaplo
        lda T + 1,y
        sta gaphi
        lda T + 2,y
        sta val
        tya
        clc
        adc #4
        sta idx
        jsr gap
        bcs atn
        lda val
        ldx flags
        jsr emit
        jmp loop
atn:    lda #M_END_ATN
        .byte $2C                       ; bit abs: skips the lda below
done:   lda #M_END
        pha
        ldx #F_META
        jsr emit
        ldy #0
:       lda CIA_ICR                     ; END out
        and #ICR_SP
        bne :+
        dey
        bne :-
:       lda #CRA_START
        sta CIA_CRA
        lda FSPORT
        and #<~FSDIR
        sta FSPORT
        ldx cnt
        ldy IEC
        pla
        rts

; Idle gaphi:gaplo units, ATN polled every unit; C set when ATN was seen.
gap:    lda gaplo
        ora gaphi
        beq gret
gl:     bit IEC                         ; 4
        bmi gatn                        ; 2
        ldy #(GAP_UNIT - 30) / 5        ; 2
:       dey                             ; 2
        bne :-                          ; 3 (2 last): 5 x the count - 1
        bit $00                         ; 3
        lda gaplo                       ; 3
        bne :+                          ; 3 / 2
        dec gaphi                       ; 5 (a borrow pass: 4 more)
:       dec gaplo                       ; 5
        lda gaplo                       ; 3
        ora gaphi                       ; 3
        bne gl                          ; 3     GAP_UNIT a pass
        .assert (GAP_UNIT - 30) .mod 5 = 0, error, "GAP_UNIT"
gret:   clc
        rts
gatn:   sec
        rts

; Write A through the shift register with flags X, logging around it: the
; metadata path asserts CLK 4 cycles before the write and releases it 24
; after, as mfmstream.s send does; a plain byte leaves CLK alone. The log
; and bookkeeping take over 40 cycles, so the next write is 40 or more after
; this one whatever the next idle.
emit:   sta val
        stx flags
        ldy #0
        lda IEC
        sta (lp),y                      ; 0 port
        iny
        lda CIA_CRA
        sta (lp),y                      ; 1 CRA
        iny
        lda CIA_TALO
        sta (lp),y                      ; 2 timer A low
        iny
        txa
        and #F_ICR
        beq :+
        lda CIA_ICR
:       sta (lp),y                      ; 3 ICR before (0 when not read)
        txa
        and #F_CRA
        beq :+
        lda #CRA_START | CRA_SPOUT
        sta CIA_CRA
:       ldy #5
        lda CIA_TBHI
        sta (lp),y                      ; 5 timer B high
        iny
        lda CIA_TBLO
        sta (lp),y                      ; 6 timer B low
        iny
        lda CIA_TBHI
        sta (lp),y                      ; 7 timer B high
        ldx val
        lda flags
        and #F_META
        beq plain
        lda #CLK_OUT | CLK_IDLE
        sta IEC                         ; t = -4
        stx CIA_SDR                     ; t = 0
        ldy #3                          ; 4
:       dey                             ; 2
        bne :-                          ; 3 (2 last): 6 .. 19
        nop                             ; 20
        lda #CLK_IDLE                   ; 22
        sta IEC                         ; t = 24: CLK released
        ldy #2                          ; 28
:       dey
        bne :-                          ; 30 .. 38
        ldy #4                          ; 39
        jmp after
plain:  stx CIA_SDR                     ; t = 0
        ldy #7                          ; 4
:       dey
        bne :-                          ; 6 .. 39
        ldy #4                          ; 40
        bit $00                         ; 42
after:  lda flags                       ; 42 / 45
        and #F_ICR
        beq logged                      ; 0 when ICR is not read
        lda CIA_ICR                     ; t = ICR_AT or ICR_AT + 1
logged: sta (lp),y                      ; 4 ICR after
        ldy #8
        lda val
        sta (lp),y                      ; 8 value
        iny
        lda flags
        sta (lp),y                      ; 9 flags
        iny
        lda IEC
        sta (lp),y                      ; 10 port after
        lda lp
        clc
        adc #LOG_LEN
        sta lp
        bcc :+
        inc lp + 1
:       inc cnt
        rts
