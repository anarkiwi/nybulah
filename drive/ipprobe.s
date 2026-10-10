; WD177x status edge probe for a 1581, loaded at $0300 and called through
; J of a monitor_*_1581 (which has set up the CIA timers). Issues up to
; four commands from P_CMD (0 ends the list; a Seek, $1x, first copies the
; track register to the data register: no step pulse), each but the last
; waited out until busy reads clear, then polls the status register for
; P_WRAPS timer B wraps (65536 us each), logging the full status every time
; its P_MASK bits change, the state at the start first.
;
; Log at LOG, E_LEN bytes an entry, at most LOG_MAX entries:
;   0 status   1 timer B high   2 timer B low   3 timer B high again
;   4 wraps counted at the last poll   5 timer B high at that poll
; Returns A:X = entries logged (low, high).

        .include "mfm.inc"

LOG      = $0C00                ; the DOS track cache
LOG_MAX  = 640
E_LEN    = 6
.assert LOG + LOG_MAX * E_LEN <= $2000, error, "log past the track cache"

wraps    = tmp
phase    = tmp + 1
last     = tmp + 2

        .org $0300

        jmp probe
        .byte "NYIP"
P_CMD:  .res 4
P_MASK: .res 1
P_WRAPS: .res 1
ci:     .res 1
tmo:    .res 1

probe:  lda #<LOG
        sta ptr
        lda #>LOG
        sta ptr + 1
        lda #0
        sta len
        sta len + 1
        sta wraps
        sta ci
next:   ldx ci
        lda P_CMD,x
        beq poll0
        pha
        and #$F0
        cmp #WD_SEEK & $F0
        bne :+
        WDTEST
        ldy WDTRK
        WDTEST
        sty WDDAT
:       pla
        WDISSUE
        inc ci
        ldx ci
        cpx #4
        beq poll0
        ldy P_CMD,x
        beq poll0
        WDIDLE
        jmp next

poll0:  lda P_WRAPS
        sta tmo
        lda CIA_TBHI
        sta phase
        WDTEST
        lda WDSTAT
        jmp log

poll:   WDTEST
        lda WDSTAT
        tax
        and P_MASK
        cmp last
        bne logx
tb:     lda CIA_TBHI
        eor phase
        bpl poll
        eor phase
        sta phase
        bpl poll
        inc wraps
        dec tmo
        bne poll
end:    lda len
        ldx len + 1
        rts

logx:   txa
log:    ldy #0
        sta (ptr),y
        and P_MASK
        sta last
        lda CIA_TBHI
        iny
        sta (ptr),y
        lda CIA_TBLO
        iny
        sta (ptr),y
        lda CIA_TBHI
        iny
        sta (ptr),y
        lda wraps
        iny
        sta (ptr),y
        lda phase
        iny
        sta (ptr),y
        lda ptr
        clc
        adc #E_LEN
        sta ptr
        bcc :+
        inc ptr + 1
:       inc len
        bne :+
        inc len + 1
:       lda len
        cmp #<LOG_MAX
        bne tb
        lda len + 1
        cmp #>LOG_MAX
        bne tb
        beq end
