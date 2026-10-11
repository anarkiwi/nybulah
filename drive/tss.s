; 1571 sync sensor at 2 MHz: SYNC (VIA2 PB7) read every 6 to 13 cycles,
; each sync recorded with VIA1 T2 stamps and the bytes counted (byte ready
; sets V) since the previous record; rising index edges recorded too.
; Loaded at $0300 over the seek build of track.s (whose prep placed the head
; and left the WD1770 in type I status, the index live in bit 1) and called
; there through J. Parameters and state live in zero page at ZP (inside the
; range the host saves around a session). Records of RECLEN bytes go to
; expansion RAM from REC up to page rend; returns A = status.
;
;   0    kind  1..4 short: SYNC seen high at that poll of the seen sequence
;              5 long: a = start stamp, b = end stamp
;              6 index: a = stamp after the WD1770 read that saw the edge
;   1-2  a     T2 lo, hi (hi read some cycles after lo, per kind: host tables)
;   3-4  b
;   5-6  cnt   bytes counted before this record since the previous one
;   7          unused
;
; Cycles (2 MHz), R = the read that saw SYNC low; the seen sequence polls
; again at R+7, R+17, R+26 and R+36, reading the start stamp lo at R+13 and
; hi at R+32, then the release loop polls 6 apart (11 at its turn) from
; R+50. A short kind k reads T2 lo 14 (kind 1) or 13 cycles after R and hi
; 12, 19, 28 or 19 cycles after lo (19 for the long kind too); the long kind reads the end lo 7 cycles
; after the poll that saw SYNC high and hi 4 after lo. Between seen
; sequences SYNC is read at most 13 cycles apart (the byte path), 11 at the
; idle loop's turn and 6 or 8 otherwise (33 once at each index level change
; and not at all while a record is stored), so a two-cell pulse (12.6 cycles at
; zone 3, 310 rpm) is always seen; a one-cell pulse is seen when a read
; falls inside it. V is sampled at most 26 cycles apart in the idle loop;
; a byte that landed before a sync is counted right after the sync's
; stamps, and from the first byte after a sync (7 cells after its end) on,
; samples come at most 45 cycles apart and clear V within 5, so no byte
; ready merges with the next at a byte period of 50 cycles or more; no
; instruction between two samples touches V (adc, sbc, bit and plp would)
; and every count is followed by a data port read. The two syncs a tail separates are
; a byte or more apart: a sync starting within the record store of the
; previous one is not seen. The index is polled every eighth byte, at most
; 0.4 ms apart.

        .setcpu "6502"

; A branch whose cycle count is part of a timing table: no page crossing.
.macro BR op, target
        op target
        .assert >* = >(target), error, "timed branch crosses a page"
.endmacro

VIA1PA   = $1801
T2CL     = $1808
T2CH     = $1809
VIA2PB   = $1C00
VIA2PA   = $1C01
PCR2     = $1C0C
WD       = $2000
WD_INDEX = $02
PA_2MHZ  = $20
PCR_SOE_MASK = $F1
PCR_SOE_OFF  = $0C
PCR_READ_SOE = $EE
REC      = $6000

ST_KILLER  = $04
ST_TIMEOUT = $10
ST_FULL    = $40
ST_SLOW    = $80

K_LONG   = 5
K_INDEX  = 6
RECLEN   = 8
POLLS    = 6                    ; SYNC reads per idle body, V every third
RPOLLS   = 2                    ; SYNC reads per release body

ZP      = $60
revs    = ZP + 0                ; rising index edges to end at (0: none)
tmo     = ZP + 1                ; wait budget, 256 bodies per unit (0: 256)
rend    = ZP + 2                ; page after the last record page
status  = ZP + 3
ri      = ZP + 4                ; next record: offset in page rpg
rpg     = ZP + 5
wdst    = ZP + 6                ; WD1770 status at entry
ilev    = ZP + 7                ; WD1770 status at the last index poll
cnth    = ZP + 8
tsl     = ZP + 9
tsh     = ZP + 10
kind    = ZP + 11
teh     = ZP + 12
ZP_SIZE = 13

        .include "wdtest.inc"

        .segment "CODE"
        .org $0300

start:  lda VIA1PA
        and #PA_2MHZ
        bne :+
        lda #ST_SLOW
        rts
:       lda #0
        sta status
        sta cnth
        sta ri
        lda #>REC
        sta rpg
        jsr setpg
        WDTEST
        lda WD
        sta wdst
        sta ilev
        lda PCR2
        ora #PCR_READ_SOE
        sta PCR2
        ldy #0
        ldx #0
        clv
        jmp pl

; Byte ready: counted and the data port read (without the read the 1571 at
; 2 MHz raises no byte ready for the next byte after a sync), then the index
; every eighth byte.
byte:   iny
        clv
        lda VIA2PB
        BR bpl, seen
        cpx VIA2PA              ; the port read re-arms byte ready
        lda VIA2PB
        BR bpl, seen
        tya
        BR beq, wrap
        lda VIA2PB
        BR bpl, seen
        tya
        and #7
        BR bne, pl
