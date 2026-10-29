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

pvgdiff: tools/pvgdiff.c src/tvg.c $(HEADERS)
	$(CC) $(CFLAGS) -Isrc -o $@ tools/pvgdiff.c src/tvg.c -lm

# ------------------------------------------------------------------ tests
#
# make test-fetch     download the test SVGs into tests/data
# make test-baseline  bundle them with the current converters
# make test           bundle again and compare renders with the baseline

# The converters' Python packages, installed into .venv from requirements.txt.
PYTHON := .venv/bin/python

.venv: requirements.txt
	python3 -m venv .venv
	$(PYTHON) -m pip install -q -r requirements.txt
	@touch .venv

TEST_DATA := tests/data
TWEMOJI   := $(TEST_DATA)/twemoji
TWEMOJI_URL := https://github.com/jdecked/twemoji.git
TWEMOJI_REV := b6b55fef1e8636b540a6d016a4729ca8cdf2e60b

# Fetches only the SVGs, about 18 MB.
$(TWEMOJI)/assets/svg:
	rm -rf $(TWEMOJI)
	git init -q $(TWEMOJI)
	git -C $(TWEMOJI) remote add origin $(TWEMOJI_URL)
	git -C $(TWEMOJI) sparse-checkout set assets/svg
	git -C $(TWEMOJI) fetch -q --depth 1 --filter=blob:none origin $(TWEMOJI_REV)
	git -C $(TWEMOJI) checkout -q FETCH_HEAD

test-fetch: $(TWEMOJI)/assets/svg

# $(1) is the output dir. 1/8 unit grid, 0.05 unit tolerance.
define bundle
	@mkdir -p $(1)
	$(PYTHON) -I tools/svg2pvg.py --grid 0 --tolerance 0.05 $(1)/twemoji.pvg $(TWEMOJI)/assets/svg/*.svg
endef

test-baseline: .venv $(TWEMOJI)/assets/svg
	$(call bundle,tests/baseline)

test: .venv pvgdiff $(TWEMOJI)/assets/svg
	@test -f tests/baseline/twemoji.pvg || { echo "Run make test-baseline first."; exit 1; }
	$(call bundle,tests/out)
	./pvgdiff 72 tests/baseline/twemoji.pvg tests/out/twemoji.pvg

clean:
	rm -f pvg2png tvgsheet pvgdiff
	rm -rf tests/out

.PHONY: all clean test test-fetch test-baseline
