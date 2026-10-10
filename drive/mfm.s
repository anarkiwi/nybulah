; 1581 head motion, probing, sector I/O and track writing through the WD177x,
; loaded at $0300 (512 bytes) and $0790 (the rest, after the largest 1581
; monitor) and called through the J command of a monitor_*_1581. Buffers
; live in the DOS track cache ($0C00-$1FFF), which the host invalidates
; afterwards.
; Entries (jump table at $0300 + 3 k), parameters and results in the block
; that follows the "NYMF" tag; A, X, Y returned as listed:
;
;  0 sense    force interrupt (no command runs: type I status), then
;             A = status (T0, IP, WP, MO live), X = track register,
;             Y = CIA port A; P_PB = CIA port B. Nothing moves.
;  1 motor    P_ARG bit 0: motor and activity LED on (1) or off (0).
;  2 side     PA0 = P_ARG bit 0 (0: header side 0, the drive's head 1).
;  3 restore  WD Restore at the 12 ms rate, bounded: P_ARG = c (<= MAX_CYL)
;             is the most step pulses allowed. Pulses come STEP_US apart
;             and TR00 is sampled STEP_US after each, so a force interrupt
;             (c - 1/2) STEP_US after the command stops the WD between pulse
;             c and pulse c + 1. A = 0 with TR00 sensed (track register 0),
;             $80 | type I status without it, $FF for c too large; c = 0 only
;             checks TR00. For c > 0, P_T1 = microseconds from a stamp
;             before the command to its end; P_RS = its first valid status,
;             the last status before the force interrupt (bit 0: stopped by
;             the deadline), the type I status after it and the track
;             register then ($FF less the pulses issued when stopped by the
;             deadline; 0 when the WD found TR00 itself).
;  4 seek     WD Seek from the track register to P_ARG (<= MAX_CYL, else
;             A = $FF and nothing moves); A = status.
;  5 index    two rising index edges (type I status IP): times in P_T0,
;             P_T1; A = 0, or $FF on timeout.
;  6 readaddr one Read Address: P_ID = the six bytes, P_T0 = time of the
;             first; A = status, $FF on timeout.
;  7 readsec  Read Sector P_SEC with the track register at P_ARG (restored
;             afterwards): the first P_LEN bytes to P_BUF (later ones are
;             written to ROM); A = status, $FF on timeout; X:Y = bytes read.
;  8 writesec P_CNT sectors from P_SEC up, track register P_ARG, P_LEN bytes
;             each from P_BUF, back to back; P_FLAGS bit 0 writes deleted
;             data marks; A = the status of the last sector written or the
;             first with WP, RNF or LD, X = sectors written.
;  9 writetrk Write Track from the image at P_BUF: token t: 0 ends it,
;             1..127 repeats the next byte t times, 128..255 copies t - 127
;             bytes; after the end the last byte repeats until the WD stops
;             at the index. A = status, $FF on timeout.
; 10 settrk   track register = P_ARG (nothing moves).
;
; Write commands carry the precompensation bit the DOS gives P_CYL. Every
; wait counts timer B wraps (65536 us) into P_TMO and gives up when it runs
; out (any CIA flag the mask enables counts: the ICR read clears them all).
; Times are 24 bit microseconds: wraps << 16 | elapsed (= ~timer B).

        .include "mfm.inc"

        .org $0300

        jmp sense
        jmp motor
        jmp side
        jmp restore
        jmp seek
        jmp index
        jmp readaddr
        jmp readsec
        jmp writesec
        jmp writetrk
        jmp settrk
        .byte "NYMF"
P_ARG:  .res 1
P_SEC:  .res 1
P_CNT:  .res 1
P_CYL:  .res 1
P_FLAGS: .res 1
P_TMO:  .res 1
P_BUF:  .res 2
P_LEN:  .res 2
P_PB:   .res 1
P_T0:   .res 3
P_T1:   .res 3
P_ID:   .res 6
P_RS:   .res 4
wraps:  .res 1
tmo:    .res 1
fill:   .res 1
trksave: .res 1
pages:  .res 1
count:  .res 2
due:    .res 3
t:      .res 3                  ; elapsed lo, hi, wraps
raw:    .res 3                  ; timer B high, low, high again

; Command A; A = its first valid status (mfm.inc WDISSUE).
wdcmd:  WDISSUE
        rts

; Type I status with T0 live: force interrupt, then a Seek to the track
; register's own value (data register = track register: no step pulse,
; datasheet Seek flowchart), each waited out until idle. T0 is updated only
; by a type I command (datasheet status register note 4; an idle $D0 leaves
; it clear on the 1581); the WD alone sees TR00 (schematic sheet 2) and the
; DOS never reads it. A = status, X = track register, Y clobbered.
status1:
        lda #WD_FORCE
        jsr wdgo
        WDTEST
        ldx WDTRK
        WDTEST
        stx WDDAT
        lda #WD_SEEK
