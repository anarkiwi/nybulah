OUT ?= ../nybulah/drivecode
PROTOS := 1 2 $(if $(wildcard proto_x.inc),3)
TRACK_MODELS := 1541 1571
TRACK_BASE := 0300
PASSPG_1541 := 9F
PASSPG_1571 := 7F
M1581 := s1 s2 xb s4
M1581_PROTO_s1 := 1
M1581_PROTO_s2 := 2
M1581_PROTO_xb := 4
M1581_PROTO_s4 := 5
MFM := $(if $(wildcard mfm.s),mfm) $(if $(wildcard mfmstream.s),mfmstream)
BINS := $(PROTOS:%=$(OUT)/monitor_s%.bin) $(TRACK_MODELS:%=$(OUT)/track_%.bin) \
	$(OUT)/sense_1571.bin $(OUT)/seek_1571.bin $(OUT)/stream_1571.bin \
	$(if $(wildcard proto_xb.inc),$(OUT)/monitor_xb.bin) \
	$(if $(wildcard proto_srq.inc),$(OUT)/monitor_s4.bin $(OUT)/ciaprobe.bin) \
	$(M1581:%=$(OUT)/monitor_%_1581.bin) $(OUT)/ciaprobe_1581.bin \
	$(MFM:%=$(OUT)/%_1581.bin) $(OUT)/ramtest_10.bin $(OUT)/ramtest_11.bin

all: $(BINS)

$(OUT)/monitor_s%.bin: monitor.s drive.cfg $(wildcard proto_*.inc)
	ca65 -t none -D PROTO=$* -o $(OUT)/monitor_s$*.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_s$*.o
	rm -f $(OUT)/monitor_s$*.o

$(OUT)/monitor_xb.bin: monitor.s drive.cfg $(wildcard proto_*.inc) burst.inc
	ca65 -t none -D PROTO=4 -o $(OUT)/monitor_xb.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_xb.o
	rm -f $(OUT)/monitor_xb.o

$(OUT)/monitor_s4.bin: monitor.s drive.cfg $(wildcard proto_*.inc) burst.inc
	ca65 -t none -D PROTO=5 -o $(OUT)/monitor_s4.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_s4.o
	rm -f $(OUT)/monitor_s4.o

$(OUT)/monitor_%_1581.bin: monitor.s drive.cfg $(wildcard proto_*.inc) burst.inc
	ca65 -t none -D PROTO=$(M1581_PROTO_$*) -D M1581=1 -o $(OUT)/monitor_$*_1581.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_$*_1581.o
	rm -f $(OUT)/monitor_$*_1581.o

$(OUT)/ciaprobe_1581.bin: ciaprobe.s
	ca65 -t none -D M1581=1 -o $(OUT)/ciaprobe_1581.o $<
	ld65 -t none -S '$$0300' -o $@ $(OUT)/ciaprobe_1581.o
	rm -f $(OUT)/ciaprobe_1581.o

$(OUT)/%_1581.bin: %.s mfm.cfg mfm.inc
	ca65 -t none -o $(OUT)/$*_1581.o $<
	ld65 -C mfm.cfg -o $@ $(OUT)/$*_1581.o
	rm -f $(OUT)/$*_1581.o

$(OUT)/ramtest_%.bin: ramtest.s
	ca65 -t none -o $(OUT)/ramtest_$*.o $<
	ld65 -t none -S '$$$*00' -o $@ $(OUT)/ramtest_$*.o
	rm -f $(OUT)/ramtest_$*.o

$(OUT)/ciaprobe.bin: ciaprobe.s
	ca65 -t none -o $(OUT)/ciaprobe.o $<
	ld65 -t none -S '$$0300' -o $@ $(OUT)/ciaprobe.o
	rm -f $(OUT)/ciaprobe.o

$(OUT)/track_%.bin: track.s track.cfg
	ca65 -t none -D MODEL=$* -o $(OUT)/track_$*.o $<
	ld65 -C track.cfg -S '$$$(TRACK_BASE)' -D PASSPG='$$$(PASSPG_$*)' -o $@ $(OUT)/track_$*.o
	rm -f $(OUT)/track_$*.o

$(OUT)/seek_1571.bin: track.s track.cfg
	ca65 -t none -D MODEL=1571 -D SEEK=1 -o $(OUT)/seek_1571.o $<
	ld65 -C track.cfg -S '$$$(TRACK_BASE)' -D PASSPG='$$$(PASSPG_1571)' -o $@ $(OUT)/seek_1571.o
	rm -f $(OUT)/seek_1571.o

$(OUT)/stream_1571.bin: stream.s stream.cfg
	ca65 -t none -o $(OUT)/stream_1571.o $<
	ld65 -C stream.cfg -S '$$$(TRACK_BASE)' -o $@ $(OUT)/stream_1571.o
	rm -f $(OUT)/stream_1571.o

$(OUT)/sense_1571.bin: sense.s sense.cfg
	ca65 -t none -o $(OUT)/sense_1571.o $<
	ld65 -C sense.cfg -S '$$6000' -o $@ $(OUT)/sense_1571.o
	rm -f $(OUT)/sense_1571.o

clean:
	rm -f $(OUT)/monitor_*.bin $(OUT)/track_*.bin $(OUT)/sense_*.bin $(OUT)/stream_*.bin $(OUT)/seek_*.bin $(OUT)/ciaprobe*.bin $(OUT)/mfm*.bin $(OUT)/ramtest_*.bin

.PHONY: all clean
