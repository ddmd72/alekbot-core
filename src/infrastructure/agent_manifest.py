"""
Agent Manifest
==============

Single source of truth for all agent declarations and intent names.

Intent:
    Typed constants for all intent strings used across the system.
    Import from here instead of using raw string literals.

AgentDescriptor instances:
    Every agent in the system — specialist or orchestrator — has a descriptor here.
    Each descriptor declares both halves:
      Part A (capabilities): intents offered, descriptions, internal flag
      Part B (requirements): allowed_intents filter, intent_remap at dispatch time

    Orchestrator descriptors (QUICK_RESPONSE, SMART_RESPONSE) are class-level attributes
    in their respective agent classes. agent_id in orchestrator descriptors is a static
    type-level identifier — coordinator never routes TO orchestrators via registry.

ALL_DESCRIPTORS:
    Specialist descriptors imported by main.py and registered into AgentRegistry at startup.
    Orchestrator descriptors are NOT in ALL_DESCRIPTORS — they are not registered in the
    registry (coordinators route to them directly by agent_id, not via intent lookup).

To add a new specialist agent:
    1. Add Intent constant below.
    2. Add AgentDescriptor and include it in ALL_DESCRIPTORS.
    3. Wire the agent class in user_agent_factory.py.
"""

from .agent_registry import AgentDescriptor, ExecutionMode


class Intent:
    """Typed constants for all agent intent names."""
    SEARCH_MEMORY        = "search_memory"
    SAVE_TO_MEMORY       = "save_to_memory"
    SEARCH_WEB           = "search_web"
    FETCH_URL            = "fetch_url"
    SEARCH_WEB_LIGHT     = "search_web_light"
    SEARCH_EMAILS        = "search_emails"
    GET_EMAIL_DETAILS    = "get_email_details"
    GET_EMAIL_ATTACHMENT = "get_email_attachment"
    MAPS_QUERY           = "maps_query"
    DEEP_RESEARCH        = "deep_research"
    COMPUTE_MATH         = "compute_math"
    COMPUTE_DATETIME     = "compute_datetime"
    COMPUTE_FINANCE      = "compute_finance"
    COMPUTE              = "compute"
    # Consolidation intents (internal=True — not exposed to LLM tool selection)
    CONSOLIDATE          = "consolidate"
    CONSOLIDATE_CLUSTER  = "consolidate_cluster"
    CONSOLIDATE_EMAIL    = "consolidate_email"
    CONSOLIDATE_FULL     = "consolidate_full"
    # Claude deep research runner (internal=True — invoked via agent_execution Cloud Task only)
    EXECUTE_DEEP_RESEARCH_CLAUDE = "execute_deep_research_claude"
    # Tasks management — single entry point; agent selects CRUD tool internally
    MANAGE_USER_TASKS = "manage_user_tasks"
    # Orchestrator notepad — short-lived cross-turn notes
    MANAGE_SELF_REMINDERS = "manage_self_reminders"
    # Document creation — produces a DOCX file attachment
    CREATE_DOCUMENT = "create_document"
    # Internal: LLM writes Node.js docx script, Python executes it. Called by DocPlannerAgent only.
    GENERATE_DOCX_CODE = "generate_docx_code"
    # PDF creation — produces HTML + PDF delivered via GCS + Slack file upload
    CREATE_PDF = "create_pdf"
    # HTML page creation — produces a single-page HTML layout delivered via GCS public link
    CREATE_HTML_PAGE = "create_html_page"
    # User-facing guide — returns what the system can do for the end user
    USER_GUIDE = "user_guide"


# ---------------------------------------------------------------------------
# Specialist agents — registered in AgentRegistry via ALL_DESCRIPTORS
# ---------------------------------------------------------------------------

