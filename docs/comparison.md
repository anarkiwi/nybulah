# Comparison with other tools

Facts about other tools are taken from their own documentation or project
pages, cited per row. Entries marked (U) rest on a single secondary source.

## Tools that use a Commodore drive

| | nybulah | nibtools | OpenCBM d64copy / cbmcopy | 1541 Ultimate, Pi1541 |
|---|---|---|---|---|
| Raw 1541 tracks | serial IEC + 8 KB RAM expansion | parallel cable | no (sectors) | no capture (drive emulation) |
| Raw 1571 tracks | SRQ streaming (firmware v12) or RAM expansion | SRQ or parallel | no | no capture |
| 1581 | WD177x tracks, sectors, D81, IMD | no | cbmcopy files only | emulated |
| Sync lengths | per-byte arrival times | no | no | n/a |
| Index alignment on a stock 1571 | WD1770 index sensor | needs an index-hole sensor mod | no | n/a |
| Revolution detection | significance test against sector headers | byte matching in a fixed window | n/a | n/a |
| Recovery from bus stalls | without power cycling (firmware v9+) | no | no | n/a |
| Write-back | D64/D71/D81, verified by re-capture | yes (nibwrite) | sectors | from emulation |
| Licence | Apache-2.0 | GPL-3.0 | GPL-2.0 | GPL-3.0 |