; Command A, then A = its status once busy reads clear (mfm.inc WDIDLE).
wdgo:   jsr wdcmd
        WDIDLE
        rts

; A wrap seen: count it and spend a unit of the timeout; Z set when it ran
; out. Called from the waits whenever ICR bit 7 is set.
spend:  inc wraps
        dec tmo
        rts

; Timer B into raw: high, low, high again.
.macro STAMP
        lda CIA_TBHI
        sta raw
        lda CIA_TBLO
        sta raw + 1
        lda CIA_TBHI
        sta raw + 2
.endmacro

; raw (taken with wraps = A) into t: the high read on the low read's side of
; a borrow (a low byte of $80 or more was read after it), inverted to
; elapsed. A wrap counted since the reads (at most one: they are under a
; wrap apart) belongs to them only when the elapsed count had restarted.
time:   sta t + 2
        lda raw + 1
        eor #$FF
        sta t
        lda raw
        ldx raw + 1
        bpl :+
        lda raw + 2
:       eor #$FF
        sta t + 1
        lda CIA_ICR
        and #ICR_TB
        beq :+
        jsr spend
:       lda wraps
        cmp t + 2
        beq :+
        lda t + 1
        bmi :+
        inc t + 2
:       rts

; Now into t.
now:    lda CIA_ICR
        and #ICR_TB
        beq :+
        jsr spend
:       STAMP
        lda wraps
        jmp time

; Copy t to P_T0 (X = 0) or P_T1 (X = 3).
keep:   ldy #0
:       lda t,y
        sta P_T0,x
        inx
        iny
        cpy #3
        bne :-
        rts

; Fresh timeout and wrap count, any command stopped.
setup:  lda #0
        sta wraps
        lda P_TMO
        sta tmo
        jmp status1

tmout:  jsr status1
        lda #$FF
        rts

sense:  jsr status1
        pha
        lda CIA_PB
        sta P_PB
        WDTEST
        ldx WDTRK
        ldy CIA_PA
        pla
        rts

motor:  lda CIA_PA
        ora #PA_MOTOR
        and #<~PA_LED
        lsr P_ARG
        bcc :+
        eor #PA_MOTOR | PA_LED
:       sta CIA_PA
        rts

side:   lda CIA_PA
        and #<~PA_SIDE
        lsr P_ARG
        adc #0
        sta CIA_PA
        rts

; due = (c - 1/2) STEP_US from a stamp taken before the command.
restore:
        jsr setup
        ldx P_ARG
        bne :+
        jmp rdone
:       cpx #MAX_CYL + 1
        bcs rbad
        lda #<(-STEP_US / 2)
        sta due
        lda #>(-STEP_US / 2)
        sta due + 1
        lda #$FF
        sta due + 2
:       lda due
        clc
        adc #<STEP_US
        sta due
        lda due + 1
        adc #>STEP_US
        sta due + 1
        bcc :+
        inc due + 2
:       dex
        bne :--
        jsr now
        ldx #0
        jsr keep
        lda #WD_RESTORE
        jsr wdcmd
        sta P_RS
rpoll:  sta P_RS + 1
        lsr
        bcc rdone
        jsr elapsed
        lda t
        cmp due
        lda t + 1
        sbc due + 1
        lda t + 2
        sbc due + 2
        bcs rdone
        WDTEST
        lda WDSTAT
        bcc rpoll
