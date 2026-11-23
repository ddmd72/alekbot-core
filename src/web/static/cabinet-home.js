/* Home's status lines (CABINET_REDESIGN_RFC §5.0): one line under each tile title, built only from the routes the
   Cabinet already calls, read in parallel when Home opens. Loaded after cabinet-sections.js (it reuses pinRequest);
   the plain scripts share one global scope. No fact and no fact count is read here: Memory's line asks the PIN state
   and, only when the facts are open, the review queue, so Home never goes around the PIN. A failed read leaves a
   neutral line on that tile: no toast, no exception. */

      const HOME_TILES = ["memory", "drives", "reminders", "settings"];

      function homeUnknown() {
        return { text: t("home.unknown"), warn: false };
      }

      // pin: GET /api/user/facts-pin or null; questions: the review queue's list, or null when it was not read.
      function homeMemoryStatus(pin, questions) {
        if (!pin) return homeUnknown();
        if (pin.has_pin && !pin.unlocked) return { text: t("home.mem_closed"), warn: false };
        const n = questions ? questions.length : 0;
        if (n > 0) return { text: t(n === 1 ? "home.mem_q_one" : "home.mem_q_many").replace("{n}", String(n)), warn: true };
        return { text: t("home.mem_open"), warn: false };
      }

      // The states are the drive card's: a row that needs a reconnect (its search is paused too) and an indexing
      // error on a drive whose search is on.
      function homeDriveProblem(row) {
        if (row.reconnect) return t("drive.needs_reconnect");
        const ix = row.indexing;
        if (ix && ix.enabled && ix.last_error) return t("drive.index_problem");
        return "";
      }

      // data: GET /api/drive/connections or null.
      function homeDrivesStatus(data) {
        if (!data || !Array.isArray(data.connections)) return homeUnknown();
        const rows = data.connections;
        if (rows.length === 0) return { text: t("home.drives_none"), warn: false };
        for (const row of rows) {
          const problem = homeDriveProblem(row);
          if (problem) return { text: row.mount_name ? `${row.mount_name}: ${problem}` : problem, warn: true };
        }
        const text = rows.length === 1 ? t("home.drives_ok_one") : t("home.drives_ok_many").replace("{n}", String(rows.length));
        return { text, warn: false };
      }

      // The time as the user's own timezone shows it (a bad zone name falls back to the browser's).
      function homeWhen(iso, timezone, lang) {
        const base = { dateStyle: "medium", timeStyle: "short" };
        const date = new Date(iso);
        try {
          return date.toLocaleString(lang, timezone ? { ...base, timeZone: timezone } : base);
        } catch {
          return date.toLocaleString(lang, base);
        }
      }

      // reminders: the list GET /api/user/reminders returns or null; the next one is the earliest still ahead.
      function homeRemindersStatus(reminders, timezone, now) {
        if (!Array.isArray(reminders)) return homeUnknown();
        let next = null;
        for (const r of reminders) {
          const at = r.due ? new Date(r.due).getTime() : NaN;
          if (Number.isNaN(at) || at < now) continue;
          if (!next || at < next.at) next = { at, r };
        }
        if (!next) return { text: t("home.rem_none"), warn: false };
        const text = t("home.rem_next").replace("{when}", homeWhen(next.r.due, timezone, _currentLang)).replace("{label}", next.r.text || "");
        return { text, warn: false };
      }

      // gmail, tasks: the two status reads or null. The first connection needing a reconnect is the line; "working"
      // is said only when both reads came back, so a failed read never turns into a claim.
      function homeSettingsStatus(gmail, tasks) {
        const needs = (s) => !!s && ((s.connected && !!s.needs_reconnect) || (!s.connected && !!s.reconnect_needed));
        if (needs(gmail)) return { text: `Gmail: ${t("gmail.needs_reconnect")}`, warn: true };
        if (needs(tasks)) return { text: `Microsoft To Do: ${t("todo.needs_reconnect")}`, warn: true };
        if (!gmail || !tasks) return homeUnknown();
        return { text: t("home.set_ok"), warn: false };
      }

      async function homeJson(path) {
        try {
          const res = await authFetch(`${API}${path}`, { credentials: "include" });
          return res.ok ? await res.json() : null;
        } catch {
          return null;
        }
      }

      async function homeMemory() {
        let pin = null;
        try {
          const r = await pinRequest("GET", "");
          if (r.ok && r.data) pin = r.data;
        } catch { /* neutral line */ }
        let questions = null;
        if (pin && !(pin.has_pin && !pin.unlocked)) {
          const data = await homeJson("/api/user/facts/review-queue");
          questions = data && Array.isArray(data.questions) ? data.questions : null;
        }
        return homeMemoryStatus(pin, questions);
      }

      async function homeDrives() {
        return homeDrivesStatus(await homeJson("/api/drive/connections"));
      }

      async function homeReminders() {
        const [data, zone] = await Promise.all([homeJson("/api/user/reminders"), homeJson("/api/user/timezone")]);
        return homeRemindersStatus(data ? data.reminders || [] : null, zone && zone.timezone, Date.now());
      }

      async function homeSettings() {
        const [gmail, tasks] = await Promise.all([homeJson("/api/gmail/status"), homeJson("/api/tasks/microsoft/status")]);
        return homeSettingsStatus(gmail, tasks);
      }

      function renderHomeTile(name, status) {
        const tile = document.getElementById(`home-tile-${name}`);
        const line = document.getElementById(`home-status-${name}`);
        if (!tile || !line) return;
        line.textContent = status.text;
        tile.classList.toggle("c-tile-warn", !!status.warn);
      }

      let _homeLoad = 0;
      async function loadHome() {
        const load = ++_homeLoad;
        const reads = { memory: homeMemory, drives: homeDrives, reminders: homeReminders, settings: homeSettings };
        await Promise.all(HOME_TILES.map(async (name) => {
          let status;
          try {
            status = await reads[name]();
          } catch {
            status = homeUnknown();
          }
          if (load === _homeLoad) renderHomeTile(name, status);   // a later visit owns the tiles
        }));
      }
