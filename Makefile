PYTHON ?= .venv/bin/python
SCRIPT ?= extract_bilibili_audio.py
SOURCE ?=

.PHONY: check-source extract import

check-source:
	@if [ -z '$(SOURCE)' ]; then \
		echo "Usage: make extract SOURCE='<Bilibili URL or share text>'" >&2; \
		echo "   or: make import SOURCE='<Bilibili URL or share text>'" >&2; \
		exit 2; \
	fi

extract: check-source
	$(PYTHON) $(SCRIPT) '$(SOURCE)'

import: check-source
	$(PYTHON) $(SCRIPT) '$(SOURCE)' --import-to-music
