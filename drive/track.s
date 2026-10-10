; Raw track capture and write for the 1541 and 1571, called through the
; monitor's J command. Assembled per model (-D MODEL=1541 or 1571), which
; fixes the expansion RAM buffer (BUFPG, NPAGES pages); CODE is linked at
; the base given to ld65 -S and padded to $200 bytes, followed in the same
; file by the PASS segment for the expansion RAM page after the buffer.
; The host fills the parameter block, calls an entry point
; and reads results back from the block. Every entry returns the status byte
; in A.
;
;   base+0  prep   motor/LED/density bits, 1571 side, step, settle
;   base+3  read   one capture pass of npages pages into the buffer
;   PASS    write  write npages pages from the buffer, then read mode
;
; -D SEEK=1 (1571) assembles prep (and its delay) alone, in base RAM, for
; drives streaming without expansion RAM (drive/stream.s takes the same page
; to stream); prep is the same code either way.
;
; Parameters and state live in zero page at ZP (the host saves and restores
; that range around a session). The host also saves PCR (and on the 1571
; VIA1 PA, clearing PA5 for 1 MHz) before the first call and restores them
; afterwards; read and write leave SOE off on return.
;
; VIA2 $1C00: PB0-1 stepper phase, PB2 motor, PB3 LED, PB4 write enable
; (0 = protected), PB5-6 density, PB7 SYNC (0 = sync). $1C01 GCR data.
; PCR $1C0C: CA2 = SOE (byte ready to SO), CB2 = mode (1 read, 0 write).
; Byte ready sets V through SO; no BIT on a VIA register while SOE is on,
; since BIT overwrites V.
;
; VIA1 timer 2 counts down in one-shot mode (ACR bit 5 clear, set up by the
; host) and keeps counting after it expires. VIA1 timer 1 is the monitor's
; watchdog and is not touched.
; 1571: VIA1 PA2 side, PA5 2 MHz; WD1770 at $2000-$2003. Its status bit 1 is
; the live index only in type I status, which a force interrupt shows only
; when the WD is idle (datasheet); so prep ends by taking it as the 1571 DOS
; does (diskin): Force Interrupt, then a Seek to the track register's own
; value (no step pulse), each followed by a wait for busy to clear, and
; leaves that status in wdst. Every WD access keeps the DOS's address rule
; (wdtest.inc).
;
; Read passes (kind):
;   0 BITS  the byte stored per byte ready
;   1 TB    T2 low byte stored per byte ready
;   2 TS    per SYNC: TC, TH = bytes counted before it, TD = X after the
;           release wait; a wait that wraps X stores an entry with TD = 0
;           and the same count, so a sync spans its run of entries
; Start modes: 0 now, 1 from within a sync (the pass's first byte follows
; it), 2 at the second of two index edges (1571), 3 after alen bytes equal
; to anchor (TB and TS). The anchor matcher resets
; to its first byte on a mismatch without re-testing the mismatched byte;
; the host chooses anchors that this finds.
;
; Every wait is bounded: an inner counter in X (or Y) and the outer tmo,
; 256 x 256 iterations of at least 9 cycles (over 3 revolutions) of
; cumulative waiting per call, then a timeout status.
;
; Cycle tables (1 MHz). s is the cycle a branch samples V; a byte latched at
; L is seen by the first sample at or after L.
;
; Byte-ready waits (BITS, write, anchor): VWAIT, samples 4 and 5 cycles
; apart, 7 once per 256 iterations. BITS from a sample that sees V: data
; read at s+8, next wait at s+19, s+32 after a page change. A byte detected
; u cycles late leaves u' <= max(7, 7 + 32 - T) for the next, so nothing is
; lost while 7 + 32 + 8 < 2T: T > 23.5, 8 cells at zone 3 above 331 rpm.
; Anchor matcher: next wait at s+23 (match or mismatch), < T. From the
; sample that sees the last anchor byte, TB's wait starts after 41 cycles
; and TS's poll after 41, inside the two byte periods that would merge V.
;
; TB: TB_CHAIN samples 2 apart, then ldx #0 and a loop sampling at 0, 4, 8
; (bvc) of every 11 cycles; X reaching 0 takes the out path, sampling at 9
; and 16 and resuming the loop at 21. T2 is read 5 cycles after a bvs
; sample, 4 after the bvc one. The next wait starts 14 cycles after the T2
; read (27 after a page change). A byte detected in the chain is followed
; by a wait that starts before the next byte while T > 21. The host rebuilds
; each byte's sample window from these numbers (nybulah.passes).
;
; TS: poll V and PB7 every 13 cycles (an iteration that counts a byte skips
; the PB7 read). SYNC seen low: the release wait reads PB7 every 11 cycles
; from 7 cycles after the handler starts; after the release is seen the poll
; resumes within 32 cycles. A short SYNC was still low at the read that saw
; it, so the release follows that read and the two bytes after it are
; counted separately while 11 + 32 + 3 < 16 cells. PB7 reads in the poll
; are at most 46 cycles apart (a page of counted bytes after the rare
; timeout check); release-wait reads come 11 + 11i + 34 * (i / 256) cycles
; after the read that saw SYNC.

        .setcpu "6502"

