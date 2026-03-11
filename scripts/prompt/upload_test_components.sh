#!/bin/bash
# Upload test components for 4-level prompt integration tests
# SESSION_26: Quick setup for test_prompt_4level_e2e.py

set -e

ENV=${1:-test}

echo "📦 Uploading test components to $ENV environment..."

# Upload account-level test component
echo "  - Uploading ACCOUNT: test_master_account"
python scripts/prompt/sync_components.py \
  --env $ENV \
  --level account \
  --account-id test_master_account

# Upload user-level test component
echo "  - Uploading USER: test_dev_user"
python scripts/prompt/sync_components.py \
  --env $ENV \
  --level user \
  --user-id test_dev_user

echo "✅ Test components uploaded successfully!"
echo ""
echo "Run tests:"
echo "  pytest tests/integration/test_prompt_4level_e2e.py -v -s"
