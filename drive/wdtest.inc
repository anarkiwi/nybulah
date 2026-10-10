; WD1770/WD177x access rule of the 1571 and 1581 DOS (wd1770.src and
; mfmmacro.src WDTEST, "fill address error"): no instruction that touches the
; WD starts at an address whose low two bits are 00.

; Pad with a nop when the next instruction would start at an address whose
; low two bits are 00 (untimed code).
.macro WDTEST
        .if (* & 3) = 0
        nop
        .endif
.endmacro

; After an unconditional jump: pad with an unreachable byte so the WD access
; at the next label keeps the rule without changing any timing.
.macro WDALIGN
        .if (* & 3) = 0
        .byte $EA
        .endif
.endmacro

; After an unconditional jump: pad with unreachable bytes to an address
; that is k modulo 4, so a timed block's WD accesses keep the rule.
.macro ALIGN4 k
        .res (k - *) & 3, $EA
.endmacro

; Assert the rule instead (timed code).
.macro WDOK
        .assert (* & 3) <> 0, error, "WD access at an address ending in 00"
.endmacro