.ifndef SEEK
SEEK = 0
.endif

.if MODEL = 1541
BUFPG    = $80
.elseif MODEL = 1571
BUFPG    = $60
.else
        .error "MODEL must be 1541 or 1571"
.endif
NPAGES   = 31
PASSPG   = BUFPG + NPAGES       ; the PASS segment: one page after the buffer
TC       = BUFPG * 256
TH       = TC + 256
TD       = TC + 512

T2CL     = $1808
T2CH     = $1809
VIA1PA   = $1801
VIA2PB   = $1C00
VIA2PA   = $1C01
VIA2DDRA = $1C03
PCR2     = $1C0C
WD       = $2000
WD_TRK   = $2001
WD_DAT   = $2003

PB_STEP  = $03
PB_KEEP  = $93                  ; phase, write enable, SYNC
PB_WE    = $10
PA_SIDE  = $04
PCR_SOE_MASK = $F1
PCR_SOE_OFF  = $0C
PCR_READ_SOE = $EE              ; CB2 high (read), CA2 high (SOE)
PCR_WRITE_MASK = $11
PCR_WRITE_SOE = $CE             ; CB2 low (write), CA2 high (SOE)
WD_FORCE_INT = $D0
WD_SEEK  = $18                  ; h = 1 (no spin-up wait), no verify (DOS diskin)
WD_INDEX = $02

ST_NOSYNC   = $01               ; no matching sync before the start timeout
ST_NOINDEX  = $02               ; no index pulse, or a 1541
ST_KILLER   = $04               ; a sync outlasted the wait budget, or held
ST_WPROT    = $08               ; write protected, nothing written
ST_TIMEOUT  = $10               ; byte ready stopped
ST_NOANCHOR = $20               ; the anchor never passed
ST_FULL     = $40               ; TS table full

TB_CHAIN  = 12
MATCH_TMO = 80
WEND_TMO = 8                    ; x 7 cycles: over a byte period at zone 0                  ; x 256 mismatched bytes and waits: over 2 rev
ANCHOR_MAX = 8
INDEX_TMO = 120                 ; x 256 x 16 cycles: over 2 rev, per edge
WD_SETTLE = 8                   ; x 5 cycles before trusting WD1770 status
WD_IDLE_TMO = 24                ; x 256 x 11 cycles waiting for busy to clear

ZP      = $60
pbset   = ZP + 0                ; motor, LED and density bits for $1C00
side    = ZP + 1                ; VIA1 PA2 value (1571)
steps   = ZP + 2                ; signed halftracks to step
stepms  = ZP + 3
settle  = ZP + 4
mode    = ZP + 5
npages  = ZP + 6
kind    = ZP + 7
alen    = ZP + 8
anchor  = ZP + 9                ; ANCHOR_MAX bytes
status  = ZP + 17
endpg   = ZP + 18               ; pages left when a pass ended
endy    = ZP + 19               ; Y then: bytes into the page, or TS entries
tfirst  = ZP + 20               ; T2 lo, hi when read was entered
tlast   = ZP + 22               ; T2 lo, hi at the end of a pass
idx1    = ZP + 24               ; T2 lo, hi at two index edges (mode 2)
idx2    = ZP + 26
cntl    = ZP + 28               ; TS byte count
cnth    = ZP + 29
npg     = ZP + 30
tmo     = ZP + 31
tmp     = ZP + 32
wdst    = ZP + 33               ; 1571: WD status at the end of prep
ZP_SIZE = 34
.assert anchor + ANCHOR_MAX = status, error, "anchor overlaps results"

; A branch whose cycle count is part of a timing table: no page crossing.
.macro BR op, target
        op target
        .assert >* = >(target), error, "timed branch crosses a page"
.endmacro

; Wait for byte ready (V) with the bounded counter in reg (x or y).
.macro VWAIT ok, fail, reg
        .local w