rdone:  jsr elapsed
        ldx #3
        jsr keep
        jsr status1                     ; stops the WD before pulse c + 1
        sta P_RS + 2
        WDTEST
        ldx WDTRK
        stx P_RS + 3
        and #ST_T0
        beq rfail
        lda #0
        WDTEST
        sta WDTRK
        rts
rfail:  jsr status1
        ora #$80
        rts
rbad:   lda #$FF
        rts

; t = now - P_T0.
elapsed:
        jsr now
        ldx #0
        sec
:       lda t,x
        sbc P_T0,x
        sta t,x
        inx
        txa
        eor #3
        bne :-
        rts

seek:   lda P_ARG
        cmp #MAX_CYL + 1
        bcs rbad
        WDTEST
        sta WDDAT
        lda #WD_SEEK
        jsr wdcmd
wait:   WDTEST
:       lda WDSTAT
        lsr
        bcs :-
        rol
        rts

settrk: jsr status1
        lda P_ARG
        WDTEST
        sta WDTRK
        rts

        .segment "CODE2"
        .org $0790

; Wait until the index bit of the type I status equals A.
ipwait: sta fill
:       bit CIA_ICR
        bpl :+
        jsr spend
        beq ipout
:       WDTEST
        lda WDSTAT
        and #ST_IP
        cmp fill
        bne :--
        rts
ipout:  pla
        pla
        jmp tmout

index:  jsr setup
        ldx #0
:       lda #0
        jsr ipwait
        lda #ST_IP
        jsr ipwait
        txa
        pha
        jsr now
        pla
        tax
        jsr keep
        cpx #6
        bne :-
        lda #0
        rts

readaddr:
        jsr setup
        lda #WD_READADDR
        jsr wdcmd
        ldy #0
ral:    WDTEST
        lda WDSTAT
        lsr
        and #ST_DRQ >> 1                ; DRQ before BUSY: the last byte may wait
        bne :+
        bcc rae
        bit CIA_ICR
        bpl ral
        jsr spend
        bne ral
        jmp tmout
:       WDTEST
        lda WDDAT
        sta P_ID,y
        tya
        bne :+
        STAMP
        lda wraps
        sta t + 2
:       iny
        cpy #6
        bne ral
rae:    jsr wait
        pha
        cpy #0
        beq :+
        lda t + 2
        jsr time
        ldx #0
        jsr keep
:       pla
        rts

; ptr = P_BUF; X = low count and pages = high count, rounded up (the loops
; run X down, then pages).
buffer: lda P_BUF
        sta ptr
        lda P_BUF + 1
        sta ptr + 1
        ldx P_LEN
        cpx #1
        lda P_LEN + 1
        adc #0
        sta pages
        ldy #0
        rts

; Save the track register.
savetrk:
        WDTEST
        lda WDTRK
        sta trksave
        rts

; Track register = P_ARG, sector register = A.
regs:   WDTEST
        sta WDSEC
        lda P_ARG
        WDTEST
        sta WDTRK
        rts

restrk: pha
        lda trksave
        WDTEST
        sta WDTRK
        pla
        rts

readsec:
        jsr setup
        jsr savetrk
        lda P_SEC
        jsr regs
        jsr buffer
        sty count
        sty count + 1
        lda #WD_READSEC
        jsr wdcmd
rsl:    WDTEST
        lda WDSTAT
        lsr
        and #ST_DRQ >> 1                ; DRQ before BUSY: the last byte may wait
        bne rsd
        bcc rse
        bit CIA_ICR
        bpl rsl
        jsr spend
        bne rsl
        jsr restrk
        jmp tmout
rsd:    WDTEST
        lda WDDAT
        sta (ptr),y
        iny
        bne :+
        inc ptr + 1
:       inc count
        bne :+
        inc count + 1
:       dex
        bne rsl
        dec pages
        bne rsl
        lda #>$8000                     ; full: later bytes go to ROM
        sta ptr + 1
        jmp rsl
rse:    jsr wait
        jsr restrk
        ldx count
        ldy count + 1
        rts

; Precompensation bit for P_CYL (msub.src precmp).
precomp:
        ldx P_CYL
        cpx #PRECOMP_CYL
        bcc :+
        ora #WD_NOPRECOMP