Sources: [nibtools](https://github.com/rittwage/nibtools),
[OpenCBM](https://opencbm.trikaliotis.net/opencbm-6.html),
[1541 Ultimate drive docs](https://1541u-documentation.readthedocs.io/en/latest/howto/mm_drive.html),
[Pi1541](https://cbm-pi1541.firebaseapp.com/).
d2d64 reads 1571 sectors over SRQ
([blog](https://blog.worldofjani.com/?p=2752)); VICE `c1541` reaches real
drives through OpenCBM at DOS level
([VICE manual](https://vice-emu.sourceforge.io/vice_14.html)).

## Flux tools with a PC drive

| | Greaseweazle | KryoFlux | SuperCard Pro | FluxEngine | Applesauce |
|---|---|---|---|---|---|
| Commodore formats | 1541, 1571, 1581, CMD FD codecs | CBM GCR, named protection formats | C64/C128 GCR, weak-bit modes | 1541, 1581, CMD FD | 1541, 1571 |
| Output | SCP, KF, HFE, D64, D71, D81 | stream, G64, D64 | SCP, G64, D64 | D64, D81 | G64, D71 |
| Write-back | yes | yes | yes | yes | — |
| Licence | Unlicense (U) | proprietary | proprietary freeware | GPL-2.0 | free client |

Sources: [Greaseweazle image types](https://github.com/keirf/greaseweazle/wiki/Supported-Image-Types),
[KryoFlux formats](https://kryoflux.com/?page=kf_formats),
[SuperCard Pro](https://www.cbmstuff.com/index.php?route=product/product&product_id=52),
[FluxEngine](https://github.com/davidgiven/fluxengine),
[Applesauce](https://applesaucefdc.com/software/).

A PC drive measures flux directly, including no-flux areas and sub-cell
timing, which nybulah infers from byte timing. A 96 tpi PC head is not the
1541 head, and a 1541 disk's flip side needs an index hole or a fake index
([Greaseweazle: flippy disks](https://github.com/keirf/Greaseweazle/wiki/Flippy-Disks)).
nybulah reads SCP and KryoFlux streams through a 1541 read-circuit model
([formats.md](formats.md#flux-to-bits)), so flux captures and drive captures
share one analysis path.

## Converters

- [g64conv](https://github.com/markusC64/g64conv): KryoFlux, SCP and P64 to
  G64, G71, P64 and D64 (GPL-3.0).
- [ReMaster Utility](https://github.com/DarylKrans/ReMaster-Utility): NIB,
  NBZ and G64 to writable G64, with detection of several named protection
  schemes.

## Positioning

| capability | nybulah | nibtools | flux boards |
|---|---|---|---|
| Original drive head | yes | yes | no |
| No drive modification | 1571 yes; 1541 needs RAM expansion | 1571 over SRQ; 1541 needs a parallel cable | yes |
| Raw 1581 | yes | no | D81 decoding |
| True flux timing | inferred from byte timing | no | yes |
| Named-protection remastering | no | per-track presets | KryoFlux: yes |
| Statistical revolution and stability analysis | yes | no | partial |

## Historic tools

C64/C128-era copiers and their successors, from their manuals or from
secondary summaries where the manual is a scan. "?" means the source does
not say. Method: N nibbler (raw GCR), P parameter copier, F fast/sector
copier.

| tool | 1541 / 1571 / 1581 | halftracks | density | syncs, killers | write alignment | bad GCR, errors | tracks | parallel cable | method | source |
|---|---|---|---|---|---|---|---|---|---|---|
| Burst Nibbler | yes / yes / no | partial | ? | sync reduction | ? | ? | 1–41 | required | N | [c64copyprotection](https://www.c64copyprotection.com/deep-scan-burst-nibbler/) |
| Fast Hack'em C64 | yes / no / no | ? | ? | ? | ? | ? | ? | no | N, P, F; two-drive copy without the C64 | [commodoregames](https://www.commodoregames.net/copyprotection/copy-tools-parameters.asp) |
| Fast Hack'em C128 v6 | yes / yes / files only | duplicate halftracks | ? | ? | ? | errors 20–23, 27, 29 | 1–70 | no | N, F; header and tail gap settings | [manual](https://commodoremania.bytemaniacos.com/Libros/Application/Fast_Hack'em_C128_V6.0.pdf) |
| Maverick / Renegade | yes / yes / fast copy | ? | duplication | ? | ? | ? | 40+ | no (RAMBoard optional) | N, P, GCR editor | [commodoregames](https://www.commodoregames.net/copyprotection/copy-tools-parameters.asp), [dfarq](https://dfarq.homeip.net/maverick-final-generation-c-64-copier/) |
| Super Snapshot v5 | yes / yes / copy only | ? | whole-track detection | ? | ? | RapidLok copier | 1–40; 1–80 on a 1571 | no | N, P | [manual](https://rr.pokefinder.org/rrwiki/images/c/c0/Super_Snapshot_v5.0_Operating_Manual.pdf) |
| Di-Sector | yes / yes / no | partial | ? | ? | ? | ? | 36–40 | no | N, P, editor | [commodoregames](https://www.commodoregames.net/copyprotection/copy-tools-parameters.asp) |
| Copy II 64/128 | yes / yes / ? | ? | duplication | ? | ? | ? | ? | no | P, N | [commodoregames](https://www.commodoregames.net/copyprotection/copy-tools-parameters.asp) |
| Kracker Jax | yes / yes / fast copy | ? | per-track scan | ? | ? | RapidLok routines | ? | no | P, N | [c64copyprotection](https://www.c64copyprotection.com/c-128-canon/) |
| The Clone Machine | up to four 1541s / ? / no | ? | ? | ? | ? | error sectors | ? | no | F with errors | [commodoregames](https://www.commodoregames.net/copyprotection/copy-tools-parameters.asp) |
| Ultra Copy II | yes / yes / ? | yes | ? | ? | ? | data in gaps | above 35 | no | scanning copier | [c64copyprotection](https://www.c64copyprotection.com/ultra-copy-ii/) |
| MNIB | yes / yes / no | ? | per zone, detected | flags killers, no-sync | no write-back | ? | 36–41 | required | N to NIB | [project page](http://www.tim-schuermann.de/c64/de/2003/mnib.html) |
| nibtools | yes / yes / no | read and write | detected or forced | skip killers, reduce or fix syncs | sector 0, longest gap/sync, skew, timer, index hole | fix bad GCR; protection handlers | to 41 | parallel, or SRQ on a 1571 | N, NIB/G64 write | [readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt) |
| nybulah | RAM expansion / SRQ or RAM / WD177x | library | per track | measured sync lengths, killer class | 1571 index start only | analysed: illegal GCR, weak regions, MFM weak bytes | library to halftrack 84; CLI 35 or 40 | no | capture and analysis; D64/D71/D81 write | [features.md](features.md) |

## Gaps

Capabilities of the tools above that nybulah does not have:

- **Raw image write-back.** `write` takes D64, D71 and D81 only; the
  library's `Nibbler.write_track` is not driven from G64, NIB, NBZ or P64.
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt))
- **Track alignment and skew on write.** No policy aligning track starts to
  sector 0, the longest gap or sync, a skew, a timer or an index hole; index
  starts work on a 1571 only.
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt),
  [protection methods](https://www.commodoregames.net/copyprotection/protection-methods.asp))
- **Fat tracks.** Not detected and not reproduced
  ([scenarios.md](scenarios.md#how-each-scenario-presents-and-what-handles-it);
  [nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt)).
- **Halftrack and 41–42 track imaging from the CLI.** `read --tracks`
  offers 35 or 40 whole tracks to a sector image; raw captures exist only
  through `--archive`.
  ([c64copyprotection: Burst Nibbler](https://www.c64copyprotection.com/deep-scan-burst-nibbler/),
  [MNIB](http://www.tim-schuermann.de/c64/de/2003/mnib.html))
- **Protection-specific handlers and parameters** (V-MAX, RapidLok, Vorpal;
  parameter libraries).
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt),
  [commodoregames](https://www.commodoregames.net/copyprotection/copy-tools-parameters.asp),
  [Super Snapshot v5 manual](https://rr.pokefinder.org/rrwiki/images/c/c0/Super_Snapshot_v5.0_Operating_Manual.pdf))
- **Fitting raw tracks to the target drive** (sync and gap reduction,
  capacity matched to measured RPM); D64 formatting already uses the
  measured capacity.
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt))
- **Reproducing bad GCR, no-flux and weak regions on write.** Unformatted
  tracks are written as captured read noise.
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt))
- **Gap preservation on sector-image writes.** `format_track` fills gaps
  with `0x55`; gap lengths and fill are a known protection check.
  ([Fast Hack'em C128 v6 manual](https://commodoremania.bytemaniacos.com/Libros/Application/Fast_Hack'em_C128_V6.0.pdf),
  [protection methods](https://www.commodoregames.net/copyprotection/protection-methods.asp))
- **Index-hole sensor on a 1541.** Index use needs a 1571.
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt))
- **Unformatting or erasing tracks.**
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt))
- **Density changes within a track on write** (unverified for nibtools
  too): each `write_track` call uses one density.
  ([nibtools readme](https://github.com/OpenCBM/nibtools/blob/master/readme.txt))
- **Raw 1581 write from the CLI.** `write` formats from a D81;
  `r1581` track and deleted-sector writes are library-only. Historic 1581
  tools copied files or sectors only.
