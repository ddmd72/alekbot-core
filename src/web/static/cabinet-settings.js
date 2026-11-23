/* The Cabinet's Settings section (CABINET_REDESIGN_RFC §5.1, §5.3, §6): groups of collapsed rows, each row a title
   and its current value; a click opens its body, one at a time. Switches and selects save at once; only a typed
   value (the city) has a Save button. Loaded after cabinet-sections.js; the plain scripts share one global scope.
   The connect helpers (GET /api/connectors) sit here, before the Gmail row that first uses them; the drive card of
   Drives uses them too. The indentation is the other scripts': the text tests find functions by it. */
      /* ── Settings → Security → Memory PIN (CABINET_REDESIGN_RFC §5.3; CABINET_DEVICE_SESSIONS_RFC §7.7) ── */
      // The row shows one state at a time: off, on or resetting at rest, and the steps of set (2), change (3), remove
      // and forgot. The codes of a flow wait in their fields until the one request at the end of it, which clears every
      // field whatever the answer; closing the row or cancelling clears them too. No code is kept anywhere else, and
      // every text is set as text. The requests and their bodies are the API's (facts_pin_app.py), unchanged.
      const PIN_STEPS = ["off", "set1", "set2", "on", "ch1", "ch2", "ch3", "remove", "forgot", "pending"];
      // The steps that hold typed codes: a status refresh (a language switch, a re-entry) never moves the row off one.
      const PIN_INPUT_STEPS = ["set1", "set2", "ch1", "ch2", "ch3", "remove"];
      // Where a refused request sends the flow back to: the step whose entries the server refused.
      const PIN_ERROR_STEPS = {
        set: { mismatch: "set1", not_four_digits: "set1", trivial: "set1", duress_same_as_pin: "set2" },
        change: { wrong_code: "ch1", mismatch: "ch2", not_four_digits: "ch2", trivial: "ch2", duress_same_as_pin: "ch3" },
        remove: {},
      };
      let _pinStep = "";

      // The state at rest, from the server's status.
      function pinRest() {
        if (factsPin.reset_due_at) return "pending";
        return factsPin.has_pin ? "on" : "off";
      }

      // Shows one step of the row; a step that asks for a code puts the cursor in its first field.
      function pinShow(step, keepMessage) {
        _pinStep = step;
        for (const name of PIN_STEPS) document.getElementById(`pin-step-${name}`).classList.toggle("hidden", name !== step);
        if (!keepMessage) {
          pinMessage("", false);
          document.getElementById("pin-logout-offer").classList.add("hidden");
        }
        const first = PIN_INPUT_STEPS.includes(step) && document.querySelector(`#pin-step-${step} input`);
        if (first && _openSettingsRow === "pin") first.focus();
      }

      function pinCancel() {
        clearPinFields();
        pinShow(pinRest());
      }

      function renderPinSettings() {
        const rest = pinRest();
        const chip = { off: ["off", "pin.chip_off"], on: ["ok", "pin.chip_on"], pending: ["warn", "pin.chip_resetting"] }[rest];
        setSettingsChip("pin-chip", chip[0], t(chip[1]));
        renderPinReset();
        if (!PIN_INPUT_STEPS.includes(_pinStep)) pinShow(rest, true);
      }

      async function loadFactsPinSettings() {
        await loadFactsPin();
        renderPinSettings();
      }

      // Shape only, so a step never moves on with entries that cannot be sent: four digits, typed the same twice. The
      // server stays the judge of every code (a trivial one, the duress code, the attempts).
      function pinPairError(first, second) {
        const a = pinValue(first), b = pinValue(second);
        if (!/^\d{4}$/.test(a) || !/^\d{4}$/.test(b)) return "pin.err_not_four_digits";
        return a === b ? "" : "pin.err_mismatch";
      }

      function pinStepError(key, fields) {
        for (const id of fields) document.getElementById(id).value = "";
        pinMessage(t(key), true);
        document.getElementById(fields[0]).focus();
      }

      // "Next" of the set and change flows.
      function pinNext() {
        let error = "", fields = [], next = "";
        if (_pinStep === "set1") [error, fields, next] = [pinPairError("pin-new", "pin-new2"), ["pin-new", "pin-new2"], "set2"];
        else if (_pinStep === "ch1") {
          error = /^\d{4}$/.test(pinValue("pin-current-change")) ? "" : "pin.err_not_four_digits";
          [fields, next] = [["pin-current-change"], "ch2"];
        } else if (_pinStep === "ch2") [error, fields, next] = [pinPairError("pin-new-c", "pin-new2-c"), ["pin-new-c", "pin-new2-c"], "ch3"];
        else return;
        if (error) { pinStepError(error, fields); return; }
        pinShow(next);
      }

      // The duress pair of the last step: shape, then different from the new PIN (the server checks it again).
      function pinDuressError(duress, duress2, pin) {
        const error = pinPairError(duress, duress2);
        if (error) return error;
        return pinValue(duress) === pinValue(pin) ? "pin.err_duress_same_as_pin" : "";
      }

      function pinFlowFailed(r, flow) {
        if (r.status === 423) { onAccountLocked(); return; }
        const step = PIN_ERROR_STEPS[flow][r.data && r.data.error];
        if (step) pinShow(step);
        pinMessage(pinErrorText(r.data), true);
      }

      async function afterPinChange(r, flow, doneKey, offerLogout) {
        clearPinFields();
        if (!r.ok) { pinFlowFailed(r, flow); return; }
        _pinStep = "";
        await loadFactsPinSettings();
        pinMessage(t(doneKey), false);
        document.getElementById("pin-logout-offer").classList.toggle("hidden", !offerLogout);
      }

      async function submitPinSet() {
        const error = pinDuressError("pin-duress", "pin-duress2", "pin-new");
        if (error) { pinStepError(error, ["pin-duress", "pin-duress2"]); return; }
        const r = await pinRequest("POST", "/set", {
          pin: pinValue("pin-new"), pin_confirm: pinValue("pin-new2"),
          duress: pinValue("pin-duress"), duress_confirm: pinValue("pin-duress2"),
        });
        await afterPinChange(r, "set", "pin.done_set", false);
      }

      async function submitPinChange() {
        const error = pinDuressError("pin-duress-c", "pin-duress2-c", "pin-new-c");
        if (error) { pinStepError(error, ["pin-duress-c", "pin-duress2-c"]); return; }
        const r = await pinRequest("POST", "/change", {
          current: pinValue("pin-current-change"),
          pin: pinValue("pin-new-c"), pin_confirm: pinValue("pin-new2-c"),
          duress: pinValue("pin-duress-c"), duress_confirm: pinValue("pin-duress2-c"),
        });
        await afterPinChange(r, "change", "pin.done_changed", true);
      }

      async function submitPinRemove() {
        if (!/^\d{4}$/.test(pinValue("pin-current-remove"))) {
          pinStepError("pin.err_not_four_digits", ["pin-current-remove"]);
          return;
        }
        const r = await pinRequest("POST", "/remove", { current: pinValue("pin-current-remove") });
        await afterPinChange(r, "remove", "pin.done_removed", false);
      }

      /* ── Settings ────────────────────────────────────── */
      // Settings' static texts, merged into the shell's map so one applyTranslations call covers them.
      Object.assign(I18N_SELECTORS, {
        "#settings-title": "settings.title",
        "#settings-account-label": "settings.account",
        "#settings-account-hint": "settings.account_hint",
        "#settings-logout": "settings.logout",
        "#settings-profile-label": "settings.profile",
        "#settings-tz-title": "settings.tz.title",
        "#settings-tz-desc": "settings.tz.desc",
        "#settings-tz-label": "settings.tz.title",
        "#settings-city-title": "settings.city.title",
        "#settings-city-desc": "settings.city.desc",
        "#settings-city-label": "settings.city.title",
        "#settings-city-save": "btn.save",
        "#settings-lang-title": "settings.uilang.title",
        "#settings-uilang-desc": "settings.uilang.desc",
        "#settings-uilang-label": "settings.uilang.title",
        "#lang-ui-default": "settings.uilang.system",
        "#settings-reply-title": "settings.reply.title",
        "#settings-reply-desc": "settings.reply.desc",
        "#lang-mirror-btn": "settings.reply.mirror",
        "#settings-voice-title": "settings.voice.title",
        "#settings-voice-desc": "settings.voice.desc",
        "#settings-connections-label": "settings.connections",
        "#settings-platforms-title": "settings.platforms_title",
        "#settings-drives-title": "nav.drives",
        "#settings-security-label": "settings.security",
        "#pin-title": "pin.title",
        "#pin-desc": "pin.desc",
        "#pin-start-set": "pin.set",
        "#pin-set1-count": "pin.step_1_of_2",
        "#pin-set1-title": "pin.new",
        "#pin-label-new": "pin.new",
        "#pin-label-new2": "pin.new_again",
        "#pin-set1-hint": "pin.new_hint",
        "#pin-set1-next": "pin.next",
        "#pin-set1-cancel": "pin.cancel",
        "#pin-set2-count": "pin.step_2_of_2",
        "#pin-set2-title": "pin.duress",
        "#pin-duress-why-title": "pin.duress_why_title",
        "#pin-duress-why": "pin.duress_why",
        "#pin-label-duress": "pin.duress",
        "#pin-label-duress2": "pin.duress_again",
        "#pin-set2-hint": "pin.duress_hint",
        "#pin-set-btn": "pin.set",
        "#pin-set2-back": "pin.back",
        "#pin-on-desc": "pin.on_desc",
        "#pin-start-change": "pin.change",
        "#pin-start-remove": "pin.remove_short",
        "#pin-forgot": "pin.forgot",
        "#pin-ch1-count": "pin.change_step_1",
        "#pin-change-title": "pin.current",
        "#pin-label-current-change": "pin.current",
        "#pin-ch1-hint": "pin.current_hint",
        "#pin-ch1-next": "pin.next",
        "#pin-ch1-cancel": "pin.cancel",
        "#pin-ch2-count": "pin.change_step_2",
        "#pin-ch2-title": "pin.new",
        "#pin-label-new-c": "pin.new",
        "#pin-label-new2-c": "pin.new_again",
        "#pin-ch2-next": "pin.next",
        "#pin-ch2-back": "pin.back",
        "#pin-ch3-count": "pin.change_step_3",
        "#pin-ch3-title": "pin.duress",
        "#pin-ch3-hint": "pin.duress_again_hint",
        "#pin-label-duress-c": "pin.duress",
        "#pin-label-duress2-c": "pin.duress_again",
        "#pin-change-btn": "pin.change",
        "#pin-ch3-back": "pin.back",
        "#pin-remove-title": "pin.remove_title",
        "#pin-remove-hint": "pin.remove_hint",
        "#pin-label-current-remove": "pin.current",
        "#pin-remove-btn": "pin.remove",
        "#pin-remove-cancel": "pin.cancel",
        "#pin-forgot-title": "pin.forgot",
        "#pin-forgot-desc": "pin.forgot_desc",
        "#pin-reset-request": "pin.request_reset",
        "#pin-forgot-cancel": "pin.cancel",
        "#pin-reset-cancel": "pin.reset_cancel",
        "#pin-logout-text": "pin.logout_offer",
        "#pin-logout-btn": "devices.logout_others",
        "#devices-title": "devices.title",
        "#devices-subtitle": "devices.subtitle",
        "#devices-logout-others": "devices.logout_others",
        "#devices-logout-all": "devices.logout_all",
        "#passkeys-title": "passkeys.title",
        "#passkeys-subtitle": "passkeys.subtitle",
        "#passkeys-add": "passkeys.add",
        "#passkeys-resume-hint": "passkeys.resume_hint",
        "#devices-lost-title": "devices.lost_title",
        "#devices-lost-intro": "devices.lost_intro",
        "#devices-lost-1": "devices.lost_1",
        "#devices-lost-2": "devices.lost_2",
        "#devices-lost-3": "devices.lost_3",
        "#settings-voice-label": "settings.voice_group",
        "#settings-modes-title": "settings.modes_title",
        "#settings-modes-value": "settings.modes_value",
        "#settings-more-label": "settings.more",
        "#settings-dr-title": "settings.dr.title",
        "#settings-dr-desc": "settings.dr.desc",
        "#team-title": "team.title",
        "#team-subtitle": "team.subtitle",
        "#team-generate-btn": "team.generate_invite",
      });
      Object.assign(I18N_PLACEHOLDERS, { "#location-input": "settings.city.placeholder" });

      // Every row that opens a body: the button is #srow-<id>, the body #srow-<id>-body.
      const SETTINGS_ROWS = ["account", "tz", "city", "uilang", "reply", "voice", "platforms", "mail", "todo", "pin",
        "devices", "passkeys", "lost", "team"];
      let _openSettingsRow = "";

      // A click on the open row closes it; opening one closes the other. Leaving the PIN row drops a half-done flow
      // and the codes typed into it.
      function toggleSettingsRow(id) {
        openSettingsRow(_openSettingsRow === id ? "" : id);
      }

      function openSettingsRow(id) {
        const leftPin = _openSettingsRow === "pin" && id !== "pin";
        _openSettingsRow = id;
        if (leftPin) pinCancel();
        for (const row of SETTINGS_ROWS) {
          const open = row === id;
          const button = document.getElementById(`srow-${row}`);
          button.classList.toggle("open", open);
          button.setAttribute("aria-expanded", open ? "true" : "false");
          document.getElementById(`srow-${row}-body`).classList.toggle("hidden", !open);
        }
        if (id === "pin") pinShow(_pinStep || pinRest(), true);
      }

      // A row's status chip: works (ok), off, needs attention (warn).
      function setSettingsChip(id, kind, text) {
        document.getElementById(id).className = `c-chip c-chip-${kind}`;
        document.getElementById(`${id}-text`).textContent = text;
      }

      function setSettingsValue(id, text) {
        document.getElementById(id).textContent = text;
      }

      // "{n} keys" in the plural form the interface language asks for (one, few, many, other).
      function plural(base, n) {
        let form = "other";
        try { form = new Intl.PluralRules(_currentLang).select(n); } catch { /* "other" */ }
        const key = `${base}_${form}`;
        return (t(key) === key ? t(`${base}_other`) : t(key)).replace("{n}", String(n));
      }

      // The address the page was opened with, read before navigate() rewrites it to ?tab=…: a connect return opens
      // its connection's row, the old aliases a row of their group (CABINET_REDESIGN_RFC §4). Once per page load.
      const SETTINGS_ENTRY = new URLSearchParams(window.location.search);
      const CONNECTION_ROWS = { mail: "mail", tasks: "todo" };
      let _settingsEntryRead = false;

      async function settingsEntryRow() {
        if (SETTINGS_ENTRY.get("connected") || SETTINGS_ENTRY.get("connect_error")) {
          const kind = SETTINGS_ENTRY.get("connected") || SETTINGS_ENTRY.get("kind");
          const connector = (await loadConnectors()).find((c) => c.kind === kind);
          if (connector && connector.integration === "drive") return { row: "", group: "" };
          const row = connector && Object.prototype.hasOwnProperty.call(CONNECTION_ROWS, connector.integration)
            ? CONNECTION_ROWS[connector.integration] : "";
          return { row, group: "connections" };
        }
        const tab = SETTINGS_ENTRY.get("tab");
        // ?tab=devices is where the Google sign-in of a passkey change returns, to resume it (googleProofUrl).
        if (tab === "devices") return { row: "passkeys", group: "security" };
        if (tab === "team" && isOwner()) return { row: "team", group: "more" };
        return { row: "", group: "" };
      }

      async function openSettingsEntry() {
        if (_settingsEntryRead) return;
        _settingsEntryRead = true;
        const { row, group } = await settingsEntryRow();
        if (_activeSection !== "settings" || (!row && !group)) return;
        if (row) openSettingsRow(row);
        const target = document.getElementById(row ? `srow-${row}` : `settings-${group}-label`);
        if (target && target.scrollIntoView) target.scrollIntoView({ block: "start" });
      }

      // What the rows show before their first read is blank, never a default that may not be the stored value.
      let _settingsLoaded = false;
      let _settingsLocation = "";
      const _settingsCounts = { devices: null, passkeys: null };

      // The current value of each Profile row and the counts of Security, in the interface language.
      function renderSettingsValues() {
        const tz = document.getElementById("timezone-input");
        setSettingsValue("settings-tz-value", _settingsLoaded && tz.selectedIndex >= 0 ? tz.options[tz.selectedIndex].textContent : "");
        setSettingsValue("settings-city-value", _settingsLoaded ? _settingsLocation || t("settings.city.none") : "");
        const ui = document.getElementById("lang-ui-select");
        setSettingsValue("settings-uilang-value", _langUiSelected && ui.selectedIndex >= 0
          ? ui.options[ui.selectedIndex].textContent : t("settings.uilang.system"));
        const always = _langUiSelected ? t(`settings.reply.always_${_langUiSelected}`) : t("settings.reply.always_default");
        document.getElementById("lang-fixed-btn").textContent = always;
        setSettingsValue("settings-reply-value", _langMirrorSelected ? t("settings.reply.mirror") : always);
        const voices = Array.from(document.querySelectorAll('.voice-lang[aria-pressed="true"]')).map((b) => b.textContent);
        setSettingsValue("settings-voice-value", !_settingsLoaded ? "" : voices.length ? voices.join(", ") : t("settings.voice.auto"));
        for (const [row, base] of [["devices", "devices.count"], ["passkeys", "passkeys.count"]]) {
          const n = _settingsCounts[row];
          setSettingsValue(`settings-${row}-value`, n === null ? "" : plural(base, n));
        }
      }

      async function loadSettings() {
        openSettingsRow("");
        try {
          const res = await authFetch(`${API}/api/user/timezone`, { credentials: "include" });
          if (!res.ok) throw new Error();
          const data = await res.json();
          const sel = document.getElementById("timezone-input");
          sel.value = data.timezone || "UTC";
          if (!sel.value) sel.value = "UTC"; // fallback if stored value not in list
        } catch {
          showToast("Failed to load settings", "error");
        }
        try {
          const locRes = await authFetch(`${API}/api/user/location`, { credentials: "include" });
          if (locRes.ok) {
            const locData = await locRes.json();
            _settingsLocation = locData.location || "";
            document.getElementById("location-input").value = _settingsLocation;
          }
        } catch { /* silent */ }
        await loadLanguage();
        await loadVoiceLanguages();
        await loadDeepResearch();
        _settingsLoaded = true;
        renderSettingsValues();
        await openSettingsEntry();
      }

      async function loadDeepResearch() {
        try {
          const res = await authFetch(`${API}/api/user/deep-research`, { credentials: "include" });
          if (!res.ok) return;
          const data = await res.json();
          document.getElementById("dr-second-pass").checked = data.second_pass || false;
        } catch { /* silent */ }
      }

      // A switch saves at once; a refused save puts it back.
      async function saveDeepResearch() {
        const input = document.getElementById("dr-second-pass");
        const secondPass = input.checked;
        try {
          const res = await authFetch(`${API}/api/user/deep-research`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ second_pass: secondPass }),
          });
          if (!res.ok) throw new Error();
          showToast(secondPass ? "Two-pass mode enabled" : "Single-pass mode", "success");
        } catch {
          input.checked = !secondPass;
          showToast("Failed to save deep research settings", "error");
        }
      }

      // The select saves at once.
      async function saveTimezone() {
        const tz = document.getElementById("timezone-input").value.trim();
        if (!tz) { showToast("Enter a timezone", "error"); return; }
        try {
          const res = await authFetch(`${API}/api/user/timezone`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timezone: tz }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.error || "Invalid timezone", "error");
            return;
          }
          renderSettingsValues();
          showToast(t("toast.tz_saved"), "success");
        } catch {
          showToast(t("toast.tz_failed"), "error");
        }
      }

      // The one typed value of Settings: saved by its button (or Enter).
      async function saveLocation() {
        const loc = document.getElementById("location-input").value.trim();
        try {
          const res = await authFetch(`${API}/api/user/location`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ location: loc }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.error || "Failed to save location", "error");
            return;
          }
          _settingsLocation = loc;
          renderSettingsValues();
          showToast("Location saved", "success");
        } catch {
          showToast("Failed to save location", "error");
        }
      }

      /* ── Language settings ───────────────────────────── */
      // Interface language and reply language are one stored pair (preferred_language + agent_mirror): either row
      // changes its half and sends both, through one POST /api/user/language.
      let _langUiSelected = "";
      let _langMirrorSelected = true;

      function setLangUI(lang) {
        _langUiSelected = lang;
        const sel = document.getElementById("lang-ui-select");
        if (sel) sel.value = lang;
        applyTranslations(lang);
        renderSettingsValues();
      }

      function setLangMirror(mirror) {
        _langMirrorSelected = mirror;
        for (const [id, on] of [["lang-mirror-btn", mirror], ["lang-fixed-btn", !mirror]]) {
          const button = document.getElementById(id);
          button.classList.toggle("on", on);
          button.setAttribute("aria-pressed", on ? "true" : "false");
        }
        renderSettingsValues();
      }

      /* ── Voice message languages ──────────────────────── */
      /* A pill per language, not an ordered list: any number may be spoken, and the provider treats the list as
         *possible* languages with no stated precedence, so there is no order for the user to arrange. A tap saves. */

      function toggleVoiceLang(button) {
        button.setAttribute("aria-pressed", button.getAttribute("aria-pressed") === "true" ? "false" : "true");
      }

      async function loadVoiceLanguages() {
        try {
          const res = await authFetch(`${API}/api/user/voice-languages`, { credentials: "include" });
          if (!res.ok) return;
          const data = await res.json();
          const selected = new Set(data.voice_languages || []);
          document.querySelectorAll(".voice-lang").forEach((b) => {
            b.setAttribute("aria-pressed", selected.has(b.dataset.lang) ? "true" : "false");
          });
        } catch { /* non-fatal */ }
      }

      // A refused save reads the stored list back, so the pills show what is saved.
      async function saveVoiceLanguages() {
        const codes = Array.from(document.querySelectorAll('.voice-lang[aria-pressed="true"]')).map((b) => b.dataset.lang);
        try {
          const res = await authFetch(`${API}/api/user/voice-languages`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ voice_languages: codes }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.error || t("toast.voice_failed"), "error");
            await loadVoiceLanguages();
          } else {
            showToast(t("toast.voice_saved"), "success");
          }
        } catch {
          showToast(t("toast.voice_failed"), "error");
          await loadVoiceLanguages();
        }
        renderSettingsValues();
      }

      async function loadLanguage() {
        try {
          const res = await authFetch(`${API}/api/user/language`, { credentials: "include" });
          if (!res.ok) return;
          const data = await res.json();
          setLangUI(data.preferred_language || "");
          setLangMirror(data.agent_mirror !== false);
        } catch { /* non-fatal */ }
      }

      // A refused save reads the stored pair back, so both rows show what is saved.
      async function saveLanguage() {
        try {
          const body = {
            preferred_language: _langUiSelected || null,
            agent_mirror: _langMirrorSelected,
          };
          const res = await authFetch(`${API}/api/user/language`, {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.error || t("toast.lang_failed"), "error");
            await loadLanguage();
            return;
          }
          showToast(t("toast.lang_saved"), "success");
        } catch {
          showToast(t("toast.lang_failed"), "error");
          await loadLanguage();
        }
      }

      /* ── Settings → Connections: Telegram, Slack, phone ──────────────────────── */
      async function loadPlatforms() {
        try {
          const res = await authFetch(`${API}/api/user/platforms`, {
            credentials: "include",
          });
          const data = res.ok ? await res.json() : { platforms: {} };
          renderPlatforms(data.platforms || {});
        } catch {
          renderPlatforms({});
        }
      }

      // One row for the three ids a message or a call is recognised by. Telegram and Slack link by the id typed here; a
      // phone links only through its code check (request-phone-otp), which this page has never offered, so its item
      // shows the state and, once linked, the unlink.
      function renderPlatforms(platforms) {
        const linked = ["telegram", "slack", "phone"].filter((id) => !!platforms[id]).length;
        setSettingsChip("settings-platforms-chip", linked ? "ok" : "off", t("settings.platforms_count").replace("{n}", String(linked)));
        document.getElementById("platforms-container").innerHTML = [
          platformItem("telegram", platforms.telegram, "Telegram", t("platform.telegram_desc"), "123456789"),
          platformItem("slack", platforms.slack, "Slack", t("platform.slack_desc"), "U0AC..."),
          platformItem("phone", platforms.phone, t("platform.phone"), t("platform.phone_desc"), ""),
        ].join("");
      }

      function platformItem(id, linkedId, label, description, placeholder) {
        if (linkedId) {
          return `<div class="c-mini">
            <div class="c-mini-main"><div class="c-mini-t">${label}</div><div class="c-mini-s">${t("platform.connected")}: ${escapeHtml(linkedId)}</div></div>
            <button type="button" class="c-lnk c-lnk-danger" onclick="unlinkPlatform('${id}')">${t("platform.disconnect")}</button>
          </div>`;
        }
        if (!placeholder) {
          return `<div class="c-mini">
            <div class="c-mini-main"><div class="c-mini-t">${label}</div><div class="c-mini-s">${description} · ${t("platform.not_connected")}</div></div>
          </div>`;
        }
        return `<div class="c-mini">
          <div class="c-mini-main"><label class="c-mini-t" for="input-${id}">${label}</label><div class="c-mini-s">${description}</div></div>
          <div class="c-inline">
            <input id="input-${id}" type="text" placeholder="${placeholder}" class="c-input" />
            <button type="button" onclick="linkPlatform('${id}', '${label}')" class="c-btn c-sec">${t("platform.link_account")}</button>
          </div>
        </div>`;
      }

      async function linkPlatform(id, label) {
        const input = document.getElementById(`input-${id}`);
        const value = input.value.trim();
        if (!value) {
          showToast("Enter your " + label + " ID", "error");
          return;
        }
        try {
          const isTg = id === "telegram";
          const res = await authFetch(
            `${API}${isTg ? "/api/user/link-telegram" : "/api/user/link-platform"}`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              credentials: "include",
              body: JSON.stringify(
                isTg
                  ? { telegram_user_id: value }
                  : { platform: id, platform_user_id: value },
              ),
            },
          );
          if (!res.ok) {
            const e = await res.json();
            throw new Error(e.error || "Link failed");
          }
          showToast(`${label} linked`, "success");
          loadPlatforms();
        } catch (e) {
          showToast(e.message, "error");
        }
      }

      async function unlinkPlatform(platform) {
        if (!confirm(`Disconnect ${platform}?`)) return;
        try {
          const res = await authFetch(
            `${API}/api/user/link-platform?platform=${platform}`,
            { method: "DELETE", credentials: "include" },
          );
          if (!res.ok) throw new Error("Unlink failed");
          showToast(`${platform} disconnected`, "success");
          loadPlatforms();
        } catch (e) {
          showToast(e.message, "error");
        }
      }

      /* ── Connectors (MULTI_DRIVE_RFC §5.2): the connect links come from GET /api/connectors ── */
      // Keyed by integration, never by connector kind: the Cabinet holds no kind of its own.
      const CONNECTED_TOAST_KEYS = {
        mail: "toast.gmail_connected",
        tasks: "toast.todo_connected",
        drive: "toast.drive_connected",
      };
      const SCOPE_TOASTS = {
        mail: "Google did not grant access to your mail — reconnect and tick the Gmail checkbox",
        tasks: "Google did not grant access to your tasks — reconnect and tick the Tasks checkbox",
      };
      let _connectorsPromise = null;

      function loadConnectors() {
        if (!_connectorsPromise) {
          _connectorsPromise = authFetch(`${API}/api/connectors`, { credentials: "include" })
            .then((res) => (res.ok ? res.json() : { connectors: [] }))
            .then((body) => body.connectors || [])
            .catch(() => {
              _connectorsPromise = null; // a failed read is retried on the next render
              return [];
            });
        }
        return _connectorsPromise;
      }

      // The connect (or, with a credential id, reconnect) link of an integration; "" when no connector offers it.
      async function connectHref(integration, credentialId) {
        const connector = (await loadConnectors()).find((c) => c.integration === integration);
        if (!connector) return "";
        const base = `/auth/connect/${encodeURIComponent(connector.kind)}`;
        return credentialId ? `${base}?credential=${encodeURIComponent(credentialId)}` : base;
      }

      // An <a> button to `href`, or nothing when the integration has no connector here. The drive card keeps the old
      // button look; the Settings rows pass the shared component's classes.
      function connectButton(href, label, classes = "btn btn-black") {
        return href ? `<a href="${escapeHtml(href)}" class="${classes}">${label}</a>` : "";
      }

      // The callback's ?connected=<kind> / ?connect_error=<status>&kind=<kind>, named by the connector's label.
      async function showConnectToasts(qs) {
        const connected = qs.get("connected");
        const error = qs.get("connect_error");
        if (!connected && !error) return;
        const connectors = await loadConnectors();
        const connector = connectors.find((c) => c.kind === (connected || qs.get("kind")));
        const label = connector ? connector.label : "Integration";
        if (connected) {
          const key = connector && CONNECTED_TOAST_KEYS[connector.integration];
          showToast(key ? t(key) : `${label} connected`, "success");
          return;
        }
        if (error === "scope")
          showToast((connector && SCOPE_TOASTS[connector.integration]) ||
            `${label} did not get the access it needs — reconnect and allow every permission asked`, "error");
        else if (error === "account")
          showToast(`${label}: you signed in with another account — reconnect with the account this connection uses`, "error");
        else
          showToast(`${label} connection error`, "error");
      }

      async function loadGmailStatus() {
        try {
          const [statusRes, autoIndexRes, dailyReviewRes] = await Promise.all([
            authFetch(`${API}/api/gmail/status`, { credentials: "include" }),
            authFetch(`${API}/api/gmail/auto-index`, { credentials: "include" }),
            authFetch(`${API}/api/gmail/daily-review`, { credentials: "include" }),
          ]);
          if (!statusRes.ok) throw new Error();
          const data = await statusRes.json();
          if (autoIndexRes.ok) {
            const ai = await autoIndexRes.json();
            data.auto_index_enabled = ai.enabled;
            data.auto_index_hour = ai.hour;
          }
          if (dailyReviewRes.ok) {
            const dr = await dailyReviewRes.json();
            data.daily_review_enabled = dr.enabled;
            data.daily_review_hour = dr.hour;
          }
          data.connect_href = await connectHref("mail");
          data.reconnect_href = await connectHref("mail", data.credential_id);
          renderGmailStatus(data);
        } catch {
          // Graceful degradation
        }
      }

      async function saveDailyReview(enabled, hour) {
        try {
          const res = await authFetch(`${API}/api/gmail/daily-review`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled, hour }),
          });
          if (!res.ok) throw new Error();
          showToast(t("toast.daily_review_saved"), "success");
        } catch {
          showToast(t("toast.daily_review_failed"), "error");
        }
      }

      async function saveGmailAutoIndex(enabled, hour) {
        try {
          const res = await authFetch(`${API}/api/gmail/auto-index`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled, hour }),
          });
          if (!res.ok) throw new Error();
          showToast(enabled ? `Auto-index enabled at ${hour}:00` : "Auto-index disabled", "success");
        } catch {
          showToast("Failed to save auto-index settings", "error");
        }
      }

      // The Gmail row: its chip, and in its body the coverage, the two schedules (a switch that saves at once; the
      // hour shows while the switch is on, by CSS) and the actions.
      function renderGmailStatus(data) {
        const container = document.getElementById("gmail-container");

        const linked = data.connected;
        // R1: the saved connection is gone while Gmail was connected before (SECRETS_AT_REST_RFC §6.2)
        const missing = !linked && !!data.reconnect_needed;
        const needsReconnect = (linked && !!data.needs_reconnect) || missing;
        const indexActive = linked && !!data.indexing_active;
        const activeJobId = data.active_job_id || null;

        if (needsReconnect) setSettingsChip("settings-gmail-chip", "warn", t("gmail.needs_reconnect"));
        else if (linked) setSettingsChip("settings-gmail-chip", "ok", t("settings.chip_works"));
        else setSettingsChip("settings-gmail-chip", "off", t("platform.not_connected"));

        const reconnectHtml = needsReconnect
          ? `<div class="c-box c-box-danger">${missing ? t("gmail.reconnect_hint_missing") : t("gmail.reconnect_hint")}</div>`
          : "";

        let metaHtml = "";
        let schedulesHtml = "";
        if (linked) {
          const fmt = (iso) =>
            iso
              ? new Date(iso).toLocaleDateString("en-US", {
                  month: "short",
                  year: "numeric",
                })
              : null;
          const from = fmt(data.oldest_indexed_through),
            to = fmt(data.indexed_through);
          const cov =
            from && to ? `${from} – ${to}` : to ? `Up to ${to}` : "Not indexed";
          metaHtml = `
            <div class="c-mini">
              <div class="c-mini-main"><div class="c-mini-t">Coverage</div><div class="c-mini-s">${cov}</div></div>
              ${data.indexed_through ? `<button type="button" class="c-lnk c-lnk-danger" onclick="deleteGmailData()">Delete Data</button>` : ""}
            </div>`;

          const makeHourOptions = (selectedHour) =>
            Array.from({length: 24}, (_, h) =>
              `<option value="${h}" ${h === selectedHour ? "selected" : ""}>${String(h).padStart(2,"0")}:00</option>`
            ).join("");
          const aiHour = data.auto_index_hour ?? 8;
          const drHour = data.daily_review_hour ?? 9;
          schedulesHtml = `
            <div class="c-mini c-sched">
              <label class="c-mini-t" for="dr-toggle">${t("gmail.daily_review")}</label>
              <input type="checkbox" class="c-switch" id="dr-toggle" ${data.daily_review_enabled ? "checked" : ""}
                onchange="saveDailyReview(this.checked, parseInt(document.getElementById('dr-hour').value))">
              <div class="c-sched-hour">
                <label for="dr-hour">${t("gmail.run_at")}</label>
                <select id="dr-hour" class="c-input c-input-sm" onchange="saveDailyReview(true, parseInt(this.value))">${makeHourOptions(drHour)}</select>
                <span>${t("gmail.in_your_tz")}</span>
              </div>
            </div>
            <div class="c-mini c-sched">
              <label class="c-mini-t" for="ai-toggle">${t("gmail.auto_index")}</label>
              <input type="checkbox" class="c-switch" id="ai-toggle" ${data.auto_index_enabled ? "checked" : ""}
                onchange="saveGmailAutoIndex(this.checked, parseInt(document.getElementById('ai-hour').value))">
              <div class="c-sched-hour">
                <label for="ai-hour">${t("gmail.run_at")}</label>
                <select id="ai-hour" class="c-input c-input-sm" onchange="saveGmailAutoIndex(true, parseInt(this.value))">${makeHourOptions(aiHour)}</select>
                <span>${t("gmail.in_your_tz")}</span>
              </div>
            </div>`;
        }

        const actionButtons = missing
          ? connectButton(data.connect_href, t("gmail.reconnect"), "c-btn c-pri")
          : !linked
          ? connectButton(data.connect_href, t("platform.connect_gmail"), "c-btn c-pri")
          : needsReconnect
          ? `${connectButton(data.reconnect_href, t("gmail.reconnect"), "c-btn c-pri")}
             <button type="button" onclick="disconnectGmail()" class="c-lnk c-lnk-danger">${t("platform.disconnect")}</button>`
          : `<button type="button" onclick="triggerGmailIndex()" class="c-btn c-sec" ${indexActive ? "disabled" : ""}>${indexActive ? t("platform.indexing") : t("platform.index_emails")}</button>
             ${indexActive && activeJobId ? `<button type="button" data-job-id="${escapeHtml(activeJobId)}" onclick="cancelGmailIndex(this.dataset.jobId, this)" class="c-btn c-sec">${t("platform.cancel")}</button>` : ""}
             <button type="button" onclick="disconnectGmail()" class="c-lnk c-lnk-danger">${t("platform.disconnect")}</button>`;

        container.innerHTML = `
          <p class="c-hint">${t("platform.gmail_desc")}</p>
          ${reconnectHtml}
          ${metaHtml}
          ${schedulesHtml}
          <div class="c-btns">${actionButtons}</div>
        `;
      }

      function triggerGmailIndex() {
        renderModal(
          "index-gmail",
          "Index Gmail",
          `<div class="c-flow">
            <div class="c-field">
              <span class="c-field-label">Index Mode</span>
              <div class="c-radios">
                <label class="c-radio"><input type="radio" name="index-mode" value="incremental" checked onchange="updateIndexModal()"> Incremental (New only)</label>
                <label class="c-radio"><input type="radio" name="index-mode" value="backfill" onchange="updateIndexModal()"> Backfill (To specific date)</label>
              </div>
            </div>
            <div id="backfill-date-row" class="c-field hidden">
              <label for="backfill-until-input">Index until date</label>
              <input type="date" id="backfill-until-input" class="c-input" />
            </div>
            <div id="index-pages-row" class="c-field">
              <label for="index-pages-input">Max pages to fetch</label>
              <input type="number" id="index-pages-input" class="c-input" value="1" min="1" placeholder="Empty = fetch all" />
            </div>
          </div>`,
          `<button onclick="closeModal()" class="c-btn c-sec">Cancel</button>
           <button id="modal-index-btn" onclick="submitIndexModal()" class="c-btn c-pri">Start Indexing</button>`,
        );
      }

      function updateIndexModal() {
        const mode = document.querySelector(
          'input[name="index-mode"]:checked',
        ).value;
        document.getElementById("backfill-date-row").classList.toggle("hidden", mode !== "backfill");
        if (mode === "incremental")
          document.getElementById("index-pages-input").value = "1";
        if (mode === "backfill")
          document.getElementById("index-pages-input").value = "";
      }

      async function submitIndexModal() {
        const mode = document.querySelector(
          'input[name="index-mode"]:checked',
        ).value;
        const pagesEl = document.getElementById("index-pages-input");
        const maxPages = Math.max(1, parseInt(pagesEl.value) || 1);
        const backfillUntil =
          (document.getElementById("backfill-until-input") || {}).value || null;

        if (mode === "backfill" && !backfillUntil) {
          showToast("Select date for Backfill", "error");
          return;
        }

        const btn = document.getElementById("modal-index-btn");
        btn.textContent = "Starting...";
        btn.disabled = true;

        const body = { mode };
        if (mode === "incremental" || pagesEl.value.trim())
          body.max_pages = maxPages;
        if (mode === "backfill" && backfillUntil)
          body.backfill_until = backfillUntil;

        try {
          const res = await authFetch(`${API}/api/gmail/index`, {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          closeModal();
          if (res.status === 202) {
            showToast("Indexing started in background.", "success");
            loadGmailStatus();
          } else if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(`Error: ${err.error || res.status}`, "error");
          } else {
            const data = await res.json();
            showToast(`Done: ${data.emails_stored} stored`, "success");
            loadGmailStatus();
          }
        } catch (e) {
          showToast(`Error: ${e.message}`, "error");
          btn.textContent = "Start";
          btn.disabled = false;
        }
      }

      async function cancelGmailIndex(jobId, btn) {
        if (!confirm("Cancel indexing?")) return;
        btn.textContent = "Canceling...";
        btn.disabled = true;
        try {
          const res = await authFetch(`${API}/api/gmail/jobs/${jobId}/cancel`, {
            method: "POST",
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          showToast("Indexing canceled.", "success");
          loadGmailStatus();
        } catch {
          showToast("Error canceling", "error");
          btn.textContent = "Cancel";
          btn.disabled = false;
        }
      }

      async function deleteGmailData() {
        if (!confirm("Delete all indexed emails? Cannot be undone.")) return;
        try {
          const res = await authFetch(`${API}/api/gmail/data`, {
            method: "DELETE",
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          showToast("Data deleted.", "success");
          loadGmailStatus();
        } catch {
          showToast("Failed to delete", "error");
        }
      }

      async function disconnectGmail() {
        if (!confirm("Disconnect Gmail? Indexed data remains.")) return;
        try {
          const res = await authFetch(`${API}/api/gmail/disconnect`, {
            method: "DELETE",
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          showToast("Gmail disconnected.", "success");
          loadGmailStatus();
        } catch {
          showToast("Failed to disconnect", "error");
        }
      }

      /* ── Settings → Connections: Microsoft To Do ─────────────────────────────── */
      async function loadTasksStatus() {
        try {
          const res = await authFetch(`${API}/api/tasks/microsoft/status`, {
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          const data = await res.json();
          data.connect_href = await connectHref("tasks");
          renderServiceStatus(
            "tasks",
            "Microsoft To Do",
            t("platform.tasks_desc"),
            data,
          );
        } catch {}
      }

      function renderServiceStatus(id, name, description, data) {
        const container = document.getElementById("tasks-container");

        const linked = data.connected;
        // R1: the saved connection is gone while a primary list shows a past connection (SECRETS_AT_REST_RFC §6.2)
        const missing = !linked && !!data.reconnect_needed;

        if (missing) setSettingsChip("settings-todo-chip", "warn", t("todo.needs_reconnect"));
        else if (linked) setSettingsChip("settings-todo-chip", "ok", t("settings.chip_works"));
        else setSettingsChip("settings-todo-chip", "off", t("platform.not_connected"));

        const actionButtons = linked
          ? `<button type="button" onclick="disconnectService('${id}')" class="c-lnk c-lnk-danger">${t("platform.disconnect_account")}</button>`
          : missing
          ? connectButton(data.connect_href, t("todo.reconnect"), "c-btn c-pri")
          : connectButton(data.connect_href, t("platform.connect_todo"), "c-btn c-pri");

        container.innerHTML = `
          <p class="c-hint">${description}</p>
          ${missing ? `<div class="c-box c-box-danger">${t("todo.reconnect_hint_missing")}</div>` : ""}
          <div class="c-btns">${actionButtons}</div>
        `;
      }

      async function disconnectService(id) {
        const name = "MS ToDo";
        if (!confirm(`Disconnect ${name}?`)) return;
        try {
          const res = await authFetch(`${API}/api/tasks/microsoft/disconnect`, {
            method: "DELETE",
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          showToast(`${name} disconnected`, "success");
          loadTasksStatus();
        } catch (e) {
          showToast("Error disconnecting", "error");
        }
      }

      /* ── Settings → Security: devices (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6) ─── */
      // label and platform_hint are the browser's User-Agent text: untrusted, always escaped.
      function renderDevices(devices) {
        const list = document.getElementById("devices-list");
        if (!devices.length) {
          list.innerHTML = `<p class="c-hint">${escapeHtml(t("devices.empty"))}</p>`;
          return;
        }
        const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
        list.innerHTML = devices.map((d) => {
          const current = d.is_current
            ? ` <span class="c-chip c-chip-ok">${escapeHtml(t("devices.current"))}</span>`
            : "";
          const kind = d.auth_kind === "google" ? t("devices.kind_google")
            : d.auth_kind === "passkey" ? t("devices.kind_passkey") : d.auth_kind;
          return `<div class="c-mini">
            <div class="c-mini-main">
              <div class="c-mini-t">${escapeHtml(d.label)}${current}</div>
              <div class="c-mini-s">${escapeHtml(d.platform_hint)} · ${escapeHtml(kind)}</div>
              <div class="c-mini-s">${escapeHtml(t("devices.created"))}: ${escapeHtml(when(d.created_at))} · ${escapeHtml(t("devices.last_used"))}: ${escapeHtml(when(d.last_seen_at))}</div>
            </div>
            <button type="button" class="c-lnk c-lnk-danger" data-sid="${escapeHtml(d.sid)}" data-current="${d.is_current ? "1" : ""}" onclick="logoutDevice(this.dataset.sid, !!this.dataset.current)">${escapeHtml(t("devices.logout"))}</button>
          </div>`;
        }).join("");
      }

      async function loadDevices() {
        try {
          const res = await authFetch(`${API}/api/user/devices`, { credentials: "include" });
          if (!res.ok) throw new Error();
          const devices = (await res.json()).devices || [];
          renderDevices(devices);
          _settingsCounts.devices = devices.length;
          renderSettingsValues();
        } catch {
          showToast(t("devices.failed"), "error");
        }
      }

      async function postDevices(path) {
        const res = await authFetch(`${API}/api/user/devices/${path}`, { method: "POST", credentials: "include" });
        if (!res.ok) throw new Error();
      }

      async function logoutDevice(sid, isCurrent) {
        try {
          await postDevices(`${encodeURIComponent(sid)}/logout`);
        } catch {
          showToast(t("devices.failed"), "error");
          return;
        }
        if (isCurrent) { window.location.href = "/"; return; }
        showToast(t("devices.done"));
        loadDevices();
      }

      async function logoutOtherDevices() {
        if (!confirm(t("devices.confirm_others"))) return;
        try {
          await postDevices("logout-others");
        } catch {
          showToast(t("devices.failed"), "error");
          return;
        }
        showToast(t("devices.done"));
        loadDevices();
      }

      async function logoutAllDevices() {
        if (!confirm(t("devices.confirm_all"))) return;
        try {
          await postDevices("logout-all");
        } catch {
          showToast(t("devices.failed"), "error");
          return;
        }
        window.location.href = "/";
      }

      /* ── Settings → Security: Face ID and passkeys (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5) ─── */
      // label and platform_hint are the User-Agent text of the session that added the passkey: untrusted, escaped.
      // Adding one (addPasskeyHere) sits with the sign-in offer in cabinet-sections.js: the banner adds one too.
      function renderPasskeys(passkeys) {
        const list = document.getElementById("passkeys-list");
        if (!passkeys.length) {
          list.innerHTML = `<p class="c-hint">${escapeHtml(t("passkeys.empty"))}</p>`;
          return;
        }
        const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
        list.innerHTML = passkeys.map((p) => {
          const synced = p.synced ? ` · ${escapeHtml(t("passkeys.synced"))}` : "";
          const used = p.last_used_at
            ? `${escapeHtml(t("passkeys.last_used"))}: ${escapeHtml(when(p.last_used_at))}`
            : escapeHtml(t("passkeys.never_used"));
          return `<div class="c-mini">
            <div class="c-mini-main">
              <div class="c-mini-t">${escapeHtml(p.label)}</div>
              <div class="c-mini-s">${escapeHtml(p.platform_hint)}${synced}</div>
              <div class="c-mini-s">${escapeHtml(t("devices.created"))}: ${escapeHtml(when(p.created_at))} · ${used}</div>
            </div>
            <button type="button" class="c-lnk c-lnk-danger" data-id="${escapeHtml(p.id)}" onclick="removePasskey(this.dataset.id)">${escapeHtml(t("passkeys.remove"))}</button>
          </div>`;
        }).join("");
      }

      async function loadPasskeys() {
        try {
          const passkeys = (await fetchPasskeys()).passkeys || [];
          renderPasskeys(passkeys);
          _settingsCounts.passkeys = passkeys.length;
          renderSettingsValues();
        } catch {
          showToast(t("devices.failed"), "error");
        }
      }

      // A passkey change needs a fresh proof (RFC §7.3). This browser holds a passkey of the user (a local marker) ->
      // the Face ID step-up, retried once. Otherwise, or when the step-up fails, the page signs in with Google and
      // comes back to Settings with the passkeys row open (the `devices` alias) and a resume flag that restarts the
      // change. No passkey sheet ever opens on a device that holds no passkey (iOS shows it empty, with a QR code).
      function googleProofUrl(resume, id) {
        let next = "/cabinet?tab=devices&resume=" + resume;
        if (id) next += "&id=" + encodeURIComponent(id);
        return "/auth/login/google?next=" + encodeURIComponent(next);
      }

      async function stepUpIfLocal() {
        return window.alekSession.hasLocalPasskey() && (await window.alekSession.ensurePasskey());
      }

      async function removePasskey(id, resumed) {
        if (!resumed && !confirm(t("passkeys.confirm_remove"))) return;
        try {
          const url = `${API}/api/user/passkeys/${encodeURIComponent(id)}/remove`;
          let res = await authFetch(url, { method: "POST", credentials: "include" });
          if (!res.ok && (await window.alekSession.needsPasskey(res.clone()))) {
            if (await stepUpIfLocal()) res = await authFetch(url, { method: "POST", credentials: "include" });
            else if (!resumed) { window.location.assign(googleProofUrl("remove_passkey", id)); return; }
          }
          if (!res.ok) throw new Error();
        } catch {
          showToast(t("passkeys.failed"), "error");
          return;
        }
        showToast(t("devices.done"));
        loadPasskeys();
      }

      // Right after the Google sign-in the flag is consumed (removed from the URL) and the change starts by itself.
      // Safari allows WebAuthn without a tap since iOS 17.4; when it still refuses, the button and a hint are shown
      // in the passkeys row, which ?tab=devices opened.
      async function resumePasskeyFlow(qs) {
        const resume = qs.get("resume");
        if (!resume) return;
        const id = qs.get("id");
        try {
          const clean = new URLSearchParams(window.location.search);
          clean.delete("resume");
          clean.delete("id");
          const rest = clean.toString();
          window.history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : ""));
        } catch { /* the flag is still consumed here */ }
        if (resume === "add_passkey") {
          const outcome = await addPasskeyHere(true);
          if (outcome === "cancelled") {
            const hint = document.getElementById("passkeys-resume-hint");
            if (hint) hint.classList.remove("hidden");
          }
        } else if (resume === "remove_passkey" && id) {
          await removePasskey(id, true);
        }
      }

      /* ── Settings → More: team invites (owners only) ────────────────────────── */
      async function generateTeamInvite() {
        const email = prompt("Enter email to invite:");
        if (!email || !email.trim()) return;
        try {
          const res = await authFetch(`${API}/api/user/invite-codes`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({
              type: "team_invite",
              email: email.trim(),
              role: "MEMBER",
            }),
          });
          if (!res.ok) throw new Error("Could not create invite");
          const data = await res.json();
          const deepLink = `${window.location.origin}/join?code=${encodeURIComponent(data.code)}`;
          const expiry = new Date(data.expires_at).toLocaleString();
          const el = document.getElementById("generated-code-container");
          el.classList.remove("hidden");
          el.innerHTML = `
          <p class="c-mini-t">Share this code</p>
          <div class="c-code">${escapeHtml(data.code)}</div>
          <p class="c-hint">Copy it now, it will not be shown again. Valid until ${expiry}</p>
          <div class="c-inline">
            <input type="text" id="deep-link-input" class="c-input" value="${escapeHtml(deepLink)}" readonly />
            <button type="button" onclick="copyDeepLink()" class="c-btn c-blk">Copy Link</button>
          </div>`;
          loadCodes();
        } catch (e) {
          showToast(e.message, "error");
        }
      }

      async function loadCodes() {
        try {
          const res = await authFetch(`${API}/api/user/invite-codes`, {
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          const data = await res.json();
          const list = document.getElementById("codes-list-container");
          if (!data.invites.length) {
            list.innerHTML = `<p class="c-hint">No codes generated yet.</p>`;
            return;
          }
          list.innerHTML = data.invites
            .map(
              (c) => `
            <div class="c-mini">
              <div class="c-mini-main">
                <div class="c-mini-t c-mono">${escapeHtml(c.hint ? "\u2026" + c.hint : "\u2014")}</div>
                <div class="c-mini-s">Created ${new Date(c.created_at).toLocaleDateString()} · Used ${c.used_at ? new Date(c.used_at).toLocaleDateString() : "—"}</div>
              </div>
              <span class="c-chip ${c.is_valid ? "c-chip-ok" : "c-chip-off"}">${c.is_valid ? "Active" : "Used"}</span>
            </div>`,
            )
            .join("");
        } catch (e) {
          showToast("Error loading codes", "error");
        }
      }

      function copyDeepLink() {
        navigator.clipboard.writeText(
          document.getElementById("deep-link-input").value,
        );
        showToast(t("toast.link_copied"), "success");
      }
