OUT ?= ../nybulah/drivecode
BINS := $(OUT)/monitor_s1.bin $(OUT)/monitor_s2.bin

all: $(BINS)

$(OUT)/monitor_s%.bin: monitor.s drive.cfg
	ca65 -t none -D PROTO=$* -o $(OUT)/monitor_s$*.o $<
	ld65 -C drive.cfg -o $@ $(OUT)/monitor_s$*.o
	rm -f $(OUT)/monitor_s$*.o

clean:
	rm -f $(BINS)
