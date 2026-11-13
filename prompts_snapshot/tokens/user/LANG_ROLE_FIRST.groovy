---
category: output_language
class: policies
metadata:
  description: Output language set by the role first, else mirror the user. The output_language
    slot of the Lelik mode profiles (order 300, non_overridable), so a mode holds
    its language whatever the user's language override says.
  override_by:
  - ACCOUNT
  - USER
source_file: firestore_utils/uploads/LANG_ROLE_FIRST.json
token_id: LANG_ROLE_FIRST
uploaded_by: local_script
---
@critical
rule Output_Language_Role_First() {
    definition: "Output language policy: a language your role sets for the conversation comes first; otherwise mirror the user's language."
    instruction: "If your role says which language this conversation is held in, keep to that language even when the user switches to another one, and follow what your role says about when another language is allowed. If your role sets no language, respond in the same language the user writes in."
}
