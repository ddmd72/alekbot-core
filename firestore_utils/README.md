# Firestore Utils

> ## ⛔ AI AGENTS — READ THIS FIRST
>
> **YOU ARE FORBIDDEN FROM RUNNING ANY SCRIPT IN THIS DIRECTORY.**
> Upload and download operations connect to production Firestore.
> Uploads are destructive and irreversible.
> **ONLY THE HUMAN OWNER EXECUTES THESE SCRIPTS MANUALLY.**
> Do not call, invoke, or suggest running `upload.py` or `download.py` under any circumstance.

Utilities to download and upload Firestore documents for safe local editing.

**Important:** All operations target the `us-production` Firestore database by default.

## Prerequisites

Set up Google credentials (one of the following):

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Or login via gcloud:

```bash
gcloud auth application-default login
```

## Collection Names Reference

Collections follow the semantic naming convention (ADR-006):

**Production Collections** (no prefix):

- `domain_prompt_tokens_v3_system` - System prompt tokens
- `domain_prompt_tokens_v3_user` - User prompt tokens
- `domain_prompt_blueprints_v3` - Prompt blueprints
- `domain_prompt_profiles_v3` - Agent profiles
- `domain_prompt_overrides_v3` - User overrides
- `domain_users_v2` - User accounts
- `domain_accounts_v2` - Billing accounts
- `domain_facts_v2` - Knowledge base

**Development Collections** (development\_ prefix):

- `development_domain_prompt_tokens_v3_system`
- `development_domain_prompt_blueprints_v3`
- etc.

See `docs/08_concepts/DATABASE_SCHEMA.md` for complete schema.

## List documents in a collection

```bash
# List all documents
python firestore_utils/download.py domain_prompt_tokens_v3_system --list

# Verbose mode (show database/project info)
python firestore_utils/download.py domain_prompt_tokens_v3_system --list --verbose
```

## Download documents

Download only the `content` field (default groovy format):

```bash
# Production collections
python firestore_utils/download.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER
python firestore_utils/download.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_WEBSEARCH
python firestore_utils/download.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_CONSOLIDATION
python firestore_utils/download.py domain_prompt_tokens_v3_system FEW_SHOT_EXAMPLES_DEFAULT



# Development collections
python firestore_utils/download.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER
python firestore_utils/download.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_CONSOLIDATION
python firestore_utils/download.py development_domain_prompt_tokens_v3_system PROTOCOL_SEARCH_MEMORY
python firestore_utils/download.py development_domain_prompt_tokens_v3_system FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY
python firestore_utils/download.py development_domain_prompt_tokens_v3_system OUTPUT_FORMAT_JSON
python firestore_utils/download.py development_domain_prompt_tokens_v3_system OUTPUT_FORMAT_STANDARD
python firestore_utils/download.py development_domain_prompt_tokens_v3_system PROTOCOL_SMART_AGENT_SELECTION
python firestore_utils/download.py development_domain_prompt_tokens_v3_system PROTOCOL_QUICK_AGENT_SELECTION


python firestore_utils/download.py development_domain_prompt_tokens_v3_user ARCHETYPE_INTELLECTUAL_SNIPER
python firestore_utils/download.py development_domain_prompt_tokens_v3_user BEHAVIOR_GUIDE_RANEVSKAYA_MODE
python firestore_utils/download.py development_domain_prompt_tokens_v3_user HUMOR_PRESET_RANEVSKAYA
python firestore_utils/download.py development_domain_prompt_tokens_v3_user VIBE_BATTLE_WEARY
python firestore_utils/download.py development_domain_prompt_tokens_v3_user VOICE_APHORISTIC



python firestore_utils/download.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_SMART
python firestore_utils/download.py development_domain_prompt_tokens_v3_system FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY
python firestore_utils/download.py development_domain_prompt_tokens_v3_system BEHAVIOR_GUIDE_RANEVSKAYA_MODE

python firestore_utils/download.py development_domain_prompt_tokens_v3_system OUTPUT_FORMAT_JSON
python firestore_utils/download.py development_domain_prompt_tokens_v3_system POLICY_ALIGN_WITH_ANCHORS
python firestore_utils/download.py development_domain_prompt_tokens_v3_system POLICY_ANTI_GUARDIAN
python firestore_utils/download.py development_domain_prompt_tokens_v3_system POLICY_NO_OPEN_LOOPS
python firestore_utils/download.py development_domain_prompt_tokens_v3_system POLICY_OUTPUT_LANGUAGE
python firestore_utils/download.py development_domain_prompt_tokens_v3_system POLICY_PRIVACY
python firestore_utils/download.py development_domain_prompt_tokens_v3_system POLICY_WITTY_ACCENTUATION
python firestore_utils/download.py development_domain_prompt_tokens_v3_system RESPONSE_CONCISE
python firestore_utils/download.py development_domain_prompt_tokens_v3_system PROTOCOL_AGENT_SELECTION

python firestore_utils/download.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_TASKS
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_TASKS



python firestore_utils/upload.py development_domain_prompt_tokens_v3_user VOICE_APHORISTIC
python firestore_utils/upload.py development_domain_prompt_tokens_v3_user ARCHETYPE_INTELLECTUAL_SNIPER
python firestore_utils/upload.py development_domain_prompt_tokens_v3_user VIBE_BATTLE_WEARY
python firestore_utils/upload.py development_domain_prompt_tokens_v3_user HUMOR_PRESET_RANEVSKAYA
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_SMART
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_AGENT_SELECTION
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system OUTPUT_FORMAT_JSON

python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_QUICK

























```

