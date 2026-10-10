; 1581 streaming capture (xum1541 firmware v12 framing): runs a list of WD177x
; commands and sends every byte the WD delivers out through the 8520 shift
; register as it arrives, with no host handshake, plus metadata with CLK
; asserted at the byte's bit 7 (the adapter samples CLK 4 to 14 cycles after
; the SDR write). Loaded at $0300 (512 bytes) and $0790 (the rest) in place
; of mfm_1581 and called through J of monitor_s4_1581, the head placed.
;
; List L (the "NYMS" tag + 4): up to four entries of four bytes, op $FF
; ends it; TMO follows it:
;   op   WD command (Read Address $C8, Read Sector $88, Read Track $E8), or
;        0: wait for the next rising index edge (type I status IP)
;   trk  track register for the command (restored at the end)
;   sec  sector register
;   rep  bits 6-0: times to run the entry (1-127); bit 7: sector + 1 each
; TMO: timer B wraps (65536 us) a command or an index wait may take.
; Returns A = the end code, or ST_NOGO when the host never asserted CLK.
;
; Metadata (bit 7 clear, low bits 00, never $40-$4C outside the END family;
; then chunks %dddddd01 of 24 bit values, most significant first):
;   $04 START
;   $14 KEEP    queued when timer B bits 15-13 change while waiting
;   $0C REC     after a command, 14 bytes packed into 19 chunks: stamps
;               t_first and t_end (5 bytes each), WD status, flags (bit 0:
;               forced out by the timeout), data byte count (lo, hi)
;   $1C INDEX   the stamp (5 bytes, 7 chunks) of a rising index edge
; A stamp is timer B wraps, ICR bit 1 at the read, timer B high, low, high
; (nybulah.mfmstream.stamp_us turns it into microseconds).
;   $40 END   $44 END_TIMEOUT (the last REC was forced out)   $48 END_ATN
; A command's data bytes are contiguous in the stream; its REC counts them
; and follows them. Timer B low is read STAMP_LO cycles after the first
; byte's SDR write (t_first), the status read that saw the command end
; (t_end) or the index (INDEX); ICR STAMP_ICR cycles after it.
;
; Rules, t = 0 at an SDR write (protocol.md):
; - SDR writes 40 or more cycles apart: the 8520's shifter is idle (it flags
;   a byte done within 39 on the 1571's 6526; ciaprobe_1581 measures it);
; - a WD byte is read from the data register within 64 cycles of its DRQ
;   (one byte at 250 kbit/s), X carrying it to its write;
; - CLK asserted 2 or more cycles before a metadata write, released 14 or
;   more after it and 2 or more before a data write;
; - metadata goes out between commands with the WD idle (plain), or before
;   a command's first DRQ one byte at a time (send), the status read at
;   most 30 cycles apart throughout.
; ICR bit 1 is read for timer B wraps; ICR bit 3 follows every byte.

        .include "mfm.inc"

M_START  = $04
M_REC    = $0C
M_KEEP   = $14
M_INDEX  = $1C
M_END    = $40
END_TIMEOUT = $44
END_ATN  = $48
ST_NOGO  = $FF
F_TIMEOUT = $01
QMASK    = $3F
KEEP_MASK = $E0
PB_CLKIN = $04
STAMP_LO = 22                   ; write (or status read) -> timer B low read
STAMP_ICR = 4                   ; write (or status read) -> ICR read

qhead   = $30
keep    = $31
cnt     = $32                   ; data bytes of the command
tmo     = $34
wraps   = $35
qtail   = $36

        .org $0300

        jmp stream
        .byte "NYMS"
L:      .res 16
TMO:    .res 1
; ICR then timer B into base + 1 .. base + 4, 34 cycles: the ICR read at 4,
; the low byte at 22 (STAMP_ICR, STAMP_LO). base + 0 holds wraps, stored
; before.
.macro STAMP base
        lda CIA_ICR
        and #ICR_TB
        sta base + 1
        lda CIA_TBHI
        sta base + 2
        lda CIA_TBLO
        sta base + 3
        lda CIA_TBHI
        sta base + 4
.endmacro

