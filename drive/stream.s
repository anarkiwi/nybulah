;Streaming capture for a 1571 at 2 MHz under monitor_s4 (xum1541 firmware
; v12), loaded at $0300 in place of the seek build of track.s (whose prep
; placed the head) and called there through J; parameters in track.s's zero
; page block. Each byte ready goes out through the CIA shift register with no
; host handshake; metadata goes out the same way with CLK asserted at its
; bit 7, where the adapter samples it. Values (never $00):
;
;   %tttttt01  SYNC_START  t = T2 bits 7-2 when SYNC was seen low; written
;                          from nw, so exactly the bytes before it precede it
;   %tttttt10  SYNC_CONT   t = T2 bits 7-2; sync timestamps < 256 cycles apart
;   %tttttt11  SYNC_END    t = T2 bits 7-2 when SYNC was seen high again;
;                          may follow later bytes and metadata, never another
;                          SYNC_END, so it ends the oldest open sync
;   $04 START  $08 INDEX (rising edge)  $40 END  $44 END_NOINDEX  $48 END_ATN
;
; Parameters: revs (stop at that rising index edge), ticks (VIA1 T1 periods
; without an index edge before END_NOINDEX). Returns A = the END value sent,
; or ST_SLOW (not at 2 MHz) or ST_NOGO (no host go) without streaming.
;
; Rules, t = 0 at an SDR write (protocol.md derives them):
; - a write follows the previous one by SR_PERIOD (40) or more cycles, or by
;   the shifter's ICR flag: the shifter is idle;
; - a waiting byte is read at the first V sample after a write and held in X
;   until its write; X and the VIA latch are the only buffers;
; - data goes first: metadata waits for an idle shifter and no waiting byte,
;   except SYNC_START and SYNC_CONT, which no byte can be waiting for;
; - CLK changes 14 or more cycles after a write and 2 or more before the
;   next write: the adapter samples it 4 to 14 cycles after a write.
;
;   nw   V at 0 and 6 of 11, SYNC at 5; entered 28+ after a write; writes 12
;        cycles after the bvs that sees V.
;   pwo  after a write from nw: due metadata at 40 unless V by 22, the
;        index, every 256th byte ATN and T1; else nw at 29.
;   pwb  after a timed write: V at 1 -> read, write at 43; due metadata at
;        42 unless V by 24; V at 28 -> write at 43; else nw at 33.
;   mpw  after metadata (entered at 4): V at 4 -> read, CLK released at 18,
;        write at 46; else CLK released at 22, V at 28 -> write at 43, else
;        nw at 33.

        .setcpu "6502"

; A branch whose cycle count is part of a timing table: no page crossing.
.macro BR op, target
        op target
        .assert >* = >(target), error, "timed branch crosses a page"
.endmacro

CIA_SDR  = $400C
CIA_ICR  = $400D
CIA_CRA  = $400E
IEC      = $1800
T1CL     = $1804
IFR1     = $180D
VIA1PA_NH = $180F
CLK_IN   = $04
CLK_OUT  = $08
IRQ_T1   = $40
PA_FSDIR = $02
PA_2MHZ  = $20
CRA_START = $01
CRA_SPOUT = $40
ICR_SP   = $08

M_SSTART = $01
M_SCONT  = $02
M_SEND   = $03
M_START  = $04
M_INDEX  = $08
M_END    = $40
END_NOINDEX = $44
END_ATN  = $48

ST_SLOW  = $10
ST_NOGO  = $11

SR_PERIOD = 40
SYNC_POLLS = 3                  ; SYNC polls per run, 11 cycles apart

VIA2PB   = $1C00
VIA2PA   = $1C01
PCR2     = $1C0C
T2CL     = $1808
WD       = $2000
WD_FORCE_INT = $D0
WD_INDEX = $02
WD_SETTLE = 16                  ; x 5 cycles before trusting WD1770 status
PCR_SOE_MASK = $F1
PCR_SOE_OFF  = $0C
PCR_READ_SOE = $EE

