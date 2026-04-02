"""User-facing guide — what the system can do for the end user.

Returned by the user_guide intent (HelpAgent). SmartAgent re-presents
this in the user's language via LANG_MIRROR/LANG_FIXED.

Update this file when adding new agents or cabinet features.
Referenced from: CLAUDE.md, README.md
"""

CAPABILITIES_TEXT = """
*What I can do for you*

---

*Memory*
I build a personal knowledge base about you over time — automatically from our conversations and from your emails (if connected). I use this context in every response.

How to use:
- Ask me to save something explicitly: "remember that my mortgage rate is 3.2%"
- Search what I know: "what do you know about my car?", "find everything about my apartment renovation"
- I automatically pick up important facts from conversations — you don't need to instruct me to remember everything

Your facts are visible in Cabinet → Memory, where you can browse, search, filter by category, and delete individual entries.

---

*Web search and URL reading*
- Real-time search: news, prices, weather, public events, documentation — anything requiring current information
- Reading a URL: paste a link and ask me to summarise, extract data, or answer a specific question about its content

---

*Email* (requires Gmail connection in Cabinet → Integrations)
I index your inbox and make it fully searchable. Three levels of access:
1. Search: "find emails from my accountant about the 2024 tax return" — I search by topic, sender, date, content
2. Full email: "show me the full text of that email" — I fetch the complete body
3. Attachment: "read the PDF attached to that invoice" — I extract and parse the content (PDF, DOCX, and other formats)

These three steps can chain in one conversation — search → read full email → read attachment.

*Daily email digest* — every morning I send you a structured review of the past 24 hours in your inbox: key emails, action items, things requiring attention. Enable and set the time in Cabinet → Integrations → Gmail → Daily review.

---

*Maps and weather*
- Places: "pharmacy near Plaza Mayor open now", "best sushi in Valencia city center"
- Routes: "distance from Valencia to Madrid by car", "how long does it take to drive to the airport"
- Weather: "weather in Barcelona today"
Results include clickable Google Maps links with directions, reviews, and hours.

---

*Calculations*
I run exact computations in a Python sandbox — no rounding errors, no guessing.
- Math and conversions: "what is 17% of €4,380?", "convert 180 cm to feet and inches"
- Dates and time: "how many days until 15 August?", "what day of the week was I born if my birthday is 3 March 1990?", "time difference between Valencia and Tokyo right now"
- Finance: "monthly payment on a €250,000 mortgage at 3.5% over 25 years", "compound interest on €10,000 at 4.5% over 10 years"
- General: "calculate my BMI — 82 kg, 181 cm", "average of these numbers: 14, 27, 33, 8"

---

*Tasks* (requires Microsoft To Do or Google Tasks connection in Cabinet → Integrations)
I manage your task list through natural conversation — create, search, update, complete, and delete tasks.

Examples:
- "Add a task: call the insurance company before Friday"
- "What tasks do I have this week?"
- "Complete the budget review task"
- "Remind me every weekday to check emails at 9am" — creates a recurring task

Recurrence options: daily, weekdays (Mon–Fri), weekly on specific days, monthly, yearly.
If you set a due date without specifying a reminder time, I automatically set a reminder for the evening before.

---

*Self-reminders*
I can set reminders that fire on schedule and start a new conversation automatically — not just a notification, but an actual action executed on your behalf.

Examples:
- "Remind me every Monday morning to review my weekly goals"
- "In 3 hours, remind me to check on the delivery"
- "Every first of the month, remind me to reconcile my expenses"

Recurrence options: one-time, hourly, daily, weekly, monthly.
When a reminder fires, I act on the instruction you wrote — I don't just ping you, I execute the task in a new conversation.
After every create/update/delete you receive an immediate confirmation message.

⚠️ I also use this mechanism for myself. I may set, update, or delete reminders on my own initiative — for example, to follow up on something we discussed, to check on a task I delegated, or to remind myself about something time-sensitive. This is intentional proactive behaviour, not a bug. You will always receive a notification when I do this, so you can delete or modify the reminder if you don't want it.

---

*File attachments*
Send me files directly in the chat — documents, images, PDFs, spreadsheets. I store them securely and can:
- Read and analyse the content on any subsequent turn (no need to re-upload)
- Use files as source material for document generation (PDF, DOCX, HTML page) — e.g. "create a PDF from this file"
- Delete files when you no longer need them

Files are kept for 90 days. Supported: anything that can be converted to text (DOCX, PDF, CSV, TXT, XLSX, etc.) plus images for visual analysis.

---

*Documents*
I create formatted documents delivered directly in the chat.

- *Word document (DOCX)*: "write a formal complaint letter to my landlord about the broken heating and send it as a Word file" — I generate the document and deliver it as a file
- *PDF*: "create a professional invoice PDF for €2,400 consulting services in March" — I generate and deliver both an HTML preview link and a PDF file
- *HTML page*: "make a visual one-page summary of my investment portfolio with charts" — delivered as a public link; I automatically source real photos for visual content when relevant

All three are generated asynchronously — I confirm the request and the file arrives shortly after.

---

*Deep research*
For questions that require thorough investigation across many sources over an extended period.

How it works:
1. Tell me the topic you want researched
2. I clarify the scope with you and present the research brief for confirmation
3. After you confirm, I start the research job in the background
4. The result arrives as a structured, cited HTML report via a public link — you can continue using me normally while it runs

Use for: market analysis, comparing options in depth, background research on a topic, comprehensive how-to guides, competitor analysis.

Note: this takes time — plan accordingly and don't use it for quick factual questions (use regular web search instead).

---

*Cabinet — what you can configure at your personal dashboard*

*Integrations tab*

Gmail:
1. Click "Connect Gmail" → authorise via Google → you'll return to Cabinet with confirmation
2. Index emails: click "Index Emails" → choose mode:
   - Incremental (new only) — fastest, only emails since last sync
   - Backfill (to specific date) — index backward to a date you choose
   - Re-index everything — full re-scan of all emails
   Click "Start Indexing" → indexing runs in background, you can leave the page
3. Auto-index: check "Auto-index Gmail" → select hour → saves automatically; runs daily at that time in your timezone
4. Daily review: check "Daily review" → select hour → I send you an inbox digest each morning at that time
5. Disconnect: "Disconnect" button → confirms via dialog → OAuth revoked, indexed data preserved
6. Delete indexed data: "Delete Data" link → permanently removes all email facts from memory

Microsoft To Do:
1. Click "Connect Microsoft To Do" → authorise via Microsoft account → return to Cabinet
2. I can now read and manage your tasks from Slack/Telegram
3. Disconnect: "Disconnect account" → confirm in dialog

Google Tasks:
1. Click "Connect Google Tasks" → authorise via Google account → return to Cabinet
2. Disconnect when needed from the same section

*Memory tab*
- Browse all facts I know about you — displayed as cards with domain labels (Email, Chat, Profile, etc.)
- Filter by domain: click a domain chip to show only facts from that category
- Search: type in the search bar and press Enter — semantic search across your entire knowledge base
- Delete a fact: click "Delete" on any card → confirm → fact is permanently removed
- Correct a fact: click "Edit" → enter the correction → copy the generated message → paste it to me in chat and I'll update the fact

*Settings tab*

Timezone:
1. Click the timezone dropdown
2. Select from the list (organised by region: Europe, Americas, Asia & Pacific, UTC)
3. Click "Save" — used for scheduling reminders, auto-indexing times, and interpreting time expressions

Language:
1. UI Language — select the language for the cabinet interface itself (Ukrainian, English, French, Spanish, or system default)
2. Bot response language:
   - "Mirror input" — I respond in the same language you write in (default)
   - "Fixed language" — I always respond in the language selected above, regardless of your input language
3. Click "Save" to apply

Bot Reminders:
- View all your scheduled reminders (both self-set and bot-created)
- Create new reminders: set a label, instruction (what the bot should do), due date/time, and optionally a recurrence (hourly, daily, weekly, monthly)
- Edit existing reminders: change any field
- Delete reminders you no longer need

*Team tab* (account owner only)
1. Click "Generate New Invite Link" → enter the person's email → an invite code and shareable link are generated
2. Copy the link and send it to them
3. They open the link, complete signup, and join your account
4. Monitor invite status in the table below (Active / Used)
""".strip()
