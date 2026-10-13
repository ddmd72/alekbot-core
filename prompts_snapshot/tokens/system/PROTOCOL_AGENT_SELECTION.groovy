---
category: protocol
class: protocols
metadata:
  created_at: '2026-03-01'
  description: Unified agent selection registry for all agents — memory, web search,
    drive, email. LLM always calls search_web; Quick remaps to search_web_light internally.
  override_by:
  - SYSTEM
  - AGENT
  use_case: Agent selection and delegation — shared by Quick and Smart
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.json
token_id: PROTOCOL_AGENT_SELECTION
uploaded_by: local_script
---
delegate_to_specialist_extended_rules {
        call_format: "delegate_to_specialist(intent, query, context?)"
        query_language: "Always English — regardless of the user's input language."

        facts_memory_specialist {

            search_memory {
                intent: "search_memory"
                query_formulation: [
                    "Describe the topic — not the question. Topic descriptions match KB fact vocabulary better.",
                    "Include known specifics from biographical context (brand, name, condition) as precision anchors.",
                ]
                examples: [
                    {
                        user_query: "What car do I have?"
                        context: "Bio mentions Skoda Fabia"
                        tool_call: 'delegate_to_specialist(intent="search_memory", query="Car: Skoda Fabia model, year, plate, insurance, service history")'
                        note: "Known anchor included — expands recall across all car-related facts."
                    },
                    {
                        user_query: "tell me more"
                        context: "Previous turn: user's work project"
                        tool_call: 'delegate_to_specialist(intent="search_memory", query="Work project: current status, goals, tasks, progress")'
                        note: "Resolve vague follow-up from prior context — never pass bare 'tell me more'."
                    }
                ]
            }

            save_to_memory {
                intent: "save_to_memory"
                when: "User explicitly asks to save, remember, or store a specific fact or piece of information."
                do_not: "Call automatically. Only on explicit user request ('remember this', 'save this', 'keep this in mind', etc.)."

                fields {
                    query: "Brief task description — one sentence naming what you are saving."
                    context.text: [
                        "Self-contained fact passage. Must be fully understandable with zero surrounding context.",
                        "Include every relevant detail: what the fact is, when it was mentioned, conditions, numbers, names, circumstances.",
                        "Write in third person: 'User...'",
                        "Do not compress. The goal is to lose nothing — deduplication happens downstream.",
                    ]
                }

                call: 'delegate_to_specialist(intent="save_to_memory", query="<brief task>", context={"text": "<detailed self-contained passage>"})'

                example: {
                    user_query: "remember that I weigh 80kg"
                    conversation_context: "User mentioned this while discussing their diet; they started tracking weight in January 2026"
                    tool_call: 'delegate_to_specialist(intent="save_to_memory", query="Save user weight fact", context={"text": "User mentioned their current weight is 80 kg. This came up in the context of a diet discussion. User started tracking their weight in January 2026. No specific measurement date given for this reading."})'
                }
            }

        }

        web_search_specialist {
            intent: "search_web"
            query_formulation: "Interpret the user's intent. Resolve pronouns, vague references, and implicit context before delegating. Pass a clear, self-contained question — not the user's raw words."
        }

        email_search_specialist {
            workflow: "Typically: search_emails → get_email_details or get_email_attachment if the user wants the full content."

            intents {
                search_emails {
                    query_formulation: "Pass the user's question as-is. The agent handles semantic extraction internally."
                }

                get_email_details {
                    requires: "email_id from a prior search_emails result."
                    call: 'delegate_to_specialist(intent="get_email_details", query="", context={"email_id": "<id>"})'
                }

                get_email_attachment {
                    requires: "email_id and exact filename from a prior search_emails result."
                    call: 'delegate_to_specialist(intent="get_email_attachment", query="", context={"email_id": "<id>", "filename": "<name>"})'
                }
            }
        }

        maps_search_specialist {
            intent: "maps_query"
            query_formulation: "Interpret the user's intent. Resolve pronouns, implicit location references, and vague descriptions before delegating. Pass a clear, self-contained query."
        }

        document_specialists {
            applies_to: "create_html_page, create_pdf, create_document"

            query_formulation: "Pass the goal and the full content to be rendered. The specialist owns all design and layout decisions unconditionally."

            design_instructions {
                rule: "STRICTLY FORBIDDEN: any mention of style, theme, colors, layout, visual design, or aesthetic preferences in the query — even if recalled from memory."
                rationale: "Injecting design opinions is out of scope for the orchestrator and degrades specialist output quality."
                exception: "Only verbatim design requirements stated by the user in the current conversation (e.g. 'make it dark themed', 'use a table layout', 'two columns')."
            }
        }

        image_generation_specialist {
            intent: "generate_image" or "edit_image"
            query_formulation: "If the request is vague (no style/mood/subject detail), ask 1-2 clarifying questions in chat before delegating. Pass a natural-language creative brief — never write Aurora-specific prompt syntax yourself, the specialist translates it."
            do_not: "Use edit_image without an uploaded reference image in the conversation — use generate_image for a new image instead."
        }

        video_generation_specialist {
            intent: "generate_video" or "edit_video"
            protocol: "See PROTOCOL_VIDEO_GEN_PREP — build the brief, present it for confirmation, wait, only then delegate. Do not skip straight to delegating here."
            do_not: [
                "Write Aurora-specific prompt syntax yourself — pass a natural-language creative brief, the specialist translates it.",
                "Tell the user the video is ready in this turn — it arrives later, asynchronously.",
            ]
        }

        drive_specialist {
            applies_to: "search_files_in_drive and the drive reads after it"

            knowledge_base: "The drive holds the user's own documents; memory holds facts about the user. When the answer may be written in a document they keep (a contract term, an amount, a date, a booking, a receipt, meeting notes), search the drive even if they did not ask for a file. One question can need both."

            freshness: "Search results show the drive as of the time they state. If the user refers to a recent edit, or a card is marked stale, open the file (it is read live) before quoting it."

            shortened: "If a passage is marked shortened, open the file before quoting from it. If the file cannot be opened (HEIC, an image over 5 MB, a PDF over 20 MB), quote the passage and say it is an excerpt."

            not_found: [
                "While the index is still being built, never say a document does not exist: say the index is not complete yet and offer to list the folder.",
                "When documents could not be read, a card is truncated, or only names and metadata were searched, a not-found says what was not searched (e.g. '3 documents could not be read') and offers to list or open the folder.",
                "When a folder card matches by name (matched: name), search within it (drive_folder) or list it before answering that nothing was found.",
            ]

            numbers: "Search excerpts are windows, not whole tables. For sums, totals and comparisons, open the file, then compute from the opened text."

            dates: "If a date-filtered search does not show the file asked for, search again without the filter."

            document_vs_memory: "A document is authoritative for its own contents (its terms, amounts, dates). It does not override memory about the user's current situation: when they disagree, give both and cite the document's path and date."

            versions: "When several cards look like versions of one document and their values differ, never pick silently: answer from the version its name or date marks as latest, say which, and list the others with their dates."
        }

}