MEMORY_SEARCH = AgentDescriptor(
    agent_id="facts_memory_agent",
    agent_type="facts_memory",
    capabilities={
        Intent.SEARCH_MEMORY: ExecutionMode.SYNC,
        Intent.SAVE_TO_MEMORY: ExecutionMode.SYNC,
    },
    description="Personal knowledge base: search and save facts",
    capability_descriptions={
        Intent.SEARCH_MEMORY: (
            "Semantic search across the personal knowledge base — "
            "biographical facts, projects, health, family, possessions, preferences"
        ),
        Intent.SAVE_TO_MEMORY: (
            "Save a specific fact or piece of information to long-term memory. "
            "Call only when the user explicitly asks to save or remember something. "
            "query: brief task description (what to save). "
            "context.text: detailed, self-contained fact passage — all circumstances, dates, "
            "names, conditions. Must be understandable without any prior conversation context."
        ),
    },
    context_schemas={
        Intent.SAVE_TO_MEMORY: {
            "text": (
                "Detailed, self-contained description of the fact to save. "
                "Include every relevant detail: what, when, conditions, numbers, names, "
                "surrounding circumstances. Write in third person ('User...'). "
                "Must be fully understandable without the surrounding conversation."
            ),
        },
    },
)

WEB_SEARCH = AgentDescriptor(
    agent_id="web_search_agent",
    agent_type="web_search",
    capabilities={
        Intent.SEARCH_WEB: ExecutionMode.SYNC,
        Intent.FETCH_URL:  ExecutionMode.SYNC,
    },
    description="Real-time web search and URL fetching",
    capability_descriptions={
        Intent.SEARCH_WEB: (
            "Real-time web search — "
            "news, prices, weather, world facts, public events, documentation"
        ),
        Intent.FETCH_URL: (
            "Fetch the content of a specific URL and return it in full detail. "
            "Use when the user provides a URL and asks to read, summarise, or analyse its content. "
            "payload: {\"url\": \"<URL to fetch>\", \"query\": \"<natural language description of what to find or extract on the page — required>\"}"
        ),
    },
)

# internal=True: never shown to LLMs directly.
# QuickAgent dispatches here via QUICK_RESPONSE.intent_remap.
WEB_SEARCH_LIGHT = AgentDescriptor(
    agent_id="web_search_light_agent",
    agent_type="web_search_light",
    capabilities={Intent.SEARCH_WEB_LIGHT: ExecutionMode.SYNC},
    description="Lightweight real-time web search (ECO tier)",
    internal=True,
)

MAPS_SEARCH = AgentDescriptor(
    agent_id="maps_search_agent",
    agent_type="maps_search",
    capabilities={Intent.MAPS_QUERY: ExecutionMode.SYNC},
    description="Place search, route computation, and weather lookup via Google Maps",
    capability_descriptions={
        Intent.MAPS_QUERY: (
            # Backend: MCPMapsAdapter (Google Maps AI Grounding Lite).
            # Update this description when switching backends (see MCP_INFRASTRUCTURE_RFC.md § 8).
            "Place search and discovery, route computation (distance and duration — "
            "not turn-by-turn directions or real-time traffic), and current weather "
            "lookup via Google Maps AI Grounding Lite. "
            "For place results, the agent delivers clickable Google Maps links "
            "(place card, directions, reviews, photos) — best practice for user convenience. "
            "Input: A natural language task. "
            "Examples: 'pharmacy near Khreschatyk open now', "
            "'route from Kyiv to Lviv by car', 'weather in Odesa today', "
            "'best sushi in Kyiv city center'. "
            "payload: {\"query\": \"<natural language task>\"}"
        ),
    },
    internal=False,
)

