CC      ?= clang
CFLAGS  ?= -fobjc-arc -O2
FRAMEWORKS = -framework Foundation -framework IOKit

BIN = macstat-sensors

.PHONY: all clean

all: $(BIN)

$(BIN): macstat-sensors.m
	$(CC) $(CFLAGS) $(FRAMEWORKS) -o $@ $<

clean:
	rm -f $(BIN)