w:      BR bvs, ok
.if .xmatch(reg, x)
        dex
.else
        dey
.endif
        BR bvs, ok
        BR bne, w
        BR bvs, ok
        dec tmo
        BR bvs, ok
        BR bne, w
        jmp fail
.endmacro

; Wait A milliseconds, 1000 cycles each at 1 MHz.
.macro DELAY_ROUTINE
delay:  tay
        beq dd
d1:     ldx #198
d2:     dex
        bne d2
        nop
        nop
        dey
        bne d1
dd:     rts
.endmacro

        .include "wdtest.inc"

        .segment "CODE"
        .org $0300                      ; the Makefile's TRACK_BASE

        jmp prep
.if !SEEK
        jmp read


; Start mode 3: wait for the anchor, abandoning the pass if it never comes.
anchor3:
        lda mode
        cmp #3
        bne mdone
        lda #MATCH_TMO
        sta tmo
        jsr match
        bcc mdone
        pla
        pla
        jmp noanchor

; Consume bytes until the last alen of them equal anchor; C set on timeout.
; X and Y clobbered.
match:  ldx #0
mw:     VWAIT mg, mfail, y
mg:     clv
        lda VIA2PA
        cmp anchor,x
        BR bne, mx
        inx
        cpx alen
        BR bne, mw
        clc
mdone:  rts
mx:     ldx #0
        dey
        BR bne, mw
        dec tmo
        BR bne, mw
mfail:  sec
        rts

.endif

soeoff: lda PCR2
        and #PCR_SOE_MASK
        ora #PCR_SOE_OFF
        sta PCR2
        lda status
        rts

prep:   lda VIA2PB
        and #PB_KEEP
        ora pbset
        sta VIA2PB
.if MODEL = 1571
        lda VIA1PA
        and #<~PA_SIDE
        ora side
        sta VIA1PA
.endif
        lda #0
        sta status
        ldx steps
        beq :++
stp:    txa
        asl                     ; C = direction (1 outward)
        lda #$01
        bcc :+
        lda #$FF
:       pha
        clc
        adc VIA2PB
        eor VIA2PB
        and #PB_STEP
        eor VIA2PB
        sta VIA2PB
        lda stepms
        jsr delay
        pla
        eor #$FF                ; steps -= direction
        sec
        adc steps
        sta steps
        tax
        bne stp
        lda settle
        jsr delay
:
.if MODEL = 1571
        jsr wdt1
        sta wdst
.endif
        lda status
        rts

.if !SEEK
; Wait until SYNC (PB7) equals bit 7 of A; C set on timeout.
pb7:    sta tmp
:       lda VIA2PB
        eor tmp
        bpl vok
        dex
        bne :-
        dec tmo
        bne :-
        sec
        rts

vok:    clc
        rts

.if MODEL = 1571
; Leading edge of the index pulse (prep left type I status); C set on
; timeout. Preserves X.
index:  lda #0
ip:     sta tmp
        lda #INDEX_TMO
        sta tmo
        WDTEST
iw:     lda WD
        and #WD_INDEX
        cmp tmp
        beq ig
        dey
        bne iw
        dec tmo
        bne iw
        sec
        rts
ig:     eor #WD_INDEX
        bne ip
        clc
        rts
.endif

noanchor:
        lda #ST_NOANCHOR
        bne fail
timeout:
        lda #ST_KILLER          ; byte ready stopped: SYNC held, or not
        bit VIA2PB
        bpl fail
        lda #ST_TIMEOUT
fail:   ora status
        sta status
finish: sty endy
        lda npg
        sta endpg
        lda T2CL
        ldx T2CH
        sta tlast
        stx tlast + 1
        jmp soeoff

read:   lda #0
        sta status
        sta cntl
        sta cnth
        sta tmo
        tay
        lda #BUFPG
        sta bst + 2
        sta tst + 2
        lda npages
        sta npg
        lda PCR2
        ora #PCR_READ_SOE
        sta PCR2
        lda T2CL
        ldx T2CH
        sta tfirst
        stx tfirst + 1
        lda mode
        bne :+
        clv                     ; V only from bytes after this, all read
        beq go
:       cmp #2
        beq idxm
        bcs go
ws:     lda #0
        jsr pb7
        lda #ST_NOSYNC
        bcs fail
        clv
        bcc go
idxm:
.if MODEL = 1571
        ldx #0