; Command A; A = its first valid status (mfm.inc WDISSUE).
wdcmd:  WDISSUE
        rts

; Force interrupt; A = the status once idle (mfm.inc WDIDLE), Y clobbered.
force:  lda #WD_FORCE
        jsr wdcmd
        WDIDLE
        rts

; Z set when a wrap used up the timeout.
tick:   lda CIA_ICR
        and #ICR_TB
        beq :+
        inc wraps
        dec tmo
        rts
:       lda #1
        rts

; Send one queued byte, the WD idle: CLK from 4 before the write to 16
; after; returns 40 after it.
plain:  ldy qhead
        ldx q,y
        iny
        tya
        and #QMASK
        sta qhead
        lda #PB_CLK | PB_FSDIR
        sta CIA_PB                      ; t = -4
        stx CIA_SDR                     ; t = 0
        ldy #2
:       dey
        bne :-
        lda #PB_FSDIR
        sta CIA_PB                      ; t = 16
        ldy #3
:       dey
        bne :-
        rts                             ; t = 40

; Before the command's first DRQ: ATN, wraps, one queued byte or a
; keepalive at a time; the status read every 30 cycles or less.
        ALIGN4 1
w0:     WDOK
        lda WDSTAT                      ; 4     u = 4
        lsr                             ; 2
        and #ST_DRQ >> 1                ; 2     C: BUSY
        BR bne, dfirst                  ; 2
        BR bcc, wend                    ; 2     ended without data
        bit CIA_PB                      ; 4
        BR bmi, wabort                  ; 2
        lda CIA_ICR                     ; 4
        and #ICR_TB                     ; 2
        BR bne, wwrap                   ; 2
        WDOK
        lda WDSTAT                      ; 4     u = 30
        and #ST_DRQ                     ; 2
        BR bne, dfirst                  ; 2
        lda qhead                       ; 4
        cmp qtail                       ; 3
        BR bne, wsend                   ; 2
        lda CIA_TBHI                    ; 4
        and #KEEP_MASK                  ; 2
        cmp keep                        ; 4
        BR beq, w0                      ; 3     u = 56: the next read at 60
        sta keep
        lda #M_KEEP
        jsr put
        jmp w0
wsend:  jmp send
wwrap:  inc wraps
        dec tmo
        bne w0
        lda #0                          ; no data: the record spans the wait
        sta cnt
        jmp timeout
wabort: jmp abort

; The command ended with no data.
wend:   WDTEST
        lda WDSTAT
        sta stat
        lda wraps
        sta last
        STAMP last
        ldx #4
:       lda last,x
        sta first,x
        dex
        bpl :-
        lda #0
        sta cnt
        jmp record


