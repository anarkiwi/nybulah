OUT ?= ../nybulah/drivecode
PROTOS := 1 2 $(if $(wildcard proto_x.inc),3)
TRACK_MODELS := 1541 1571
TRACK_BASE := 0300
BINS := $(PROTOS:%=$(OUT)/monitor_s%.bin) $(TRACK_MODELS:%=$(OUT)/track_%.bin) \
	$(if $(wildcard proto_xb.inc),$(OUT)/monitor_xb.bin)

all: $(BINS)

$(OUT)/monitor_s%.bin: monitor.s drive.cfg $(wildcard proto_*.inc)
	ca65 -t none -D PROTO=$* -o $(OUT)/monitor_s$*.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_s$*.o
	rm -f $(OUT)/monitor_s$*.o

$(OUT)/monitor_xb.bin: monitor.s drive.cfg $(wildcard proto_*.inc)
	ca65 -t none -D PROTO=4 -o $(OUT)/monitor_xb.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_xb.o
	rm -f $(OUT)/monitor_xb.o

$(OUT)/track_%.bin: track.s track.cfg
	ca65 -t none -D MODEL=$* -o $(OUT)/track_$*.o $<
	ld65 -C track.cfg -S '$$$(TRACK_BASE)' -o $@ $(OUT)/track_$*.o
	rm -f $(OUT)/track_$*.o

clean:
	rm -f $(OUT)/monitor_*.bin $(OUT)/track_*.bin

.PHONY: all clean
