/* The Reminders section (CABINET_REDESIGN_RFC §5.5): the list as rows (label, next run, repeat); a row opens to Edit
   and Delete; "+ New" and Edit open the one form (#reminder-form), which is moved into place: above the list for a
   new reminder, into the row's body for an edit. One form is open at a time because there is one form.
   Loaded after cabinet-sections.js (escapeHtml, esc, showToast, authFetch, API, t); the plain scripts share one
   global scope, so nothing here runs at load time.
   The indentation is the old inline script's, kept on purpose: the text tests find functions by it. */
      /* ── Bot Reminders ────────────────────────────────── */
      let _remindersData = [];
      let _remindersZone = null;       // the user's timezone name, as the list route returns it
      let _remFormFor = null;          // null: form closed; "": a new reminder; otherwise the note_id being edited

      // The recurrence presets of the form and the strings that name them in a row; any other rule is shown raw.
      const REM_PRESETS = {
        "FREQ=HOURLY": "rem.hourly",
        "FREQ=DAILY": "rem.daily",
        "FREQ=WEEKLY": "rem.weekly",
        "FREQ=MONTHLY": "rem.monthly",
      };

      // Selector → string key for the static markup of this section (the shell's map does not know these).
      const REM_STRINGS = {
        "#reminders-new": "rem.new",
        "#rem-label-text": "rem.f_label",
        "#rem-label-instruction": "rem.f_instruction",
        "#rem-label-due": "rem.f_due",
        "#rem-label-repeat": "rem.f_repeat",
        "#rem-label-rrule": "rem.f_rrule",
        "#rem-save": "btn.save",
        "#rem-cancel": "rem.cancel",
        '#rem-recurrence option[value=""]': "rem.once",
        '#rem-recurrence option[value="FREQ=HOURLY"]': "rem.hourly",
        '#rem-recurrence option[value="FREQ=DAILY"]': "rem.daily",
        '#rem-recurrence option[value="FREQ=WEEKLY"]': "rem.weekly",
        '#rem-recurrence option[value="FREQ=MONTHLY"]': "rem.monthly",
        '#rem-recurrence option[value="__custom__"]': "rem.custom",
      };

      function applyReminderStrings() {
        for (const [sel, key] of Object.entries(REM_STRINGS)) {
          const el = document.querySelector(sel);
          if (el) el.textContent = t(key);
        }
        document.getElementById("rem-text").placeholder = t("rem.ph_label");
        document.getElementById("rem-instruction").placeholder = t("rem.ph_instruction");
        document.getElementById("rem-rrule-help").innerHTML = escapeHtml(t("rem.rrule_help"))
          .replace("{a}", "<code>FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0</code>")
          .replace("{b}", "<code>FREQ=MONTHLY;BYDAY=-1SU</code>");
      }

      function _remNote(text) {
        return `<div class="c-rem-note">${escapeHtml(text)}</div>`;
      }

      async function loadReminders() {
        const list = document.getElementById("reminders-list");
        applyReminderStrings();
        try {
          const res = await authFetch(`${API}/api/user/reminders`, { credentials: "include" });
          if (res.status === 501) { _remPark(); list.innerHTML = _remNote(t("rem.unavailable")); return; }
          if (!res.ok) throw new Error();
          const data = await res.json();
          _remindersData = data.reminders || [];
          _remindersZone = data.timezone || null;
          renderReminders();
        } catch {
          _remPark();
          list.innerHTML = _remNote(t("rem.load_failed"));
        }
      }

      // A date in the user's own timezone (the one Settings holds), not the browser's.
      function _remWhen(iso) {
        if (!iso) return "—";
        const opts = { dateStyle: "medium", timeStyle: "short" };
        try {
          return new Date(iso).toLocaleString(undefined, _remindersZone ? { ...opts, timeZone: _remindersZone } : opts);
        } catch {
          return new Date(iso).toLocaleString(undefined, opts);
        }
      }

      // The value a datetime-local input takes for an instant: the wall clock of the user's timezone, because the
      // server reads the sent text as the user's own time.
      function _remInputValue(iso) {
        const d = new Date(iso);
        try {
          if (_remindersZone) {
            return new Intl.DateTimeFormat("sv-SE", {
              timeZone: _remindersZone, year: "numeric", month: "2-digit", day: "2-digit",
              hour: "2-digit", minute: "2-digit", hourCycle: "h23",
            }).format(d).replace(" ", "T");
          }
        } catch { /* an unknown zone name: fall back to the browser's clock below */ }
        return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      }

      function _remRepeat(rule) {
        if (!rule) return t("rem.once");
        return REM_PRESETS[rule] ? t(REM_PRESETS[rule]) : rule;
      }

      function renderReminders() {
        const list = document.getElementById("reminders-list");
        _remPark();                      // the form is a node of this page: take it out before the rows are rebuilt
        if (!_remindersData.length) {
          _remFormFor = _remFormFor === "" ? "" : null;
          list.innerHTML = _remNote(t("rem.empty"));
          _remPlace();
          return;
        }
        list.innerHTML = _remindersData.map(r => {
          const due = _remWhen(r.due);
          const rec = esc(_remRepeat(r.recurrence));
          const instr = esc(r.instruction || "").replace(/\n/g, "<br>");
          // AGENT_WORKSPACE_RFC §8.3: one read-only line; edited through chat.
          const fileLine = r.workspace_file
            ? `<p class="c-hint">workspace file: workspace:${esc(r.workspace_file)}</p>`
            : "";
          // A custom rule is shown raw in the row; the server's wording of it goes in the body.
          const isCustom = r.recurrence && !REM_PRESETS[r.recurrence] && r.recurrence_label;
          const ruleLine = isCustom ? `<p class="c-hint">${esc(r.recurrence_label)}</p>` : "";
          return `<div class="c-rem" data-note-id="${escapeHtml(r.note_id)}">
            <button type="button" class="c-row" aria-expanded="false" data-note-id="${escapeHtml(r.note_id)}" onclick="toggleReminderRow(this.dataset.noteId)">
              <span class="c-rem-main">
                <span class="c-row-title">${esc(r.text)}</span>
                <span class="c-rem-sub"><span>${esc(due)}</span><span class="c-rem-repeat">${rec}</span></span>
              </span>
              <svg class="c-chev" width="8" height="14" viewBox="0 0 8 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 1l6 6-6 6"/></svg>
            </button>
            <div class="c-row-body" hidden>
              <div class="c-rem-view">
                <p class="c-hint">${instr}</p>
                ${ruleLine}
                ${fileLine}
                <div class="c-btns">
                  <button type="button" class="c-btn c-sec" data-note-id="${escapeHtml(r.note_id)}" onclick="editReminder(this.dataset.noteId)">${esc(t("rem.edit"))}</button>
                  <button type="button" class="c-btn c-dng" data-note-id="${escapeHtml(r.note_id)}" onclick="deleteReminder(this.dataset.noteId)">${esc(t("rem.delete"))}</button>
                </div>
              </div>
              <div class="c-rem-slot"></div>
            </div>
          </div>`;
        }).join("");
        _remPlace();
      }

      function _remRow(noteId) {
        return [...document.getElementById("reminders-list").children].find(w => w.dataset && w.dataset.noteId === noteId) || null;
      }

      function _remSetRowOpen(wrap, open) {
        const btn = wrap.querySelector(".c-row");
        btn.classList.toggle("open", open);
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        wrap.querySelector(".c-row-body").hidden = !open;
      }

      function toggleReminderRow(noteId) {
        const wrap = _remRow(noteId);
        if (!wrap) return;
        const open = wrap.querySelector(".c-row-body").hidden;
        if (!open && _remFormFor === noteId) hideReminderForm();     // closing a row closes the form inside it
        _remSetRowOpen(wrap, open);
      }

      // Put the form where _remFormFor says: parked (closed), above the list (new), or in a row's body (edit).
      function _remPark() {
        const slot = document.getElementById("reminders-new-slot");
        const form = document.getElementById("reminder-form");
        slot.appendChild(form);
        form.hidden = true;
        slot.hidden = true;
        document.querySelectorAll("#reminders-list .c-rem.editing").forEach(w => w.classList.remove("editing"));
      }

      function _remPlace() {
        const slot = document.getElementById("reminders-new-slot");
        const form = document.getElementById("reminder-form");
        if (_remFormFor === null) { _remPark(); return; }
        if (_remFormFor === "") {
          _remPark();
          slot.hidden = false;
          form.hidden = false;
          return;
        }
        const wrap = _remRow(_remFormFor);
        if (!wrap) { _remFormFor = null; _remPark(); return; }
        _remPark();
        _remSetRowOpen(wrap, true);
        wrap.classList.add("editing");
        wrap.querySelector(".c-rem-slot").appendChild(form);
        form.hidden = false;
      }

      function showReminderForm(note) {
        applyReminderStrings();
        document.getElementById("rem-edit-id").value = note ? note.note_id : "";
        document.getElementById("rem-text").value = note ? note.text : "";
        document.getElementById("rem-instruction").value = note ? note.instruction : "";
        document.getElementById("rem-due").value = note && note.due ? _remInputValue(note.due) : "";
        const rule = note?.recurrence || "";
        const sel = document.getElementById("rem-recurrence");
        const preset = [...sel.options].some(o => o.value === rule && rule);
        sel.value = rule ? (preset ? rule : "__custom__") : "";
        document.getElementById("rem-rrule").value = preset ? "" : rule;
        onRecurrenceChange();
        _remFormFor = note ? note.note_id : "";
        _remPlace();
        document.getElementById("rem-text").focus();
      }

      function onRecurrenceChange() {
        const custom = document.getElementById("rem-recurrence").value === "__custom__";
        document.getElementById("rem-rrule-row").hidden = !custom;
      }

      function hideReminderForm() {
        _remFormFor = null;
        _remPark();
      }

      function editReminder(noteId) {
        const note = _remindersData.find(r => r.note_id === noteId);
        if (note) showReminderForm(note);
      }

      async function saveReminder() {
        const editId = document.getElementById("rem-edit-id").value;
        const text = document.getElementById("rem-text").value.trim();
        const instruction = document.getElementById("rem-instruction").value.trim();
        const due = document.getElementById("rem-due").value;
        const recSel = document.getElementById("rem-recurrence").value;
        const recurrence = recSel === "__custom__"
          ? document.getElementById("rem-rrule").value.trim()
          : recSel;
        if (!text || !instruction || !due) { showToast(t("rem.fill_all"), "error"); return; }
        if (recSel === "__custom__" && !recurrence) { showToast(t("rem.need_rule"), "error"); return; }
        // Always sent: an empty string is how the API is told to stop repeating.
        const body = { text, instruction, due, recurrence };
        try {
          const url = editId ? `${API}/api/user/reminders/${editId}` : `${API}/api/user/reminders`;
          const method = editId ? "PUT" : "POST";
          const res = await authFetch(url, { method, credentials: "include", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
          if (!res.ok) { const e = await res.json().catch(()=>({})); showToast(e.error || t("rem.failed"), "error"); return; }
          hideReminderForm();
          showToast(editId ? t("rem.updated") : t("rem.created"), "success");
          await loadReminders();
        } catch { showToast(t("rem.save_failed"), "error"); }
      }

      async function deleteReminder(noteId) {
        if (!confirm(t("rem.confirm_delete"))) return;
        try {
          const res = await authFetch(`${API}/api/user/reminders/${noteId}`, { method: "DELETE", credentials: "include" });
          if (!res.ok) { showToast(t("rem.delete_failed"), "error"); return; }
          if (_remFormFor === noteId) _remFormFor = null;     // the row the form sat in is gone
          showToast(t("rem.deleted"), "success");
          await loadReminders();
        } catch { showToast(t("rem.delete_failed"), "error"); }
      }
