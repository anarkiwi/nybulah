; One march element over a table of RAM pages, run through the monitor's 'J'.
;
; Assembled at two page origins (ramtest_10.bin at $1000, ramtest_11.bin at
; $1100); the bytes that differ are high bytes of addresses inside the block,
; so the host relocates it to any page. The layout below the entry jump is
; shared with nybulah/ramtest.py.
;
; For each page in pages[pfirst], stepping pstep until pend, and each offset
; from yfirst, stepping ystep until it wraps back to yfirst, the cell value
; for data D is D ^ K with the background
;   K = (offset & ymask) ^ (cmask if offset is odd) ^ (page & pmask) ^ c0.
; flags bit 7 reads and compares with D = dr, then bit 6 writes D = dw. A
; mismatch counts and, for the first NLOG, logs offset, page, expected and read.
; pcl/pch count the mismatches of each page index. Only the cells in pages[]
; are written; ptr is restored from the stack. Returns the count in A (low), X.

ptr     = $30
NLOG    = 16
MAXP    = 64

        .segment "CODE"
start:  jmp run
flags:  .res 1
dr:     .res 1
dw:     .res 1
ymask:  .res 1
cmask:  .res 1
pmask:  .res 1
c0:     .res 1
pfirst: .res 1
pend:   .res 1
pstep:  .res 1
yfirst: .res 1
ystep:  .res 1
count:  .res 2
log:    .res 4 * NLOG
pk:     .res 1
k:      .res 1
fx:     .res 1
sx:     .res 1
pages:  .res MAXP
pcl:    .res MAXP                       ; failures per page, low bytes
pch:    .res MAXP                       ; then high bytes
.assert pch = pcl + MAXP && 2 * MAXP <= 128, error, "pcl/pch cleared as one run"

run:    lda ptr
        pha
        lda ptr+1
        pha
        lda #0
        sta ptr
        sta count
        sta count+1
        ldx #2 * MAXP - 1
:       sta pcl,x
        dex
        bpl :-
        ldx pfirst
page:   lda pages,x
        sta ptr+1
        and pmask
        eor c0
        sta pk
        ldy yfirst
cell:   tya
        lsr
        lda #0
        bcc :+
        lda cmask
:       eor pk
        sta k
        tya
        and ymask
        eor k
        sta k
        bit flags
        bpl nord
        eor dr
        eor (ptr),y
        beq nord
        jsr fail
nord:   bit flags
        bvc nowr
        lda k
        eor dw
        sta (ptr),y
nowr:   tya
        clc
        adc ystep
        tay
        cpy yfirst
        bne cell
        txa
        clc
        adc pstep
        tax
        cpx pend
        bne page
        pla
        sta ptr+1
        pla
        sta ptr
        lda count
        ldx count+1
        rts

; A = read ^ expected (nonzero). Preserves X and Y.
fail:   stx sx
        sta fx
        lda count+1
        bne bump
        lda count
        cmp #NLOG
        bcs bump
        asl
        asl
        tax
        tya
        sta log,x
        lda ptr+1
        sta log+1,x
        lda k
        eor dr
        sta log+2,x
        eor fx
        sta log+3,x
bump:   ldx sx
        inc pcl,x
        bne :+
        inc pch,x
:       inc count
        bne :+
        inc count+1
:       rts
