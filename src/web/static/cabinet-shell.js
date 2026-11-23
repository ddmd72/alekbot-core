/* The Cabinet shell (CABINET_REDESIGN_RFC §4, §6): app state, i18n plumbing, navigation, addresses, init.
   Loaded after cabinet-strings.js and before cabinet-sections.js; the three plain scripts share one global
   scope, so a top-level statement here may not call a function of a later file at load time.
   The indentation is the old inline script's, kept on purpose: the text tests find functions by it. */
      const { authFetch, addPasskey } = window.alekSession;
      /* ── App state ───────────────────────────────────── */
      const API = "";
      let currentUser = null;
      let allFacts = [];


      // Selector → translation key (for static DOM elements)
      const I18N_SELECTORS = {
        "#nav-integrations":          "nav.integrations",
        "#nav-mobile-integrations .tab-label":   "nav.integrations",
        "#nav-memory":                "nav.memory",
        "#nav-mobile-memory .tab-label":         "nav.memory",
        "#nav-team":                  "nav.team",
        "#nav-mobile-team .tab-label":           "nav.team",
        "#nav-settings":              "nav.settings",
        "#nav-mobile-settings .tab-label":       "nav.settings",
        "#nav-devices":               "nav.devices",
        "#nav-mobile-devices .tab-label":        "nav.devices",
        "#devices-title":             "devices.title",
        "#devices-subtitle":          "devices.subtitle",
        "#devices-logout-others":     "devices.logout_others",
        "#devices-logout-all":        "devices.logout_all",
        "#devices-lost-title":        "devices.lost_title",
        "#devices-lost-intro":        "devices.lost_intro",
        "#devices-lost-1":            "devices.lost_1",
        "#devices-lost-2":            "devices.lost_2",
        "#devices-lost-3":            "devices.lost_3",
        "#passkeys-title":            "passkeys.title",
        "#passkeys-subtitle":         "passkeys.subtitle",
        "#passkeys-add":              "passkeys.add",
        "#passkey-offer-text":        "passkeys.offer_text",
        "#passkey-offer-add":         "passkeys.offer_add",
        "#passkey-offer-dismiss":     "passkeys.offer_dismiss",
        "#pin-title": "pin.title",
        "#pin-desc": "pin.desc",
        "#pin-label-new": "pin.new",
        "#pin-label-new2": "pin.new_again",
        "#pin-label-duress": "pin.duress",
        "#pin-label-duress2": "pin.duress_again",
        "#pin-set-btn": "pin.set",
        "#pin-change-title": "pin.change_title",
        "#pin-label-current-change": "pin.current",
        "#pin-label-new-c": "pin.new",
        "#pin-label-new2-c": "pin.new_again",
        "#pin-label-duress-c": "pin.duress",
        "#pin-label-duress2-c": "pin.duress_again",
        "#pin-change-btn": "pin.change",
        "#pin-remove-title": "pin.remove_title",
        "#pin-label-current-remove": "pin.current",
        "#pin-remove-btn": "pin.remove",
        "#pin-forgot": "pin.forgot",
        "#pin-reset-cancel": "pin.reset_cancel",
        "#pin-logout-text": "pin.logout_offer",
        "#pin-logout-btn": "devices.logout_others",
        "#facts-pin-title": "pin.gate_title",
        "#facts-pin-desc": "pin.gate_desc",
        "#facts-pin-unlock": "pin.unlock",
        "#facts-pin-forgot": "pin.forgot",
        "#facts-pin-reset-cancel": "pin.reset_cancel",
        "#nav-call":                  "nav.call",
        "#nav-mobile-call .tab-label":           "nav.call",
        "#nav-modes":                 "nav.modes",
        "#nav-mobile-modes .tab-label":          "nav.modes",
        "#memory-title":              "memory.title",
        "#memory-subtitle":           "memory.subtitle",
        "#memory-search-btn":         "memory.search_btn",
        "#back-to-browse-btn":        "memory.clear",
        "#integrations-title":        "integrations.title",
        "#integrations-subtitle":     "integrations.subtitle",
        "#team-title":                "team.title",
        "#team-subtitle":             "team.subtitle",
        "#team-generate-btn":         "team.generate_invite",
        "#settings-title":            "settings.title",
        "#settings-subtitle":         "settings.subtitle",
        "#settings-tz-title":         "settings.tz.title",
        "#settings-tz-desc":          "settings.tz.desc",
        "#settings-tz-save":          "btn.save",
        "#settings-lang-title":       "settings.lang.title",
        "#settings-lang-desc":        "settings.lang.desc",
        "#settings-lang-ui-label":    "settings.lang.ui_label",
        "#settings-lang-bot-label":   "settings.lang.bot_label",
        "#settings-lang-save":        "btn.save",
        "#settings-voice-title":      "settings.voice.title",
        "#settings-voice-desc":       "settings.voice.desc",
        "#settings-voice-save":       "btn.save",
        "#lang-mirror-btn":           "settings.lang.mirror",
        "#lang-fixed-btn":            "settings.lang.fixed",
        "#settings-dr-title":         "settings.dr.title",
        "#settings-dr-desc":          "settings.dr.desc",
        "#settings-dr-label":         "settings.dr.label",
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
        if (active && active.id === "section-integrations") {
          loadPlatforms();
          loadGmailStatus();
          loadTasksStatus();
          loadDriveStatus();
        }
        if (active && active.id === "section-memory" && factDomainsLang !== _currentLang) {
          loadFactDomains().then(() => {
            if (factsMode === "browse" && factsState !== "closed") renderDomainChips();
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

      /* ── Navigation ──────────────────────────────────── */
      let _activeSection = "integrations";
      const _visitedSections = new Set();

      function navigate(name) {
        if (_activeSection === "memory" && name !== "memory") relockFacts();
        if (name !== "integrations" && _driveStatusTimer) {
          clearTimeout(_driveStatusTimer);
          _driveStatusTimer = null;
        }
        _driveStatusSnapshot = null;   // reopening always renders
        document.querySelectorAll(".section").forEach((s) => {
          s.classList.remove("active");
        });
        document
          .querySelectorAll(".nav-item")
          .forEach((n) => n.classList.remove("active"));

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

        // Update Desktop Nav
        const btnDesktop = document.getElementById(`nav-${name}`);
        if (btnDesktop) btnDesktop.classList.add("active");

        // Update Mobile Nav
        const btnMobile = document.getElementById(`nav-mobile-${name}`);
        if (btnMobile) btnMobile.classList.add("active");

        _activeSection = name;

        if (name === "memory") enterMemory();
        if (name === "integrations") {
          loadPlatforms();
          loadGmailStatus();
          loadTasksStatus();
          loadDriveStatus();
        }
        if (name === "team") loadCodes();
        if (name === "settings") { loadSettings(); loadFactsPinSettings(); }
        if (name === "devices") loadDevices();
        if (name === "devices") loadPasskeys();
      }

      /* ── Init ────────────────────────────────────────── */
      // On a phone the Cabinet's front door is the call screen. Only a bare /cabinet redirects:
      // ?tab= (the call screen's back link) and OAuth/invite return params stay here.
      function redirectPhoneToCall() {
        const qs = new URLSearchParams(window.location.search);
        const isPhone = window.matchMedia("(max-width: 767px) and (pointer: coarse)").matches;
        if (isPhone && [...qs.keys()].length === 0) {
          window.location.replace("/cabinet/call");
          return true;
        }
        return false;
      }

      async function init() {
        if (redirectPhoneToCall()) return;
        try {
          const res = await authFetch(`${API}/auth/me`, { credentials: "include" });
          if (!res.ok) throw new Error("Not authenticated");
          currentUser = await res.json();

          document.getElementById("user-email").textContent =
            currentUser.user.email;

          if (currentUser.account.role === "owner") {
            document.getElementById("nav-team").classList.remove("hidden");
            document
              .getElementById("nav-mobile-team")
              .classList.remove("hidden");
          }

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

          const tab = qs.get("tab");
          navigate(tab && document.getElementById(`section-${tab}`) ? tab : "integrations");
          offerPasskey();
        } catch {
          showToast("Please log in first", "error");
          setTimeout(() => (window.location.href = "/auth/login"), 2000);
        }
      }

      document.addEventListener("DOMContentLoaded", init);