EMAIL_SEARCH = AgentDescriptor(
    agent_id="email_search_agent",
    agent_type="email_search",
    capabilities={
        Intent.SEARCH_EMAILS:        ExecutionMode.SYNC,
        Intent.GET_EMAIL_DETAILS:    ExecutionMode.SYNC,
        Intent.GET_EMAIL_ATTACHMENT: ExecutionMode.SYNC,
    },
    description="Email archive specialist",
    capability_descriptions={
        Intent.SEARCH_EMAILS: (
            "Semantic search across the user's indexed email archive "
            "by topic, sender, date, or document type. "
            "Returns a list of emails with their ids and attachment filenames."
        ),
        Intent.GET_EMAIL_DETAILS: (
            "Fetch full body of a specific email. "
            "Use after search_emails to read the content of a found email. "
            'Requires: context={"email_id": "<id from search_emails result>"}'
        ),
        Intent.GET_EMAIL_ATTACHMENT: (
            "Extract and read an email attachment as text (PDF, DOCX, etc.). "
            "Use after search_emails when the email has an attachment. "
            'Requires: context={"email_id": "<id from search_emails result>", "filename": "<filename from search_emails result>"}'
        ),
    },
    context_schemas={
        Intent.GET_EMAIL_DETAILS: {
            "email_id": "Email message ID from a prior search_emails result",
        },
        Intent.GET_EMAIL_ATTACHMENT: {
            "email_id": "Email message ID from a prior search_emails result",
            "filename": "Exact attachment filename from a prior search_emails result",
        },
    },
)

COMPUTE = AgentDescriptor(
    agent_id="compute_agent",
    agent_type="compute",
    capabilities={
        Intent.COMPUTE_MATH:     ExecutionMode.SYNC,
        Intent.COMPUTE_DATETIME: ExecutionMode.SYNC,
        Intent.COMPUTE_FINANCE:  ExecutionMode.SYNC,
        Intent.COMPUTE:          ExecutionMode.SYNC,
    },
    description="Precise computation via Python code execution",
    capability_descriptions={
        Intent.COMPUTE_MATH: (
            "Precise arithmetic, algebra, equations, unit conversions "
            "(km to miles, kg to lbs, liters to gallons, celsius to fahrenheit). "
            "Executes Python code — use instead of computing in-head. "
            "ONLY computes what you tell it. Does NOT search or fetch external data. "
            'payload: {"query": "<expression or question>"}'
        ),
        Intent.COMPUTE_DATETIME: (
            "Date and time calculations — differences between dates, day-of-week, "
            "age, timezone conversions, countdowns (days until X). "
            "Has access to current datetime. Use for ANY date arithmetic. "
            "ONLY computes — does NOT look up holidays, events, or external calendars. "
            'payload: {"query": "<question about dates/time>"}'
        ),
        Intent.COMPUTE_FINANCE: (
            "Financial calculations: loan/mortgage payments, compound interest, "
            "investment returns, amortization schedules, tax formulas. "
            "ONLY computes with numbers YOU provide. Has NO access to live rates, "
            "stock prices, or market data — for those use search_web instead. "
            'payload: {"query": "<financial calculation with all numbers provided>"}'
        ),
        Intent.COMPUTE: (
            "General-purpose computation that does not fit math/datetime/finance "
            "categories — statistics, BMI, calorie estimates, scoring, ranking, "
            "any numeric analysis. Executes Python code in sandbox. "
            "ONLY computes — does NOT search, fetch, or access external data. "
            'payload: {"query": "<computation request>"}'
        ),
    },
)

DEEP_RESEARCH_AGENT = AgentDescriptor(
    agent_id="deep_research_agent",
    agent_type="deep_research",
    capabilities={Intent.DEEP_RESEARCH: ExecutionMode.SYNC},
    description="Autonomous deep research",
    capability_descriptions={
        Intent.DEEP_RESEARCH: (
            "Executes 80–160 searches over 5–60 minutes and returns a cited long-form report "
            "as a public HTML link. "
            "Use ONLY when the user has explicitly requested deep research and confirmed "
            "the research brief. NOT for quick facts, NOT for regular web search. "
            "Result is delivered asynchronously as a link — inform the user it will arrive separately. "
            "payload: {\"query\": \"<complete research brief>\", \"language\": \"<language>\", "
            "\"brief\": \"<one-sentence summary of the research request, max 400 chars — "
            "used as confirmation to the user and stored as metadata>\"}"
        ),
    },
    internal=False,
)