Download full document as JSON:

```bash
# Blueprints
python firestore_utils/download.py development_domain_prompt_blueprints_v3 universal_agent_v1 --format json
python firestore_utils/download.py development_domain_prompt_blueprints_v3 universal_agent_v1
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 universal_agent_v1




# Profiles (SYSTEM level)
python firestore_utils/download.py development_domain_prompt_profiles_v3 universal_agent_v1_SYSTEM_smart --format json
python firestore_utils/download.py development_domain_prompt_profiles_v3 universal_agent_v1_SYSTEM_router --format json
python firestore_utils/download.py development_domain_prompt_profiles_v3 universal_agent_v1_SYSTEM_memorysearch --format json

```

Specify output file name:

```bash
python firestore_utils/download.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER \
  --output firestore_utils/downloads/custom_name.groovy
```

## Upload documents

Upload groovy file:

```bash
# Production
python firestore_utils/upload.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER
python firestore_utils/upload.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_CONSOLIDATION


# Development
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_CONSOLIDATION
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_SMART
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_SMART_AGENT_SELECTION
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_MEMORY_SEARCH
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_WEBSEARCH
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_QUICK
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_WEBSEARCH_LIGHT
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system EMAILSEARCH_COGNITIVE_PROCESS




python firestore_utils/upload.py development_domain_prompt_tokens_v3_system FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_SEARCH_MEMORY
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system OUTPUT_FORMAT_JSON
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system OUTPUT_FORMAT_STANDARD
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system EMAILSEARCH_OUTPUT_FORMAT



```

Upload JSON file (full document):

```bash
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_MEMORY_SEARCH --format json

# Blueprints
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 universal_agent_v1 --format json
python firestore_utils/upload.py domain_prompt_blueprints_v3 universal_agent_v1 --format json

# Profiles (SYSTEM level)
python firestore_utils/upload.py development_domain_prompt_profiles_v3 universal_agent_v1_SYSTEM_memorysearch --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 universal_agent_v1_SYSTEM_smart --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 universal_agent_v1_SYSTEM_websearch_light --format json

python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_WEBSEARCH_LIGHT --format json
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_QUICK_AGENT_SELECTION --format json

# Email Classifier agent (blueprint + profile + tokens)
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 email_classifier_v1 --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 email_classifier --format json
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system EMAIL_CLASSIFIER_TAXONOMY --format json
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system EMAIL_CLASSIFIER_COGNITIVE_PROCESS --format json


```

## Database Parameter (Advanced)

By default, scripts connect to `us-production` database. To override:

```bash
python firestore_utils/download.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER \
  --database us-production

python firestore_utils/upload.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_ROUTER \
  --database us-production

python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_WEBSEARCH_LIGHT \
  --database us-production

python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_QUICK_AGENT_SELECTION \
  --database us-production
```

## Notes

- **Default database:** `us-production` (contains both production and development collections)
- `document_id` is derived from the document name argument
- `.groovy` behavior depends on document type:
  - **Token v3 documents** (with `token_id` field): Updates only `content` field (smart merge)
  - **Legacy documents**: Full overwrite with `{content, updated_at, uploaded_by, source_file}`
- `.json` uploads the full JSON document with full overwrite (`merge=False`)
- `updated_at`, `uploaded_by`, `source_file` are added automatically
- Downloads are stored in `firestore_utils/downloads/` by default
- Uploads are read from `firestore_utils/uploads/`

## Token v3 Smart Update

For Token v3 documents (Prompt Design System v3), the upload script automatically detects the document type:

```bash
# Safe for Token v3 - only updates content field
python firestore_utils/upload.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_CONSOLIDATION

# Output: ℹ️  Updated content field only (Token v3 document)
```

This prevents accidentally overwriting `token_id`, `category`, `class`, and `metadata` fields when updating prompt content.
