#!/usr/bin/env bash
# setup_local.sh — copy gitignored local files from old repo to this one.
#
# Usage (run from the root of the new repo):
#   bash scripts/setup_local.sh /path/to/old/alek-core
#
# What is copied:
#   .env                        — secrets and settings
#   config/secrets/             — Firebase admin key
#   scripts/memory/             — local PII data and scripts
#   firestore_utils/downloads/  — local Firestore downloads
#   debug_prompts/              — local debug output
#
# What is NOT copied (recreate manually):
#   venv/         — run: python3 -m venv venv && pip install -r requirements.txt
#   *.log         — recreated on app start

set -euo pipefail

SOURCE="${1:-}"
TARGET="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "$SOURCE" ]]; then
    echo "Usage: bash scripts/setup_local.sh /path/to/old/alek-core"
    exit 1
fi

if [[ ! -d "$SOURCE" ]]; then
    echo "Error: source directory not found: $SOURCE"
    exit 1
fi

echo "Source: $SOURCE"
echo "Target: $TARGET"
echo ""

copy_if_exists() {
    local src="$SOURCE/$1"
    local dst="$TARGET/$1"
    if [[ -e "$src" ]]; then
        mkdir -p "$(dirname "$dst")"
        cp -a "$src" "$dst"
        echo "  ✓ $1"
    else
        echo "  - $1 (not found, skipping)"
    fi
}

echo "Copying local config files..."
copy_if_exists ".env"
copy_if_exists "config/secrets"
copy_if_exists "scripts/memory"
copy_if_exists "firestore_utils/downloads"
copy_if_exists "debug_prompts"

echo ""
echo "Done. Don't forget to:"
echo "  1. Update GOOGLE_APPLICATION_CREDENTIALS in .env if repo path changed"
echo "  2. python3 -m venv venv && pip install -r requirements.txt"
