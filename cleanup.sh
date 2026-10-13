#!/usr/bin/env sh
set -eu
DATA_DIR="${CLIPFORGE_DATA:-./data}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
case "$RETENTION_DAYS" in *[!0-9]*|'') echo "RETENTION_DAYS must be an integer" >&2; exit 2;; esac
mkdir -p "$DATA_DIR"
find "$DATA_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION_DAYS" -print -exec rm -rf -- {} \;