:       rts

writesec:
        jsr setup
        jsr savetrk
        lda #0
        sta count
wsl:    lda P_SEC
        clc
        adc count
        jsr regs
        jsr buffer
        lda P_FLAGS
        and #WD_DELETED
        ora #WD_WRITESEC
        jsr precomp
        jsr wdcmd
wsb:    WDTEST
        lda WDSTAT
        lsr
        bcc wsd
        and #ST_DRQ >> 1
        bne :+
        bit CIA_ICR
        bpl wsb
        jsr spend
        bne wsb
        jsr restrk
        jmp tmout
:       lda (ptr),y
        WDTEST
        sta WDDAT
        iny
        bne :+
        inc ptr + 1
:       dex
        bne wsb
        dec pages
        bne wsb
wsd:    jsr wait
        jsr restrk
        WDTEST
        lda WDSTAT
        and #ST_WP | ST_RNF | ST_LD
        bne :+
        inc count
        lda P_BUF
        clc
        adc P_LEN
        sta P_BUF
        lda P_BUF + 1
        adc P_LEN + 1
        sta P_BUF + 1
        lda count
        cmp P_CNT
        bcc wsl
:       WDTEST
        lda WDSTAT
        ldx count
        rts

; Write Track feed: one loop for repeat tokens, one for literal tokens, X the
; next byte, cnt the token's bytes left with it. A byte is made ready right
; after the last write and written on DRQ. From one write to the next takes
; less than a byte time (64 cycles) on every path, a token decoded between
; them included (59 at most), so no token boundary eats into the next byte's
; margin: a DRQ is written within one poll pass and the write, 33 cycles, and
; the data register is loaded 31 or more cycles before the WD takes the byte
; whatever the image. Wraps only spend the timeout.
cnt      = len

; The next token from (ptr),y: X its first byte, cnt its length, on to rep or
; lit (the repeat or literal loop); token 0 goes to fin.
.macro DECODE rep, lit, fin
        .local literal
        lda (ptr),y
        beq fin
        iny
        bne :+
        inc ptr + 1
:       cmp #$80
        bcs literal
        sta cnt
        lda (ptr),y
        tax
        iny
        bne rep
        inc ptr + 1
        bne rep                         ; always
literal:
        sbc #$7F
        sta cnt
        lda (ptr),y
        tax
        iny
        bne lit
        inc ptr + 1
        bne lit                         ; always
.endmacro

; Poll for DRQ (to put) while busy (else wtd), spending the timeout.
.macro WPOLL put
        .local poll
poll:   WDTEST
        lda WDSTAT
        and #ST_BUSY | ST_DRQ
        lsr
        bcc wtd
        bne put
        bit CIA_ICR
        bpl poll
        dec tmo
        bne poll
        jmp tmout
.endmacro

; The command, then the first token decoded: the first status read (DRQ is
; up from the command) comes STATUS_VALID or more cycles after the write, 2
; + (5 WT_LOOPS - 1) + 3 + 35 at the least (the shortest decode to a read),
; and the first byte follows it within three byte times, which the datasheet
; asks for. The track register and precompensation are as for Write Sector.
WT_LOOPS = (STATUS_VALID - 39 + 4) / 5
.assert 2 + 5 * WT_LOOPS - 1 + 3 + 35 >= STATUS_VALID, error, "WT_LOOPS"
writetrk:
        jsr setup
        jsr buffer
        lda #WD_WRITETRK
        jsr precomp
        WDTEST
        sta WDCMD
        ldx #WT_LOOPS
:       dex
        bne :-
        beq tok                         ; always
wtd:    jmp wait
tok:    DECODE wpoll_r, wpoll_l, wend
wend:   sta cnt                         ; A = 0: the last byte from now on
        beq wpoll_r
wput_r: WDTEST
        stx WDDAT
wloop_r:
        dec cnt
        beq tok
wpoll_r:
        WPOLL wput_r
wput_l: WDTEST
        stx WDDAT
wloop_l:
        dec cnt
        beq tok
        lda (ptr),y
        tax
        iny
        bne wpoll_l
        inc ptr + 1
wpoll_l:
        WPOLL wput_l