ZP      = $60
ticks   = ZP + 5
revs    = ZP + 6
tmo     = ZP + 31
ilev    = ZP + 18
cnt     = ZP + 19
pm      = ZP + 20                       ; due metadata, then a second
pm2     = ZP + 21
tse     = ZP + 22

        .segment "CODE"

        jmp stream

        .segment "ZPCODE"

stream: lda VIA1PA_NH
        and #PA_2MHZ
        bne :+
        lda #ST_SLOW
        rts
:       lda #WD_FORCE_INT
        sta WD
        ldy #WD_SETTLE
:       dey
        bne :-
        sty pm                          ; Y = 0
        sty pm2
        lda WD
        and #WD_INDEX
        sta ilev
        lda PCR2
        ora #PCR_READ_SOE
        sta PCR2
        lda ticks
        sta tmo
gw:     lda IEC                         ; host go: CLK
        and #CLK_IN
        bne :+
        jsr tick
        bne gw
        lda #ST_NOGO
        jmp done
:       lda VIA1PA_NH
        ora #PA_FSDIR
        sta VIA1PA_NH
        lda #CRA_START | CRA_SPOUT
        sta CIA_CRA
        lda #M_START
        jsr meta
        lda ticks
        sta tmo
        clv
        jmp nw

        .segment "CODE"

; Byte ready (or SYNC low); entered 28 or more cycles after a write.
nw:     BR bvs, nv
        lda VIA2PB
        BR bvs, nv
        BR bmi, nw
        jmp ss
nv:     ldx VIA2PA
        clv
        stx CIA_SDR                     ; t = 0
pwo:    lda pm
        BR bne, pwm                     ; due metadata
        lda WD
        eor ilev
        and #WD_INDEX
        BR bne, pwe
        dec cnt
        BR beq, pws
        nop
        jmp nw                          ; 29

; Due metadata at 40 (42 from pwb), CLK at 17; a byte arriving by 22 goes
; first (ee) and the metadata stays due.
pwm:    ldy pm
        lda #CLK_OUT
        BR bvs, ee                      ; t = 12
        sta IEC                         ; t = 17
        BR bvs, ee                      ; t = 18
        nop
        BR bvs, ee                      ; t = 22
        nop
        lda pm2
        sta pm
        lda #0
        sta pm2
        sty CIA_SDR                     ; t = 40
        jmp mpw

pwe:    jsr edge
        jmp nw

; Every 256 bytes from nw: ATN and the no-index timeout.
pws:    lda IEC
        bpl :+
        jmp abort
:       jsr tick
        bne nw
        jmp lost

; Write of the byte in X, 43 cycles after the previous write.
tv:     stx CIA_SDR                     ; t = 0
pwb:    BR bvs, ee                      ; t = 1
        lda pm
        BR bne, pwm
        ldy #3
:       dey
        BR bne, :-
        jmp *+3                         ; 3 cycles, V untouched
        BR bvs, bl                      ; t = 28
        jmp nw                          ; 33
bl:     ldx VIA2PA
        clv
        jmp tv                          ; 43

; After metadata (entered at 4): the waiting byte, CLK released, as pwb.
mpw:    BR bvs, ee                      ; t = 4
        lda #0
        ldy #2
:       dey
        BR bne, :-
        sta IEC                         ; t = 22
        nop
        jmp *+3                         ; 3 cycles, V untouched
        BR bvs, bl                      ; t = 28
        jmp nw                          ; 33
; A byte already waiting after a write (pwb at 1, mpw at 4, pwm at 12-24):
; read it now, release CLK 11 cycles after the bvs and write it 42 later.
ee:     ldx VIA2PA
        clv
        lda #0
        sta IEC
        ldy #4
:       dey
        BR bne, :-
        jmp tv