ipoll:  lda VIA2PB
        BR bpl, seen
        WDTEST
        ldx WD
        lda VIA2PB
        BR bpl, seen
        cpx ilev
        BR beq, :+
        jmp edge
:       lda VIA2PB
        BR bpl, seen
        BR bvs, byte            ; within 45 cycles of the byte path's clv
        ldx #0
        jmp pl
wrap:   inc cnth
        jmp ipoll

; Idle loop: SYNC every 6 cycles, V every third read; X counts bodies.
pl:
.repeat POLLS / 3
        lda VIA2PB
        BR bpl, seen
        BR bvs, byte
        lda VIA2PB
        BR bpl, seen
        lda VIA2PB
        BR bpl, seen
.endrepeat
        dex
        BR bne, pl
        dec tmo
        BR bne, pl
        lda #ST_TIMEOUT
        jmp fail

; SYNC seen low at R.
seen:   lda VIA2PB              ; R+7
        BR bmi, r1
        ldx T2CL                ; R+13
        lda VIA2PB              ; R+17
        BR bmi, r2
        stx tsl
        lda VIA2PB              ; R+26
        BR bmi, r3
        ldx T2CH                ; R+32
        lda VIA2PB              ; R+36
        BR bmi, r4
        stx tsh
        ldx #0
        jmp rw

r1:     ldx T2CL                ; R+14
        lda #1
        jmp t1
r2:     lda #2
        jmp t1
r3:     lda #3
        jmp t1
r4:     stx tsh
        lda #4
        jmp t2

done:   lda #0
fail:   ora status
        sta status
        lda PCR2
        and #PCR_SOE_MASK
        ora #PCR_SOE_OFF
        sta PCR2
        lda status
        rts

; The release loop and the rest in the next page: no timed branch crosses.
        .res $0400 - *, $EA
rw:
.repeat RPOLLS
        lda VIA2PB              ; first at R+50
        BR bmi, rel
.endrepeat
        dex
        BR bne, rw
        dec tmo
        BR bne, rw
        lda #ST_KILLER
        jmp fail

rel:    lda T2CL                ; release poll + 7
        ldx T2CH                ; 4 after lo
        bvc rc
        iny                     ; a byte that landed before the sync
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
rc:     stx teh
        ldx ri
s3:     sta REC + 3, x
        lda teh
s4:     sta REC + 4, x
        lda #K_LONG
        bne tc
t1:     stx tsl
        ldx T2CH
        stx tsh
t2:     bvc tc
        iny                     ; a byte that landed before the sync
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
tc:     jsr put
        ldx #0
        jmp pl

; WD1770 status changed (X): a rising index edge is recorded. V is sampled
; on entry and after the stamps, within 45 cycles of the byte path's clv.
edge:   bvc :+
        iny
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
:       stx ilev
        txa
        and #WD_INDEX
        beq :++
        lda T2CL
        sta tsl
        lda T2CH                ; 7 after lo
        sta tsh
        bvc :+
        iny
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
:       lda #K_INDEX
        jsr put
        dec revs
        bne :+
        jmp done
:       ldx #0
        jmp pl

; Store the record of kind A with stamp a = tsl/tsh and the byte count,
; then restart the count; bytes landing meanwhile belong to the next record
; (V sampled at most 45 cycles apart from the count store on). A sync seen
; at the byte path's first reads leaves a wrap of Y uncounted: no sync
; follows a sync record within a byte, so the host reads a zero count after
; a sync record as 256.
put:    sta kind
        ldx ri
        tya
s5:     sta REC + 5, x
        lda cnth
s6:     sta REC + 6, x
        ldy #0
        sty cnth
        bvc :+
        iny
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
:       lda kind
s0:     sta REC, x
        lda tsl
s1:     sta REC + 1, x
        bvc :+
        iny
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
:       lda tsh
s2:     sta REC + 2, x
        txa                     ; next record: no adc, which would set V
        ora #RECLEN - 1
        tax
        inx
        stx ri
        bne :+
        jsr page
        bvc :+
        iny
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
:       rts

; Next record page; the last one ends the pass from inside put.
page:   inc rpg
        lda rpg
        cmp rend
        bcs full
        bvc setpg               ; V sampled inside the page turn too
        iny
        clv
        cpx VIA2PA              ; the port read re-arms byte ready
        jmp setpg
full:   pla
        pla
        pla
        pla
        lda #ST_FULL
        jmp fail
setpg:  lda rpg
        sta s0 + 2
        sta s1 + 2
        sta s2 + 2
        sta s3 + 2
        sta s4 + 2
        sta s5 + 2
        sta s6 + 2
        rts

