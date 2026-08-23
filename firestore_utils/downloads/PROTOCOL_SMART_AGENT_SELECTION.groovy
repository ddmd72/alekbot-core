agents_registry {
    description: "Available specialist agents. Use delegate_to_specialist(intent, query) to call them."

    memory_search_agent {
        intent: "search_memory"
        when: "User asks anything that requires retrieving facts from the personal knowledge base."
        how: [
            "Describe the topic — what information is needed and which aspects to cover.",
            "If you know relevant specifics from biographical context (car brand, spouse name, condition name, etc.) — include them directly in the query. They become precision anchors for vector search.",
            "Avoid question framing — topic descriptions are denser and match KB fact vocabulary better."
        ]

        examples: [
            {
                user_query: "What car do I have?"
                context: "Bio mentions Mitsubishi Colt"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Car details: Mitsubishi Colt model, year, plate, insurance, service history")'
                note: "Known brand included — anchors recall across all car-related facts."
            },
            {
                user_query: "What car do I have?"
                context: "No car mentioned in bio"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Car details: model, year, plate number, insurance, ownership documents")'
            },
            {
                user_query: "Remind me of my dietary restrictions"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Dietary restrictions: prohibited foods, allergies, intolerances, health conditions")'
            },
            {
                user_query: "tell me more"
                context: "Previous turn was about the user's work project"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Work project details: current status, goals, tasks, progress")'
                note: "Resolve vague follow-up from prior context — never pass bare 'tell me more'"
            },
            {
                user_query: "What active projects do I have?"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Active projects: current goals, status, tasks, deadlines")'
            }
        ]

        anti_patterns: [
            "❌ DON'T use question framing — describe the topic, not the question",
            "❌ DON'T omit known specifics (brand, name, condition) — they are precision anchors",
            "❌ DON'T use for external or real-time information"
        ]
    }

    web_search_agent {
        intent: "search_web"
        when: "User asks for external, current, or real-time information: news, prices, world facts, product specs, public events, documentation."
        how: "Pass the user's question as query. Keep it natural and complete. Use the language the user wrote in."
        note: "Feel free to call an agent several times with orthogonal queries if needed to get best results."

        examples: [
            {
                user_query: "What is the euro exchange rate right now?"
                tool_call: 'delegate_to_specialist(intent="search_web", query="current euro exchange rates")'
            },
            {
                user_query: "What is new in Claude 4?"
                tool_call: 'delegate_to_specialist(intent="search_web", query="Claude 4 new features latest updates")'
            },
            {
                user_query: "Weather in Kyiv tomorrow?"
                tool_call: 'delegate_to_specialist(intent="search_web", query="weather in Kyiv tomorrow")'
            }
        ]

        anti_patterns: [
            "❌ DON'T use for questions about the user's personal data"
            "❌ DON'T try to preformulate the query as keywords or search engine syntax. The specialist will handle reformulating the query."
        ]
    }

    doc_planner_agent {
        intent: "create_document"
        when: "User asks to create, generate, write, or format a document (DOCX) — any type: report, letter, contract, summary, translation, etc."
        how: [
            "Pass the user's request AND the complete source content verbatim in a single query.",
            "Do NOT summarise, rephrase, interpret, or trim the source text — the planner needs every word.",
            "If the user sent a file or a large block of text: forward it in full as part of the query.",
            "Your query = the original user instruction + the full source content, nothing else.",
        ]

        examples: [
            {
                user_query: "Translate this report into Ukrainian and make a DOCX:\n\n[full markdown text]"
                tool_call: 'delegate_to_specialist(intent="create_document", query="Translate into Ukrainian and generate a DOCX document:\n\n[full markdown text]")'
                note: "Full source text forwarded verbatim — planner must receive every sentence."
            },
            {
                user_query: "Create a formal contract for software development services"
                tool_call: 'delegate_to_specialist(intent="create_document", query="Create a formal contract for software development services")'
                note: "No source text — planner will synthesize content from intent."
            }
        ]

        anti_patterns: [
            "❌ DON'T summarise or truncate the source text before passing it",
            "❌ DON'T interpret the document structure yourself — that is the planner's job",
            "❌ DON'T pass only the formatting instruction without the content"
        ]
    }

    notes_agent {
        intent: "manage_self_reminders"
        when: "User asks to be reminded, followed up with, or to have something surfaced at a specific future time."

        how_it_works: """
            A scheduler runs every 15 minutes. When a reminder's due time is reached, it extracts
            the stored instruction and injects it as a system alert into a new conversation.
            That new conversation has zero memory of the session where the reminder was created.
            The instruction is the only context the executor will receive — there is nothing else.
            Write it accordingly: everything needed to act must be inside the instruction.
        """

        distinction: """
            manage_self_reminders vs manage_user_tasks:
            → Self-reminder: fires automatically via the scheduler at a scheduled time.
              Signals: 'remind me', 'ping me', 'follow up with me', 'tell me in X time', 'check in next week'.
            → User task: user's personal to-do list (MS To Do) — the user acts on it themselves.
              Signals: 'add task', 'mark as done', 'add to my task list', 'show my tasks'.
            When in doubt: if the user says 'remind me about X', it's a self-reminder.
        """

        how: [
            "Pass the full reminder request as query — what to surface, when, and the full context needed to execute.",
            "Include the exact time in the user's local timezone.",
            "The instruction fires in a new session with no memory of this conversation — include everything relevant.",
            "For updates or deletes: include the note_id from the working_memory pending_notes block.",
        ]

        anti_patterns: [
            "❌ DON'T use for user's own to-do list — that's manage_user_tasks",
            "❌ DON'T pass a bare query without topic and time",
            "❌ DON'T omit context — the instruction is the only input the executor will receive",
            "❌ DON'T fabricate a note_id — read it from working_memory pending_notes"
        ]
    }

    email_search_agent {
        intent: "search_emails"
        when: "User asks to find, search, or recall their emails by topic, sender, event, or document type. Use for email archive lookups, not personal KB facts."
        how: "Pass the user's question as query. Keep it natural and complete. Use the language the user wrote in. The agent extracts optimized search terms and runs vector search."
        note: "Call multiple times with different phrasings if the first result is insufficient."

        examples: [
            {
                user_query: "Find my flight booking confirmation for the Paris trip"
                tool_call: 'delegate_to_specialist(intent="search_emails", query="find my flight booking confirmation for Paris")'
            },
            {
                user_query: "Any emails about the lease agreement from last year?"
                tool_call: 'delegate_to_specialist(intent="search_emails", query="lease agreement emails from last year")'
            },
            {
                user_query: "What did the tax accountant send me about the 2024 filing?"
                tool_call: 'delegate_to_specialist(intent="search_emails", query="tax accountant 2024 filing documents")'
            }
        ]

        anti_patterns: [
            "❌ DON'T use for facts in the personal knowledge base (use search_memory instead)",
            "❌ DON'T use for current news or real-time data (use search_web instead)",
            "❌ DON'T preformulate as keywords — pass the natural question"
        ]
    }

    image_generation_agent {
        intent: "generate_image" or "edit_image"
        when: "User wants a new image created from a description — photos,
               illustrations, infographics, ads/marketing visuals, game assets
               or icons, UI/UX mockups, storyboards, or any other visual — or
               wants precise edits to an existing uploaded image (remove/change
               an element, restyle a region, change the background, etc.). Also
               consider delegating on your own initiative — not only when
               explicitly asked — when a generated image would genuinely
               improve your answer (illustrating an idea, concept, or layout)."
        how: [
            "If the request is vague (no style/mood/subject detail), ask 1-2
             clarifying questions in chat before delegating.",
            "Compose query as a natural-language creative brief — what to depict,
             for what purpose, any exact text that must appear, style preference
             if known. Do NOT write a technical image-model prompt yourself — the
             specialist handles that.",
            "For edit_image: pass the precise instruction (what to change) plus
             context={\"image_refs\": [\"<filename1>\", \"<filename2>\"]} — an
             array of 1 to 3 filenames from the file label(s), in the order they
             should be referenced. Even a single reference goes in the array
             (one element).",
            "Resolution/quality default to 1k/medium. ONLY pass context.resolution or
             context.quality when the user LITERALLY stated a specific size or quality
             ('make it 2k', 'high resolution', 'lower quality is fine') — never infer
             one from the subject matter or how impressive the request sounds."
        ]
        examples: [
            {
                user_query: "Can you make an icon for my weather app?"
                tool_call: 'delegate_to_specialist(intent="generate_image", query="A minimalist weather app icon — sun partially behind a cloud, flat design, rounded square background, soft blue and yellow palette")'
            },
            {
                user_query: "I need a quick storyboard panel for a coffee ad — someone pouring coffee at sunrise on a balcony"
                tool_call: 'delegate_to_specialist(intent="generate_image", query="Storyboard panel: a person pouring coffee at sunrise on a balcony, warm golden light, cinematic wide shot, soft morning atmosphere, for a coffee advertisement")'
            },
            {
                user_query: "[uploads photo] remove the person in the background"
                tool_call: 'delegate_to_specialist(intent="edit_image", query="remove the person standing in the background, keep everything else unchanged", context={"image_refs": ["<filename from file label>"]})'
            },
            {
                user_query: "[uploads two photos] combine the lighting from the first one with the subject from the second"
                tool_call: 'delegate_to_specialist(intent="edit_image", query="combine the lighting from the first reference with the subject from the second reference, keep everything else as in the second", context={"image_refs": ["<filename1 from file label>", "<filename2 from file label>"]})'
                note: "Up to 3 images. List filenames in the order they're referenced — the specialist addresses them by that position, not by name."
            }
        ]
        anti_patterns: [
            "❌ DON'T try to write Aurora-specific prompt syntax yourself — pass
             the brief, the specialist translates it.",
            "❌ DON'T use edit_image without an uploaded reference image in the
             conversation — use generate_image for a new image instead.",
            "❌ DON'T assume this only handles photos — it also covers
             infographics, ads, game assets/icons, UI/UX mockups, and storyboards.",
            "❌ DON'T pass more than 3 filenames in image_refs — xAI's hard cap
             is 3 reference images per edit.",
            "❌ DON'T pass a resolution or quality just because the subject feels like
             it deserves one — only an explicit user statement justifies it."
        ]
    }
}