TASKS = AgentDescriptor(
    agent_id="tasks_agent",
    agent_type="tasks",
    capabilities={
        Intent.MANAGE_USER_TASKS: ExecutionMode.SYNC,
    },
    description="User's personal task list — create, view, search, update, complete, and delete tasks.",
    capability_descriptions={
        Intent.MANAGE_USER_TASKS: (
            "Manage the user's personal task list (MS To Do). "
            "payload: {\"query\": \"<delegation>\", \"context\": \"<optional background>\"}\n"
            "\n"
            "Write query as a natural language instruction — not a command. "
            "Include everything you know: what the user wants, what the task is about, "
            "all dates and times as mentioned, any relevant context. "
            "The agent is an LLM and will understand — give it the full picture, not a template.\n"
            "\n"
            "Write the delegation in the same language you use to respond to the user."
        ),
    },
    internal=False,
)


# internal=True: never shown to LLMs; invoked only via agent_execution Cloud Task.
# Per-user instances are created by UserAgentFactory as claude_deep_research_runner_{user_id}.
# NOT in ALL_DESCRIPTORS — UserAgentFactory registers instances directly.
CLAUDE_DEEP_RESEARCH_RUNNER = AgentDescriptor(
    agent_id="claude_deep_research_runner",
    agent_type="claude_deep_research_runner",
    capabilities={Intent.EXECUTE_DEEP_RESEARCH_CLAUDE: ExecutionMode.ASYNC},
    description="Claude deep research executor — runs multi-turn loop and self-delivers result",
    internal=True,
)

# internal=True: never shown to LLMs; routed directly by recipient ID, not via intent lookup.
# Per-user instances are created by UserAgentFactory as consolidation_agent_{user_id}.
# NOT in ALL_DESCRIPTORS — UserAgentFactory registers instances directly.
CONSOLIDATION_AGENT = AgentDescriptor(
    agent_id="consolidation_agent",
    agent_type="consolidation",
    capabilities={
        Intent.CONSOLIDATE:         ExecutionMode.SYNC,
        Intent.CONSOLIDATE_CLUSTER: ExecutionMode.SYNC,
        Intent.CONSOLIDATE_EMAIL:   ExecutionMode.SYNC,
        Intent.CONSOLIDATE_FULL:    ExecutionMode.SYNC,
    },
    description="Background memory consolidation",
    internal=True,
)


# ---------------------------------------------------------------------------
# Orchestrator agents — NOT registered in AgentRegistry.
# Set as class-level _descriptor in their agent classes.
# ---------------------------------------------------------------------------

QUICK_RESPONSE = AgentDescriptor(
    agent_id="quick_response_agent",
    agent_type="quick_response",
    capabilities={},        # Quick does not offer intents to other agents
    allowed_intents=None,   # can call all non-internal intents
    intent_remap={Intent.SEARCH_WEB: Intent.SEARCH_WEB_LIGHT},
)

SMART_RESPONSE = AgentDescriptor(
    agent_id="smart_response_agent",
    agent_type="smart_response",
    capabilities={},        # Smart does not offer intents to other agents
    allowed_intents=None,   # can call all non-internal intents
    intent_remap={},
)


NOTES = AgentDescriptor(
    agent_id="notes_agent",
    agent_type="notes",
    capabilities={
        Intent.MANAGE_SELF_REMINDERS: ExecutionMode.SYNC,
    },
    description="Full CRUD management of proactive self-reminders: create, update, delete, and list reminders that fire automatically at a scheduled time",
    capability_descriptions={
        Intent.MANAGE_SELF_REMINDERS: (
            "Full CRUD for proactive self-reminders (create, update, delete, list). "
            "Reminders fire automatically on schedule; each carries a self-contained instruction "
            "executed in a new conversation with no session memory.\n"
            "payload: {\"query\": \"<briefing: operation, timing, full context, note_id if updating/deleting>\"}"
        ),
    },
    internal=False,
)