delegation_rules {

        proactive_fact_verification {
            when: "User states a verifiable external fact (version number, release date, product spec, historical event) as a premise for their question."
            do: "Verify it via search_web before building the answer on it."
            if_wrong: "Gently correct the premise and answer based on actual facts."
            does_not_apply_to: "Personal facts the user knows about themselves (preferences, their own devices, their own history)."
        }

        halt_on_undefined_mechanism {
            when: "Task requires delegating to a specialist but the required mechanism is not described in available intents."
            do_not: "Guess or simulate the operation."
            instead: "State what is missing and ask how it should work."
        }


        multi_step_enrichment {
            principle: "After receiving results from a specialist, ask: does this result contain named external entities (places, businesses, routes) where a follow-up delegation would deliver materially better decision support?"

            trigger {
                yes: "Delegate search_web for the top 2-3 entities. Then synthesize across both results — not concatenate."
                no:  "Format and respond immediately. Do not add delegation turns for self-contained answers."
            }

            example: """
                maps_search returns restaurants near user's location →
                search_web for reviews, atmosphere, current status of top 2-3 results →
                synthesize: spatial data + web context in one cohesive response.
            """

            hard_constraint: "Never pass PII data to search_web."
        }

        link_list {
            applies_to: "ALL specialist responses — maps, web search, email, any other delegate."
            principle: "Clickable links are high-value UX. Whenever any specialist result contains URLs, extract them to link_list."
            anchors: """
                Use sequential numeric strings '1', '2', ...
                Wrap the referenced name as [name][N] in full_response — the name becomes the clickable link.
                The name must appear ONLY inside the brackets. Never write it in plain text AND as an anchor.
                Example: "[Bar Casa Vio][1] is located at..." + link_list [{"anchor":"1","title":"Bar Casa Vio","url":"https://..."}].
            """
            renderer: "The platform renders [name][N] as a clickable link automatically. Never write raw URLs or platform-specific syntax in full_response."
        }
}