:       jsr index
        bcs noidx
        lda T2CL
        ldy T2CH
        sta idx1,x
        sty idx1 + 1,x
        inx
        inx
        cpx #4
        bne :-
        ldy #0
        clv
        beq go
noidx:  ldy #0
.endif
        lda #ST_NOINDEX
        sta status
go:     lda kind
        bne :+
        jmp bw
:       lsr
        bcc :+
        jmp tb
:       jmp ts

; TS last in CODE so its timed branches stay in one page.
ts:     jsr anchor3
        ldy #0
tspoll:    BR bvs, tsbyte
        lda VIA2PB
        BR bpl, tsync
        dex
        BR bne, tspoll
        dec tmo
        BR bne, tspoll
        jmp timeout
tsbyte:    clv
        inc cntl
        BR bne, tspoll
        inc cnth
        lda cnth
        cmp npages
        BR bne, tspoll
        jmp finish
tsync:    BR bvs, tsbyte
        ldx #0
tswait:    lda VIA2PB
        BR bmi, tsrel
        dex
        BR bne, tswait
        lda cntl
        sta TC,y
        lda cnth
        sta TH,y
        txa
        sta TD,y
        iny
        beq full
        dec tmo
        bne tswait
        lda #ST_KILLER
        jmp fail
tsrel:  txa
        sta TD,y
        lda cntl
        sta TC,y
        lda cnth
        sta TH,y
        iny
        BR bne, tspoll
full:   lda #ST_FULL
        jmp fail

.endif

.if MODEL = 1571
; Type I status, the index live in bit 1 (see the header): A = the status,
; bit 0 (busy) set if busy never cleared (a command written while busy is
; not accepted). X and Y clobbered.
wdt1:   lda #WD_FORCE_INT
        jsr wdcmd
        WDTEST
        lda WD_TRK
        WDTEST
        sta WD_DAT
        lda #WD_SEEK
; Command A, then A = the status once busy reads clear, or after 256 x
; WD_IDLE_TMO polls of 11 cycles.
wdcmd:  WDTEST
        sta WD
        ldy #WD_SETTLE
:       dey
        bne :-
        ldx #WD_IDLE_TMO
        lda #1
        WDTEST
wdp:    bit WD
        beq wdr
        dey
        bne wdp
        dex
        bne wdp
wdr:    WDTEST
        lda WD
        rts
.endif

.if SEEK
        DELAY_ROUTINE
.else
        .reloc
        .segment "PASS"
write:  lda #ST_WPROT
        sta status
        lda VIA2PB
        and #PB_WE
        beq wret
        lda #BUFPG
        sta ld + 2
        lda npages
        sta npg
        lda #ST_NOINDEX
        sta status
        lda mode
        cmp #2
        bne wgo
.if MODEL = 1571
        jsr index
        bcc wgo
.endif
wret:   lda status
        rts
wgo:    lda #0
        sta status
        sta tmo
        tay
        lda PCR2
        and #PCR_WRITE_MASK
        ora #PCR_WRITE_SOE
        sta PCR2
        lda #$FF
        sta VIA2DDRA
        clv
wl:     VWAIT wg, wto, x
wg:     clv
ld:     lda $FF00,y
        sta VIA2PA
        iny
        BR bne, wl
        inc ld + 2
        dec npg
        BR bne, wl
        ldy #2                  ; last byte loaded, then shifted out
wfin:   ldx #WEND_TMO
:       BR bvs, wnext
        dex
        BR bne, :-
        beq wto
wnext:  dey
        BR beq, wend
        clv
        BR bne, wfin
wto:    lda #ST_TIMEOUT
        sta status
wend:   lda PCR2
        ora #PCR_READ_SOE
        sta PCR2
        lda #0
        sta VIA2DDRA
        jmp soeoff


        DELAY_ROUTINE

bw:     VWAIT bg, timeout, x
bg:     clv
        lda VIA2PA
bst:    sta $FF00,y
        iny
        BR bne, bw
        inc bst + 2
        dec npg
        BR bne, bw
        jmp finish

tb:     jsr anchor3
        ldy #0
tw:
.repeat TB_CHAIN
        BR bvs, tg
.endrepeat
        ldx #0
tl:     BR bvs, tg
        dex
        BR bvs, tg
        BR beq, tov
        BR bvc, tl
tg:     clv
        lda T2CL
tst:    sta $FF00,y
        iny
        BR bne, tw
        inc tst + 2
        dec npg
        BR bne, tw
        jmp finish
tov:    BR bvs, tg
        dec tmo
        BR bvs, tg
        BR bne, tl
        jmp timeout
.endif