; SYNC low, from nw (no byte waiting, shifter idle): SYNC_START; SYNC polled
; every 11 cycles until high, with T1, the index and SYNC_CONT whenever the
; last metadata byte is out; due metadata waits for the bytes after the sync.
; CLK stays asserted.
ss:     lda T2CL
        and #$FC
        ora #M_SSTART
        sta tse
        lda #CLK_OUT
        sta IEC
        lda CIA_ICR                     ; the flag a data byte left
        lda tse
        sta CIA_SDR                     ; t = 0
sp:     ldy #SYNC_POLLS
:       lda VIA2PB
        bmi se
        dey
        BR bne, :-
        lda IFR1
        and #IRQ_T1
        beq :+
        lda T1CL
        dec tmo
        bne :+
        jmp lost
:       lda VIA2PB
        bmi se
        lda WD
        eor ilev
        and #WD_INDEX
        beq :+
        jsr edge                        ; INDEX due
:       lda VIA2PB
        bmi se
        lda CIA_ICR
        and #ICR_SP
        beq sp                          ; last metadata byte still going out
        lda T2CL
        and #$FC
        ora #M_SCONT
        tax
        lda VIA2PB
        bmi sxi                         ; the ICR read took the flag
        stx CIA_SDR                     ; t = 0
        jmp sp

; SYNC high: stamp, then wait for the shifter, reading a byte that arrives
; meanwhile. A byte (held or waiting) goes first and SYNC_END becomes due, as
; it does behind due metadata, so SYNC_ENDs keep their order.
sxi:    lda T2CL                        ; the shifter is idle
        sta tse
        ldy #0
        bvc sx
        ldx VIA2PA                      ; the first byte after the sync
        clv
        iny
        bne sx
se:     lda T2CL                        ; 7 (8) cycles after the SYNC read
        sta tse
        ldy #0
sw:     bvc :+
        ldx VIA2PA                      ; the first byte after the sync
        clv
        iny
:       lda CIA_ICR
        and #ICR_SP
        beq sw
sx:     lda tse
        and #$FC
        ora #M_SEND
        dey
        beq sxd                         ; a byte held
        bvs sxh                         ; a byte waiting
        ldx pm
        bne sxm                         ; due metadata first
        sta CIA_SDR                     ; t = 0
        jmp mpw
sxh:    ldx VIA2PA
        clv
sxd:    jsr due
        lda #0
        sta IEC
        jmp tv
sxm:    jsr due
        jmp mpw

; Index level changed (A = the change): toggle it; on a rising edge make INDEX
; due, restart the no-index timeout and end at the last revolution.
edge:   eor ilev
        sta ilev
        beq :+
        lda #M_INDEX
        jsr due
        lda ticks
        sta tmo
        dec revs
        bne :+
        pla
        pla
        jmp last
:       rts

; Metadata A due, behind any already due; X kept.
due:    ldy pm
        bne :+
        sta pm
        rts
:       sta pm2
        rts

; Z set when a VIA1 T1 period passed and tmo ran out.
tick:   lda IFR1
        and #IRQ_T1
        beq :+
        lda T1CL
        dec tmo
        rts
:       lda #1
        rts

abort:  lda #END_ATN
        bne end
lost:   lda #END_NOINDEX
        bne end
last:   lda #M_INDEX
        jsr meta
        lda #M_END
end:    jsr meta
        lda CIA_ICR
:       lda CIA_ICR                     ; END out
        and #ICR_SP
        beq :-
        lda #CRA_START
        sta CIA_CRA
        lda VIA1PA_NH
        and #<~PA_FSDIR
        sta VIA1PA_NH
        lda tse
done:   tax
        lda PCR2
        and #PCR_SOE_MASK
        ora #PCR_SOE_OFF
        sta PCR2
        txa
        rts

; Metadata byte A 45 or more cycles after any write, CLK released at 16.
meta:   sta tse
        ldy #SR_PERIOD / 5 + 1
:       dey
        bne :-
        ldx #CLK_OUT
        stx IEC
        sta CIA_SDR                     ; t = 0
        ldy #2
:       dey
        bne :-
        lda #0
        sta IEC                         ; t = 16
        rts
