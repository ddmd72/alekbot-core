#!/bin/bash
# Install git hooks for security checks
#
# Usage: ./scripts/install-git-hooks.sh
#
# This installs:
# - pre-commit: blocks commits with sensitive data (banks, companies, keywords, PII patterns)
# - pre-push: requires confirmation before pushing to main

set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
HOOKS_SRC="${REPO_ROOT}/scripts/git-hooks"
HOOKS_DEST="${REPO_ROOT}/.git/hooks"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "Installing git hooks..."
echo ""

# Check if hooks directory exists
if [ ! -d "$HOOKS_DEST" ]; then
    echo -e "${RED}Error: .git/hooks directory not found${NC}"
    exit 1
fi

# Check if hook sources exist
if [ ! -f "${HOOKS_SRC}/pre-commit" ] || [ ! -f "${HOOKS_SRC}/pre-push" ]; then
    echo -e "${RED}Error: Hook scripts not found in ${HOOKS_SRC}${NC}"
    exit 1
fi

# Install pre-commit hook
if cp "${HOOKS_SRC}/pre-commit" "${HOOKS_DEST}/pre-commit"; then
    chmod +x "${HOOKS_DEST}/pre-commit"
    echo -e "${GREEN}✓ Installed pre-commit hook${NC}"
    echo "  - Blocks commits with sensitive data (banks, companies, PII patterns)"
    echo "  - Checks commit message for keywords like 'privacy scrub', 'leak', 'breach'"
    echo "  - Verifies real bank/company names only in allowed files/examples"
else
    echo -e "${RED}✗ Failed to install pre-commit hook${NC}"
    exit 1
fi

# Install pre-push hook
if cp "${HOOKS_SRC}/pre-push" "${HOOKS_DEST}/pre-push"; then
    chmod +x "${HOOKS_DEST}/pre-push"
    echo -e "${GREEN}✓ Installed pre-push hook${NC}"
    echo "  - Requires explicit confirmation before pushing to main"
    echo "  - Shows commits and changed files before push"
else
    echo -e "${RED}✗ Failed to install pre-push hook${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}Git hooks installed successfully!${NC}"
echo ""
echo "Configuration files:"
echo "  - ${HOOKS_SRC}/lists/real-banks.txt (blocked bank names)"
echo "  - ${HOOKS_SRC}/lists/real-companies.txt (blocked company names)"
echo "  - ${HOOKS_SRC}/lists/allowed-phrases.txt (exceptions)"
echo "  - ${HOOKS_SRC}/lists/allowed-files.txt (paths where examples are OK)"
echo ""
echo "To bypass hooks temporarily (not recommended):"
echo "  git commit --no-verify"
echo "  git push --no-verify"
echo ""
echo "For push: export SKIP_PUSH_CHECK=1 git push origin main"
echo ""
