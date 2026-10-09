OUT ?= ../nybulah/drivecode
PROTOS := 1 2 $(if $(wildcard proto_x.inc),3)
BINS := $(PROTOS:%=$(OUT)/monitor_s%.bin)

all: $(BINS)

$(OUT)/monitor_s%.bin: monitor.s drive.cfg $(wildcard *.inc)
	ca65 -t none -D PROTO=$* -o $(OUT)/monitor_s$*.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_s$*.o
	rm -f $(OUT)/monitor_s$*.o

clean:
	rm -f $(OUT)/monitor_s*.bin

.PHONY: all clean
