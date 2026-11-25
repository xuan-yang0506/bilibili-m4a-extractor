PYTHON ?= .venv/bin/python
SCRIPT ?= extract_bilibili_audio.py
AVATAR_SCRIPT ?= download_bilibili_avatar.py
SOURCE ?=
OUT_DIR ?= outputs
EXTRA_ARGS ?=

.PHONY: check-source extract import avatar

check-source:
	@if [ -z '$(SOURCE)' ]; then \
		echo "Usage: make extract SOURCE='<Bilibili URL or share text>'" >&2; \
		echo "   or: make import SOURCE='<Bilibili URL or share text>'" >&2; \
		echo "   or: make avatar SOURCE='<Bilibili space URL or user id>'" >&2; \
		exit 2; \
	fi

extract: check-source
	$(PYTHON) $(SCRIPT) '$(SOURCE)' $(EXTRA_ARGS)

import: check-source
	$(PYTHON) $(SCRIPT) '$(SOURCE)' --import-to-music $(EXTRA_ARGS)

avatar: check-source
	$(PYTHON) $(AVATAR_SCRIPT) '$(SOURCE)' --out-dir '$(OUT_DIR)'