DOC_PLANNER = AgentDescriptor(
    agent_id="doc_planner_agent",
    agent_type="doc_planner",
    capabilities={Intent.CREATE_DOCUMENT: ExecutionMode.ASYNC},
    description="Creates structured Word documents (DOCX) from natural language requests",
    capability_descriptions={
        Intent.CREATE_DOCUMENT: (
            "Creates a Word document (DOCX) delivered as a file attachment. "
            "payload: {\"query\": \"<user instruction + full source content verbatim>\"}"
        ),
    },
    internal=False,
    dispatch_deadline_s=720,  # 600s agent timeout + 2 min overhead
)

# internal=True: never shown to LLMs. Called only by DocPlannerAgent via coordinator.
DOC_GENERATOR = AgentDescriptor(
    agent_id="doc_generator_agent",
    agent_type="doc_generator",
    capabilities={Intent.GENERATE_DOCX_CODE: ExecutionMode.ASYNC},
    description="LLM-driven DOCX code generation via Node.js subprocess",
    internal=True,
    dispatch_deadline_s=720,  # 600s agent timeout + 2 min overhead
)

PDF_GENERATOR = AgentDescriptor(
    agent_id="pdf_generator_agent",
    agent_type="pdf_generator",
    capabilities={Intent.CREATE_PDF: ExecutionMode.ASYNC},
    description="Creates professional PDF documents from natural language requests",
    capability_descriptions={
        Intent.CREATE_PDF: (
            "Creates a professional PDF document — proposals, reports, memos, briefs, "
            "summaries, contracts, manuals, or any other formal document. "
            "The result is delivered as a PDF file in the chat and stored in the cloud. "
            "Use when the user explicitly requests a PDF document or formatted file. "
            "payload: {\"query\": \"<document creation request with all relevant context>\"}"
        ),
    },
    internal=False,
    dispatch_deadline_s=720,  # 600s agent timeout + 2 min overhead
)


HELP = AgentDescriptor(
    agent_id="help_agent",
    agent_type="help",
    capabilities={Intent.USER_GUIDE: ExecutionMode.SYNC},
    description="System capabilities reference",
    capability_descriptions={
        Intent.USER_GUIDE: (
            "Returns a structured list of everything this system can do — "
            "all available agents and their capabilities. "
            "Call when the user asks what you can do, what features are available, "
            "or how to use the system. "
            'payload: {"query": "<user\'s question about capabilities>"}'
        ),
    },
    internal=False,
)


HTML_PAGE_GENERATOR = AgentDescriptor(
    agent_id="html_page_generator_agent",
    agent_type="html_page",
    capabilities={Intent.CREATE_HTML_PAGE: ExecutionMode.ASYNC},
    description="Creates production-grade single-page HTML layouts",
    capability_descriptions={
        Intent.CREATE_HTML_PAGE: (
            "Creates a professional single-page HTML layout — landing pages, product showcases, "
            "portfolios, documentation pages, dashboards, or any visual web page. "
            "Result delivered as a public link. Mobile-responsive with animations. "
            "Use when the user asks for an HTML page, web page, landing page, or visual layout. "
            "payload: {\"query\": \"<page creation request with all relevant context>\"}"
        ),
    },
    internal=False,
    dispatch_deadline_s=720,  # 600s agent timeout + 2 min overhead
)


ALL_DESCRIPTORS = [
    MEMORY_SEARCH,
    WEB_SEARCH,
    WEB_SEARCH_LIGHT,
    EMAIL_SEARCH,
    MAPS_SEARCH,
    COMPUTE,
    DEEP_RESEARCH_AGENT,
    TASKS,
    NOTES,
    DOC_PLANNER,
    DOC_GENERATOR,
    PDF_GENERATOR,
    HTML_PAGE_GENERATOR,
    HELP,
]