; First byte: read at once, written 11 cycles later (w0 reads the status 39
; or more cycles after send's write, so this write is 40 or more after it),
; then stamped.
        ALIGN4 1
dfirst: WDOK
        ldx WDDAT                       ; 4
        lda wraps                       ; 3
        sta first                       ; 4
        stx CIA_SDR                     ; 4     t = 0
dstamp: STAMP first                     ; 34
        jmp dl                          ; 3     the status read at 41

; Data loop: from a write at t = 0 the status is read at 24 (28 when the
; count carries) and every 21 cycles while no DRQ; a DRQ seen (before BUSY:
; a command's last byte may wait after BUSY clears) is written 17 cycles
; after its status read: writes are 41 or more apart and a byte waits at most
; 21 + 13 cycles in the data register.
        ALIGN4 2
dl:     WDOK
        lda WDSTAT                      ; 4     t = 24
        lsr                             ; 2
        and #ST_DRQ >> 1                ; 2     C: BUSY
        BR bne, dd                      ; 3
        BR bcc, dend                    ; 2
        lda CIA_ICR                     ; 4
        and #ICR_TB                     ; 2
        BR beq, dl                      ; 3     status every 21 cycles
        inc wraps
        dec tmo
        bne dl
        jmp timeout
dd:     nop                             ; 2
        WDOK
        ldx WDDAT                       ; 4     t = 37
        stx CIA_SDR                     ; 4     t = 41 = 0
        inc cnt                         ; 5
        BR bne, :+                      ; 3 / 2
        inc cnt + 1                     ; 5
:       bit CIA_PB                      ; 4     t = 12 (16)
        BR bmi, dabort                  ; 2
        bit $00                         ; 3
        jmp dl                          ; 3     t = 20 (24)
dabort: jmp abort

; The command ended: its status, the end stamp, its record, the next entry.
dend:   WDTEST
        lda WDSTAT
        sta stat
        jsr seen
        lda wraps
        sta last
        STAMP last
        jmp record

; Send one queued byte before the command's first DRQ (entered by jmp from
; w0, back by jmp): the status is read at -14 (DRQ: no send, the byte now),
; 4 and 28; a DRQ seen after the write has its byte read at 13 or 37 and
; written at 40 or 48. The next status read in w0 is at 39; a DRQ seen
; before the write reaches dfirst's read 12 cycles later.
        ALIGN4 1
send:   ldy qhead                       ; 4
        ldx q,y                         ; 4
        WDOK
        lda WDSTAT                      ; 4     t = -14
        and #ST_DRQ                     ; 2
        bne snow                        ; 2
        lda #PB_CLK | PB_FSDIR          ; 2
        sta CIA_PB                      ; 4     t = -4
        stx CIA_SDR                     ; 4     t = 0
        WDOK
        lda WDSTAT                      ; 4     t = 4
        and #ST_DRQ                     ; 2
        BR bne, s4                      ; 2
        iny                             ; 2
        tya                             ; 2
        and #QMASK                      ; 2
        sta qhead                       ; 4     t = 18
        lda #PB_FSDIR                   ; 2
        sta CIA_PB                      ; 4     t = 24: CLK released
        WDOK
        lda WDSTAT                      ; 4     t = 28
        and #ST_DRQ                     ; 2
        BR bne, s28                     ; 2
        jmp w0                          ; 3     t = 35
        ALIGN4 1
s4:     WDOK
        ldx WDDAT                       ; 4     t = 13
        lda #PB_FSDIR                   ; 2
        sta CIA_PB                      ; 4     t = 19: CLK released
        iny                             ; 2
        tya                             ; 2
        and #QMASK                      ; 2
        sta qhead                       ; 4
        lda wraps                       ; 3
        sta first                       ; 4
        stx CIA_SDR                     ; 4     t = 40
        jmp dstamp
snow:   jmp dfirst
        ALIGN4 1
s28:    WDOK
        ldx WDDAT                       ; 4     t = 37
        lda wraps                       ; 3
        sta first                       ; 4
        stx CIA_SDR                     ; 4     t = 48
        jmp dstamp

; Queue byte A; X and Y kept.
put:    sty py
        ldy qtail
        sta q,y
        iny
        tya
        and #QMASK
        sta qtail
        ldy py
        rts

op:     .res 1
rep:    .res 1
lp:     .res 1
trksave: .res 1
sc:     .res 1
first:  .res 5                  ; wraps, ICR & TB, timer B high, low, high
last:   .res 5
stat:   .res 1                  ; the REC payload continues: status, flags,
flags:  .res 1                  ; count
count:  .res 2
REC_LEN = * - first


        .segment "CODE2"
        .org $0790

q:      .res QMASK + 1
        .assert >q = >(q + QMASK), error, "q crosses a page"
acc:    .res 1
bits:   .res 1
nb:     .res 1
py:     .res 1

; Add the first stamp's wrap flag to wraps.
seen:   lda first + 1
        lsr
        adc wraps
        sta wraps
        rts

; Queue X bytes from first + Y as six-bit chunks, most significant bit
; first, the last chunk padded with zeros.
pack:   lda #6
        sta bits
pbyte:  lda first,y
        sta sc
        lda #8
        sta nb
:       asl sc
        rol acc
        dec bits
        bne :+
        lda acc
        jsr chunk
        lda #6
        sta bits
:       dec nb
        bne :--
        iny
        dex
        bne pbyte
        lda bits
        cmp #6
        beq pret
:       asl acc
        dec bits
        bne :-
        lda acc
; Queue the low six bits of A as a chunk.
chunk:  asl
        asl
        ora #1
        jmp put
pret:   rts

; Queue a REC with flags A.
rec:    sta flags
        lda cnt
        sta count
        lda cnt + 1
        sta count + 1
        lda #M_REC
        jsr put
        ldy #0
        ldx #REC_LEN
        jmp pack

record: lda last + 1
        lsr
        adc wraps
        sta wraps
        lda #0
        jsr rec
        jmp nextcmd

; Timeout: stop the WD, close its record, end the stream.
timeout:
        WDTEST
        lda WDSTAT
        sta stat
        jsr force
        lda wraps
        sta last
        STAMP last
        lda cnt
        ora cnt + 1
        bne :++
        ldx #4
:       lda last,x
        sta first,x
        dex
        bpl :-
:       lda #F_TIMEOUT
        jsr rec
        lda #END_TIMEOUT
        jmp finish

abort:  jsr force
        lda #END_ATN
        jmp finish

; Wait for type I status IP = A with w0's housekeeping, the WD idle.
iwait:  sta sc
:       bit CIA_PB
        bmi abort
        jsr tick
        bne :+
        jmp timeout
:       lda qhead
        cmp qtail
        beq :+
        jsr plain
        jmp :--
:       lda CIA_TBHI
        and #KEEP_MASK
        cmp keep
        beq :+
        sta keep
        lda #M_KEEP
        jsr put
:       WDTEST
        lda WDSTAT
        and #ST_IP
        cmp sc
        bne :----
        rts

index:  jsr force
        lda #0
        jsr iwait
        lda #ST_IP
        jsr iwait
        lda wraps
        sta first
        STAMP first
        jsr seen
        lda #M_INDEX
        jsr put
        ldy #0
        ldx #5
        jsr pack
        jmp nextcmd

stream: lda #0
        sta qhead
        sta qtail
        sta lp
        sta wraps
        sta rep
        WDTEST
        lda WDTRK
        sta trksave
        lda TMO
        sta tmo
:       lda CIA_PB                      ; host go: CLK
        and #PB_CLKIN
        bne :+
        jsr tick
        bne :-
        lda #ST_NOGO
        rts
:       lda CIA_PB
        ora #PB_FSDIR
        sta CIA_PB
        lda #CRA_START | CRA_SPOUT      ; output, input, output (spout_patch)
        sta CIA_CRA
        lda #CRA_START
        sta CIA_CRA
        lda #CRA_START | CRA_SPOUT
        sta CIA_CRA
        lda #M_START
        jsr put
        lda CIA_TBHI
        and #KEEP_MASK
        sta keep
        jmp next

; Flush the queue with the WD idle, then the entry again or the next one.
nextcmd:
        lda qhead
        cmp qtail
        beq :+
        jsr plain
        jmp nextcmd
:       lda rep
        bne again
        lda lp
        clc
        adc #4
        sta lp
next:   ldy lp
        lda L,y
        cmp #$FF
        beq done
        sta op
        lda L + 3,y
        and #$7F
        sta rep
again:  dec rep
        lda TMO
        sta tmo
        lda #1
        sta cnt
        lda #0
        sta cnt + 1
        ldy lp
        lda op
        bne :+
        sta cnt
        jmp index
:       lda L + 1,y
        WDTEST
        sta WDTRK
        lda L + 2,y
        WDTEST
        sta WDSEC
        lda L + 3,y
        bpl :+
        tya
        tax
        inc L + 2,x
:       lda op
        jsr wdcmd
        jmp w0

done:   lda #M_END
; Queue A, send everything, wait until the last byte is out, input mode,
; drivers in, CLK released, track register back; A = the end code.
finish: pha
        jsr put
:       lda qhead
        cmp qtail
        beq :+
        jsr plain
        jmp :-
:       ldy #0
:       lda CIA_ICR
        and #ICR_SP
        bne :+
        dey
        bne :-
:       lda #CRA_START
        sta CIA_CRA
        lda CIA_PB
        and #<~(PB_FSDIR | PB_CLK)
        sta CIA_PB
        lda trksave
        WDTEST
        sta WDTRK
        pla
        rts
