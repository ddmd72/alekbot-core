/* The Cabinet shell (CABINET_REDESIGN_RFC §4, §6): app state, i18n plumbing, navigation, addresses, init.
   Loaded after cabinet-strings.js and before cabinet-sections.js and cabinet-home.js; the plain scripts share one global
   scope, so a top-level statement here may not call a function of a later file at load time.
   The indentation is the old inline script's, kept on purpose: the text tests find functions by it. */
      const { authFetch, addPasskey } = window.alekSession;
      /* ── App state ───────────────────────────────────── */
      const API = "";
      let currentUser = null;
      let allFacts = [];

      // Selector → translation key (for static DOM elements); cabinet-settings.js adds the Settings section's own.
      const I18N_SELECTORS = {
        "#nav-home":                  "nav.home",
        "#nav-mobile-home .c-bar-label":         "nav.home",
        "#nav-memory":                "nav.memory",
        "#nav-mobile-memory .c-bar-label":       "nav.memory",
        "#nav-drives":                "nav.drives",
        "#nav-mobile-drives .c-bar-label":       "nav.drives",
        "#nav-reminders":             "nav.reminders",
        "#nav-mobile-reminders .c-bar-label":    "nav.reminders",
        "#nav-settings":              "nav.settings",
        "#nav-mobile-settings .c-bar-label":     "nav.settings",
        "#home-call-title":           "home.call_title",
        "#home-call-sub":             "home.call_sub",
        "#home-modes-label":          "home.modes",
        "#home-tile-memory .c-tile-title":       "nav.memory",
        "#home-tile-drives .c-tile-title":       "nav.drives",
        "#home-tile-reminders .c-tile-title":    "nav.reminders",
        "#home-tile-settings .c-tile-title":     "nav.settings",
        "#drives-title":              "nav.drives",
        "#reminders-title":           "nav.reminders",
        "#passkey-offer-text":        "passkeys.offer_text",
        "#passkey-offer-add":         "passkeys.offer_add",
        "#passkey-offer-dismiss":     "passkeys.offer_dismiss",
        "#facts-pin-title": "pin.gate_title",
        "#facts-pin-desc": "pin.gate_desc",
        "#facts-pin-unlock": "pin.unlock",
        "#facts-pin-forgot": "pin.forgot",
        "#facts-pin-reset-cancel": "pin.reset_cancel",
        "#memory-title":              "memory.title",
        "#memory-subtitle":           "memory.subtitle",
        "#memory-search-btn":         "memory.search_btn",
        "#back-to-browse-btn":        "memory.clear",
        "#settings-rem-title":        "settings.rem.title",
        "#settings-rem-desc":         "settings.rem.desc",
      };

      const I18N_PLACEHOLDERS = {
        "#facts-search": "memory.search_placeholder",
      };

      let _currentLang = "en";

      function t(key) {
        return (LANG_DICT[_currentLang] || LANG_DICT["en"])[key] || key;
      }

      function applyTranslations(lang) {
        let resolved = lang;
        if (!lang) {
          const browser = (navigator.language || "").slice(0, 2).toLowerCase();
          resolved = LANG_DICT[browser] ? browser : "en";
        }
        const changed = resolved !== _currentLang;
        _currentLang = resolved;

        for (const [sel, key] of Object.entries(I18N_SELECTORS)) {
          const el = document.querySelector(sel);
          if (el) el.textContent = t(key);
        }
        for (const [sel, key] of Object.entries(I18N_PLACEHOLDERS)) {
          const el = document.querySelector(sel);
          if (el) el.placeholder = t(key);
        }

        // Re-render dynamic sections that are currently visible
        const active = document.querySelector(".section.active");
        if (active && active.id === "section-settings") renderPinSettings();
        // The connection cards and the drive card render their text from t(): redraw them only when the language
        // changed (loadSettings applies the stored language on every visit; that must not read them all again).
        if (changed && active && active.id === "section-settings") {
          loadPlatforms();
          loadGmailStatus();
          loadTasksStatus();
        }
        if (changed && active && active.id === "section-drives") loadDriveStatus();
        if (changed && active && active.id === "section-home") loadHome();
        if (active && active.id === "section-memory" && factDomainsLang !== _currentLang) {
          loadFactDomains().then(() => {
            if (factsMode !== "idle") renderMemoryControls();
            if (factsMode !== "idle" && factsState !== "closed") renderFacts(allFacts, true, true);
          });
        }
      }

      /* ── UI Helpers & Observers ──────────────────────── */
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              entry.target.classList.add("visible");
              observer.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.05, rootMargin: "0px 0px -50px 0px" },
      );

      function observeElements() {
        document
          .querySelectorAll(".reveal:not(.visible)")
          .forEach((el) => observer.observe(el));
      }

      /* ── Navigation and addresses (CABINET_REDESIGN_RFC §4) ─────────────────── */
      // The menu's five sections, in its order (header and phone bar alike).
      const SECTIONS = ["home", "memory", "drives", "reminders", "settings"];
      // Old ?tab= values keep working: the call pages' "← Cabinet" link is ?tab=integrations, and bookmarks hold the
      // rest. Their content now sits in these sections.
      const SECTION_ALIASES = { integrations: "drives", devices: "settings", team: "settings" };
      // Home on a phone and a computer alike (RFC §4): no tab and an unknown tab open it.
      const DEFAULT_SECTION = "home";

      // A section name, an old alias or anything else, as the section to open.
      function resolveSection(name) {
        const section = Object.prototype.hasOwnProperty.call(SECTION_ALIASES, name) ? SECTION_ALIASES[name] : name;
        return SECTIONS.includes(section) ? section : DEFAULT_SECTION;
      }

      // The section a page load opens. A connect return (?connected=<kind>, ?connect_error=…&kind=<kind>) opens the
      // place of that connector whatever ?tab= says: Drives for a drive, Settings for any other connection (Gmail,
      // To Do). `connectors` is GET /api/connectors: the Cabinet holds no kind of its own.
      function sectionFor(qs, connectors) {
        if (qs.get("connected") || qs.get("connect_error")) {
          const kind = qs.get("connected") || qs.get("kind");
          const connector = connectors.find((c) => c.kind === kind);
          return connector && connector.integration === "drive" ? "drives" : "settings";
        }
        return resolveSection(qs.get("tab"));
      }

      // The open section goes to the address, so reload and back keep the place. The address keeps only ?tab=: the
      // one-shot return parameters (connected, connect_error, kind, joined) were read on load and must not fire
      // their toast again on a reload.
      function writeAddress(name) {
        try {
          history.replaceState(history.state, "", `${window.location.pathname}?tab=${name}${window.location.hash}`);
        } catch { /* the address stays as it was; the page works the same */ }
      }

      let _activeSection = "";
      const _visitedSections = new Set();

      function navigate(name) {
        name = resolveSection(name);
        if (_activeSection === "memory" && name !== "memory") relockFacts();
        if (name !== "drives" && _driveStatusTimer) {
          clearTimeout(_driveStatusTimer);
          _driveStatusTimer = null;
        }
        _driveStatusSnapshot = null;   // reopening always renders
        document.querySelectorAll(".section").forEach((s) => {
          s.classList.remove("active");
        });
        document.querySelectorAll(".c-nav-item, .c-bar-item").forEach((n) => {
          n.classList.remove("active");
          n.removeAttribute("aria-current");
        });

        const sec = document.getElementById(`section-${name}`);
        if (sec) {
          sec.classList.add("active");
          // The entrance plays once per section; a tab the user returns to keeps what it shows
          if (!_visitedSections.has(name)) {
            _visitedSections.add(name);
            sec.querySelectorAll(".reveal").forEach((el) => {
              el.classList.remove("visible");
              setTimeout(() => el.classList.add("visible"), 50);
            });
          }
        }

        // The same item in the header and in the phone bar
        for (const id of [`nav-${name}`, `nav-mobile-${name}`]) {
          const item = document.getElementById(id);
          if (item) {
            item.classList.add("active");
            item.setAttribute("aria-current", "page");
          }
        }

        _activeSection = name;
        writeAddress(name);

        if (name === "home") loadHome();
        if (name === "memory") enterMemory();
        if (name === "drives") loadDriveStatus();
        if (name === "reminders") loadReminders();
        if (name === "settings") { loadSettings(); loadFactsPinSettings(); }
        // What Settings holds since the old Integrations, Devices and Team sections moved in
        if (name === "settings") {
          loadPlatforms();
          loadGmailStatus();
          loadTasksStatus();
          loadDevices();
          loadPasskeys();
          if (isOwner()) loadCodes();
        }
      }

      // Team invites are the account owner's (the Team block shows for an owner only).
      function isOwner() {
        return !!currentUser && currentUser.account.role === "owner";
      }

      /* ── Init ────────────────────────────────────────── */
      async function init() {
        try {
          const res = await authFetch(`${API}/auth/me`, { credentials: "include" });
          if (!res.ok) throw new Error("Not authenticated");
          currentUser = await res.json();

          document.getElementById("user-email").textContent =
            currentUser.user.email;

          if (isOwner()) document.getElementById("settings-team").classList.remove("hidden");

          // Apply the user's UI language now — previously this only ran when
          // the Settings tab was opened (loadSettings() -> loadLanguage()),
          // so the whole Cabinet (incl. these toasts) defaulted to English
          // until then.
          await loadLanguage();

          const qs = new URLSearchParams(window.location.search);
          if (qs.get("joined") === "true")
            showToast(t("toast.team_joined"), "success");
          await showConnectToasts(qs);
          await finishAddedDrive(qs);

          const connectReturn = qs.get("connected") || qs.get("connect_error");
          navigate(sectionFor(qs, connectReturn ? await loadConnectors() : []));
          offerPasskey();
          await resumePasskeyFlow(qs).catch(() => {});
        } catch {
          showToast("Please log in first", "error");
          setTimeout(() => (window.location.href = "/auth/login"), 2000);
        }
      }

      document.addEventListener("DOMContentLoaded", init);
