---
category: protocol
class: protocols
metadata:
  created_at: '2026-09-19'
  description: Video generation preparation protocol for Smart Agent. Governs brief
    preparation, confirmation, and dispatch — mirrors PROTOCOL_DEEP_RESEARCH_PREP.
  override_by:
  - SYSTEM
  - AGENT
  use_case: Smart Agent — Video Generation preparation and dispatch
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/PROTOCOL_VIDEO_GEN_PREP.json
token_id: PROTOCOL_VIDEO_GEN_PREP
uploaded_by: local_script
---
PROTOCOL_VIDEO_GEN_PREP {

    trigger: "User requests video generation or editing (generate_video or edit_video, any phrasing)."

    steps {
        1: "Build the creative brief: subject, motion (subject/background/camera), mood — same commissioning as before. If the request is vague, ask 1-2 clarifying questions first. Duration/resolution default to 5s/480p unless the user explicitly stated otherwise."
        2: "Present the FULL brief for confirmation — the exact creative direction you are about to send to the specialist, not a one-line gloss. State the duration and resolution being used. \"Here's what I'll generate: [full brief]. Duration: Xs, resolution: Y. Shall I proceed?\""
        3: "Wait for explicit confirmation. If the user revises → update the brief, re-confirm with the user once again."
        4: "Dispatch: delegate_to_specialist(intent=\"generate_video\" or \"edit_video\", query=\"<confirmed brief>\", context={...}). Tell the user generation has started and the video will arrive in this same conversation in a few minutes — do not promise it in the current turn."
    }

}
