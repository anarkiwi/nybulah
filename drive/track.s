; Raw track capture and write for the 1541 and 1571, called through the
; monitor's J command. Assembled per model (-D MODEL=1541 or 1571), which
; fixes the expansion RAM buffer (BUFPG, NPAGES pages) and the sync table
; page (TABPG); linked at the base given to ld65 -S. The host fills the
; parameter block, calls an entry point and reads results back from the
; block. Every entry returns the status byte in A.
;
;   base+0  prep   motor/LED/density bits, 1571 side, step, settle
;   base+3  read   capture npages pages into the buffer
;   base+6  write  write npages pages from the buffer, then read mode
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
; host) and keeps counting after it expires; timestamps are T2 values,
; 16-bit ones read low byte then high byte 4 cycles later. VIA1 timer 1 is the monitor's watchdog and is not touched.
; 1571: VIA1 PA2 side, PA5 2 MHz; WD1770 status at $2000, bit 1 index once
; Force Interrupt $D0 has been written to the command register.
;
; Sync table at TABPG, TAB_N entries per array, arrays TAB_STRIDE apart:
;   TSL       T2 low byte when SYNC was seen asserted
; Column TAB_N is a spare written once the table is full (lost counts the
; syncs it absorbed); CNT of entry TAB_N-1 is then unreliable.
;   PL, PH    bytes stored before the sync: low byte, pages left
;   TEL       T2 low byte when SYNC was seen released
;   CNT       Y after the release wait; (CNT - PL) mod 256 counts
;             SYNC_LOOP-cycle iterations
;
; Read loop, cycles at 1 MHz:
;   poll  bvs byte     2 / 3      byte  clv          2
;         lda VIA2PB   4                lda VIA2PA   4   latch read
;         bmi poll     3 / 2      st    sta abs,y    5
;                                       iny          2
;                                       bne poll     3 / 2
;                                       inc st+2     6   page change
;                                       dex          2
;                                       bne poll     3
; Poll period 9. Byte k ready just after a bvs: its latch is read 18 cycles
; later, poll resumes after 27 (37 with a page change) and byte k+1 is read
; by 46. Zone 3 at 300 rpm leaves 26 cycles per byte, 52 for two.
;
; Sync handler, from the PB7 read that saw SYNC: T2 read 11 cycles later.
; The release wait reads PB7 at gaps of 6 and 11 cycles. From the PB7 read
; that saw the release, T2 is read 7 cycles later and the byte loop's latch
; read follows within 30 cycles of that PB7 read when the first byte after
; the sync is already waiting.

        .setcpu "6502"

.if MODEL = 1541
BUFPG    = $80
.elseif MODEL = 1571
BUFPG    = $60
.else
        .error "MODEL must be 1541 or 1571"
.endif
NPAGES   = 31
TABPG    = BUFPG + NPAGES

T2CL     = $1808
T2CH     = $1809
VIA1PA   = $1801
VIA2PB   = $1C00
VIA2PA   = $1C01
VIA2DDRA = $1C03
PCR2     = $1C0C
WD       = $2000

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
WD_INDEX = $02

ST_NOSYNC = $01                 ; no matching sync before the start timeout
ST_NOINDEX = $02                ; no index pulse, or a 1541
ST_KILLER = $04                 ; a sync outlasted the release timeout
ST_WPROT  = $08                 ; write protected, nothing written

TAB_N = 50
TAB_STRIDE = 51
TSL = TABPG * 256
PL  = TSL + TAB_STRIDE
PH  = PL + TAB_STRIDE
TEL = PH + TAB_STRIDE
CNT = TEL + TAB_STRIDE
.assert 5 * TAB_STRIDE <= 256, error, "sync table exceeds a page"

SYNC_LOOP = 17                  ; cycles per release-wait iteration
SYNC_TMO  = 64                  ; x 256 x SYNC_LOOP cycles: over 1 rev
WAIT_TMO  = 160                 ; x 256 x 11 cycles: 2 rev
INDEX_TMO = 120                 ; x 256 x 16 cycles: over 2 rev, per edge
WD_SETTLE = 8                   ; x 5 cycles before trusting WD1770 status

