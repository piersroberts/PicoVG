CC      ?= cc
CFLAGS  ?= -O2 -std=c11 -Wall -Wextra -Wpedantic
SDL_CFLAGS := $(shell sdl2-config --cflags)
SDL_LIBS   := $(shell sdl2-config --libs)

all: pvg2png tvgsheet

HEADERS := src/tvg.h src/pvg.h

pvg2png: tools/pvg2png.c src/tvg.c $(HEADERS)
	$(CC) $(CFLAGS) -Isrc -o $@ tools/pvg2png.c src/tvg.c -lz -lm

tvgsheet: tools/tvgsheet.c src/tvg.c $(HEADERS)
	$(CC) $(CFLAGS) $(SDL_CFLAGS) -Isrc -o $@ tools/tvgsheet.c src/tvg.c $(SDL_LIBS) -lm

clean:
	rm -f pvg2png tvgsheet

.PHONY: all clean
