; Read Track with a fixed DRQ poll period, for a 1581: loaded at $0300 and
; called through J of a monitor_*_1581 with the head placed. Issues Read
; Track and polls the status every 18 + 5 P_PAD cycles (P_PAD >= 1) while no
; DRQ; a DRQ's byte is read 13 cycles after the status read that saw it, so a
; byte waits at most 31 + 5 P_PAD cycles. Bytes go to a ring over the DOS
; track cache (the last RING bytes stay), counted in X:Y. Waits for the
; first DRQ at most P_TMO timer B wraps (65536 us).
; Returns A = the WD status at the end ($FF: no DRQ in time), X:Y = bytes
; read (low, high); the ring's oldest byte sits at count mod RING.

        .include "mfm.inc"

RING_LO  = $0C                  ; ring pages: the DOS track cache
RING_HI  = $20
RING     = (RING_HI - RING_LO) * 256

phase    = tmp
tmo      = tmp + 1
cnt      = len

        .org $0300

        jmp rtpoll
        .byte "NYRP"
P_PAD:  .res 1
P_TMO:  .res 1

rtpoll: lda #0
        sta ptr
        sta cnt
        sta cnt + 1
        lda #RING_LO
        sta ptr + 1
        lda P_TMO
        sta tmo
        lda CIA_TBHI
        sta phase
        lda #WD_READTRK
        WDISSUE
wait:   WDTEST
        lda WDSTAT
        and #ST_DRQ
        bne go
        lda CIA_TBHI
        eor phase
        bpl wait
        eor phase
        sta phase
        bpl wait
        dec tmo
        bne wait
        lda #WD_FORCE
        WDISSUE
        lda #$FF
        ldx #0
        ldy #0
        rts
go:     ldy #0
        jmp data

; Pass without DRQ: 4 + 2 + 2 + 2 + 2 + 4 + (5 P_PAD - 1) + 3 = 18 + 5 P_PAD.
        ALIGN4 1
poll:   WDOK
        lda WDSTAT                      ; 4
        lsr                             ; 2     C: BUSY
        BR bcc, done                    ; 2
        and #ST_DRQ >> 1                ; 2
        BR bne, data                    ; 2 / 3
        ldx P_PAD                       ; 4 (abs)
:       dex                             ; 2
        BR bne, :-                      ; 3 (2 last)
        jmp poll                        ; 3
        WDALIGN
data:   WDOK
        lda WDDAT                       ; read 13 after the status
        sta (ptr),y
        inc cnt
        bne :+
        inc cnt + 1
:       iny
        BR bne, poll
        inc ptr + 1
        lda ptr + 1
        cmp #RING_HI
        bne poll
        lda #RING_LO
        sta ptr + 1
        jmp poll
done:   WDTEST
        lda WDSTAT
        ldx cnt
        ldy cnt + 1
        rts