ZP      = $60
pbset   = ZP + 0                ; motor, LED and density bits for $1C00
side    = ZP + 1                ; VIA1 PA2 value (1571)
steps   = ZP + 2                ; signed halftracks to step
stepms  = ZP + 3
settle  = ZP + 4
mode    = ZP + 5                ; 0 now, 1 after a sync, 2 at index (1571)
npages  = ZP + 6
marker  = ZP + 7                ; mode 1: first byte after the sync,
mmask   = ZP + 8                ; compared under this mask
status  = ZP + 9
nsync   = ZP + 10
lost    = ZP + 11               ; syncs not recorded, table full
endpg   = ZP + 12               ; pages left when a read ended
endy    = ZP + 13
tfirst  = ZP + 14               ; T2 lo, hi when read was entered
tlast   = ZP + 16               ; T2 lo, hi at the end of a read
idx1    = ZP + 18               ; T2 lo, hi at two index edges (mode 2)
idx2    = ZP + 20
zpage   = ZP + 22
zidx    = ZP + 23
zc      = ZP + 24
ztmo    = ZP + 25
tmp     = ZP + 26
ZP_SIZE = 27

        .segment "CODE"

        jmp prep
        jmp read

write:  lda #ST_WPROT
        sta status
        lda VIA2PB
        and #PB_WE
        beq wret
        lda #BUFPG
        sta ld + 2
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
        ldx npages
        ldy #0
        lda PCR2
        and #PCR_WRITE_MASK
        ora #PCR_WRITE_SOE
        sta PCR2
        lda #$FF
        sta VIA2DDRA
        clv
wl:     bvc wl
        clv
ld:     lda $FF00,y
        sta VIA2PA
        iny
        bne wl
        inc ld + 2
        dex
        bne wl
:       bvc :-                  ; last byte loaded into the shift register
        clv
:       bvc :-                  ; last byte shifted out
        lda PCR2
        ora #PCR_READ_SOE
        sta PCR2
        lda #0
        sta VIA2DDRA
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
:       lda status
        rts

; Wait A milliseconds, 1000 cycles each at 1 MHz.
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

.if MODEL = 1571
; Leading edge of the index pulse; C set on timeout. Preserves X.
index:  lda #WD_FORCE_INT
        sta WD
        ldy #WD_SETTLE
:       dey
        bne :-
        tya
ip:     sta tmp
        lda #INDEX_TMO
        sta ztmo
iw:     lda WD
        and #WD_INDEX
        cmp tmp
        beq ig
        dey
        bne iw
        dec ztmo
        bne iw
        sec
        rts
ig:     eor #WD_INDEX
        bne ip
        clc
        rts
.endif

read:   lda #0
        sta status
        sta lost
        sta zidx
        sta zc
        lda #BUFPG
        sta st + 2
        lda PCR2
        ora #PCR_READ_SOE
        sta PCR2
        lda T2CL
        ldx T2CH
        sta tfirst
        stx tfirst + 1
        lda mode
        beq go0
        cmp #1
        bne idxm
        lda #WAIT_TMO
        sta ztmo
ws:     lda VIA2PB
        bpl wsend
        dey
        bne ws
        dec ztmo
        bne ws
        lda #ST_NOSYNC
        bne rdst
wsend:  lda VIA2PB
        bpl wsend
        clv
:       bvc :-
        clv
        lda VIA2PA
        sta BUFPG * 256
        eor marker
        and mmask
        bne ws
        ldy #1
        ldx npages
        bne poll
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
        beq go0
noidx:
.endif
        lda #ST_NOINDEX
rdst:   sta status
go0:    clv
        ldy #0
        ldx npages

poll:   bvs byte
        lda VIA2PB
        bmi poll
sync:   bvs byte
        stx zpage
        lda T2CL
        ldx zidx
        cpx #TAB_N
        bcc :+
        inc lost                ; full: record into the spare column
        dec zidx
:       sta TSL,x
        lda zc
        sta CNT - 1,x
        tya
        sta PL,x
        lda zpage
        sta PH,x
        inc zidx
        lda #SYNC_TMO
        sta ztmo
sw:     lda VIA2PB
        bmi send
        lda VIA2PB
        bmi send
        iny
        bne sw
        dec ztmo
        bne sw
        lda T2CL
        sta TEL,x
        sty zc
        ldy PL,x
        ldx zpage
        lda #ST_KILLER
        ora status
        sta status
        bne finish
send:   lda T2CL
        sta TEL,x
        sty zc
        ldy PL,x
        ldx zpage
wF:     bvc wF
byte:   clv
        lda VIA2PA
st:     sta $FF00,y
        iny
        bne poll
        inc st + 2
        dex
        bne poll

finish: stx endpg
        sty endy
        lda T2CL
        ldx T2CH
        sta tlast
        stx tlast + 1
        ldx zidx
        stx nsync
        lda zc
        sta CNT - 1,x
        jmp soeoff
