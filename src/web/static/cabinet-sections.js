/* The Cabinet's sections (CABINET_REDESIGN_RFC §6): the rest of the old inline script, in its old order, until
   later steps split it per section. Loaded last, after cabinet-strings.js and cabinet-shell.js.
   The indentation is the old inline script's, kept on purpose: the text tests find functions by it. */
      /* ── Fact domains: keys, labels and order come from the server's registry ── */
      let factDomains = [],
        factDomainLabels = {},
        factDomainsLang = null;

      async function loadFactDomains() {
        const lang = _currentLang;
        if (factDomainsLang === lang) return;
        try {
          const res = await factsFetch(
            `${API}/api/user/fact-domains?lang=${encodeURIComponent(lang)}`,
            { credentials: "include" },
          );
          if (!res.ok) return;
          const data = await res.json();
          factDomains = (data.domains || [])
            .slice()
            .sort((a, b) => a.order - b.order);
          factDomainLabels = Object.fromEntries(
            factDomains.map((d) => [d.key, d.label]),
          );
          factDomainsLang = lang;
        } catch {
          // labels stay as they were; the badges fall back to the stored value
        }
      }

      function domainLabel(d) {
        return d ? factDomainLabels[d] || d.replace(/_/g, " ") : "Uncategorized";
      }

      // The badge of a fact: its profile domain's label; none while the fact has no profile yet.
      function factBadge(f) {
        return f.primary_domain ? domainLabel(f.primary_domain) : "";
      }

      /* ── Devices (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6) ─── */
      // label and platform_hint are the browser's User-Agent text: untrusted, always escaped.
      function renderDevices(devices) {
        const list = document.getElementById("devices-list");
        if (!devices.length) {
          list.innerHTML = `<p style="color:var(--text-subtle);font-style:italic">${escapeHtml(t("devices.empty"))}</p>`;
          return;
        }
        const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
        list.innerHTML = devices.map((d) => {
          const current = d.is_current
            ? ` <span class="fact-domain-badge">${escapeHtml(t("devices.current"))}</span>`
            : "";
          const kind = d.auth_kind === "google" ? t("devices.kind_google")
            : d.auth_kind === "passkey" ? t("devices.kind_passkey") : d.auth_kind;
          return `<div style="background:var(--bg-hover);border-radius:var(--radius-sm);padding:14px;margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
              <div style="flex:1">
                <div style="font-weight:600;color:var(--text-main);font-size:15px">${escapeHtml(d.label)}${current}</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px">${escapeHtml(d.platform_hint)} · ${escapeHtml(kind)}</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px">${escapeHtml(t("devices.created"))}: ${escapeHtml(when(d.created_at))} · ${escapeHtml(t("devices.last_used"))}: ${escapeHtml(when(d.last_seen_at))}</div>
              </div>
              <button class="btn btn-secondary" style="padding:4px 10px;font-size:13px" data-sid="${escapeHtml(d.sid)}" data-current="${d.is_current ? "1" : ""}" onclick="logoutDevice(this.dataset.sid, !!this.dataset.current)">${escapeHtml(t("devices.logout"))}</button>
            </div>
          </div>`;
        }).join("");
      }

      async function loadDevices() {
        try {
          const res = await authFetch(`${API}/api/user/devices`, { credentials: "include" });
          if (!res.ok) throw new Error();
          renderDevices((await res.json()).devices || []);
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

      /* ── Passkeys (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5) ─── */
      // label and platform_hint are the User-Agent text of the session that added the passkey: untrusted, escaped.
      const PASSKEY_OFFER_KEY = "alek.passkeyOffer";

      function renderPasskeys(passkeys) {
        const list = document.getElementById("passkeys-list");
        if (!passkeys.length) {
          list.innerHTML = `<p style="color:var(--text-subtle);font-style:italic">${escapeHtml(t("passkeys.empty"))}</p>`;
          return;
        }
        const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
        list.innerHTML = passkeys.map((p) => {
          const synced = p.synced ? ` · ${escapeHtml(t("passkeys.synced"))}` : "";
          const used = p.last_used_at
            ? `${escapeHtml(t("passkeys.last_used"))}: ${escapeHtml(when(p.last_used_at))}`
            : escapeHtml(t("passkeys.never_used"));
          return `<div style="background:var(--bg-hover);border-radius:var(--radius-sm);padding:14px;margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
              <div style="flex:1">
                <div style="font-weight:600;color:var(--text-main);font-size:15px">${escapeHtml(p.label)}</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px">${escapeHtml(p.platform_hint)}${synced}</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px">${escapeHtml(t("devices.created"))}: ${escapeHtml(when(p.created_at))} · ${used}</div>
              </div>
              <button class="btn btn-secondary" style="padding:4px 10px;font-size:13px" data-id="${escapeHtml(p.id)}" onclick="removePasskey(this.dataset.id)">${escapeHtml(t("passkeys.remove"))}</button>
            </div>
          </div>`;
        }).join("");
      }

      async function fetchPasskeys() {
        const res = await authFetch(`${API}/api/user/passkeys`, { credentials: "include" });
        if (!res.ok) throw new Error();
        return res.json();
      }

      async function loadPasskeys() {
        try {
          renderPasskeys((await fetchPasskeys()).passkeys || []);
        } catch {
          showToast(t("devices.failed"), "error");
        }
      }

      async function addPasskeyHere() {
        let outcome = await addPasskey();
        // The user already has a passkey: adding another needs a fresh assertion on this session. Step up, retry once.
        if (outcome === "passkey_required") outcome = (await window.alekSession.ensurePasskey()) ? await addPasskey() : "failed";
        rememberPasskeyOffer();
        if (outcome === "added") showToast(t("passkeys.added"));
        else if (outcome === "exists") showToast(t("passkeys.exists"));
        else if (outcome === "failed") showToast(t("passkeys.failed"), "error");
        if (_activeSection === "settings") loadPasskeys();   // the passkey list sits in Settings
        return outcome;
      }

      async function removePasskey(id) {
        if (!confirm(t("passkeys.confirm_remove"))) return;
        try {
          const url = `${API}/api/user/passkeys/${encodeURIComponent(id)}/remove`;
          let res = await authFetch(url, { method: "POST", credentials: "include" });
          // A fresh passkey assertion is needed while the user has a passkey: step up, retry once.
          if (!res.ok && (await window.alekSession.needsPasskey(res.clone())) && (await window.alekSession.ensurePasskey())) {
            res = await authFetch(url, { method: "POST", credentials: "include" });
          }
          if (!res.ok) throw new Error();
        } catch {
          showToast(t("passkeys.failed"), "error");
          return;
        }
        showToast(t("devices.done"));
        loadPasskeys();
      }

      // Storage can be absent or refuse (a private window): then the offer simply shows again next time.
      function passkeyOfferDone() {
        try { return localStorage.getItem(PASSKEY_OFFER_KEY) === "done"; } catch { return false; }
      }

      function rememberPasskeyOffer() {
        try { localStorage.setItem(PASSKEY_OFFER_KEY, "done"); } catch { /* shown again next time */ }
        document.getElementById("passkey-offer").classList.add("hidden");
      }

      // After a Google sign-in, once per browser: the banner offering "Add Face ID". Never in the way of the page.
      async function offerPasskey() {
        if (passkeyOfferDone() || typeof window.PublicKeyCredential === "undefined") return;
        try {
          if ((await fetchPasskeys()).offer === true) document.getElementById("passkey-offer").classList.remove("hidden");
        } catch {
          // the offer is a convenience; the Devices tab keeps the button
        }
      }

      async function acceptPasskeyOffer() {
        await addPasskeyHere();
      }

      function dismissPasskeyOffer() {
        rememberPasskeyOffer();
      }

      /* ── Facts PIN (CABINET_DEVICE_SESSIONS_RFC §7.7) ─── */
      // The server decides; the page only asks. A code is typed into a field, sent once and cleared: it is never kept,
      // logged or put in a URL, and every text shown is set as text (never as markup).
      const NO_PIN = { has_pin: false, unlocked: false, reset_due_at: null, can_request_reset: false };
      let factsPin = { ...NO_PIN };
      const PIN_FIELDS = ["pin-new", "pin-new2", "pin-duress", "pin-duress2", "pin-current-change", "pin-new-c",
        "pin-new2-c", "pin-duress-c", "pin-duress2-c", "pin-current-remove", "facts-pin-input"];

      function pinValue(id) {
        return document.getElementById(id).value;
      }

      function clearPinFields() {
        for (const id of PIN_FIELDS) document.getElementById(id).value = "";
      }

      async function pinRequest(method, path, body) {
        const init = { method, credentials: "include" };
        if (body !== undefined) {
          init.headers = { "Content-Type": "application/json" };
          init.body = JSON.stringify(body);
        }
        const res = await authFetch(`${API}/api/user/facts-pin${path}`, init);
        let data = null;
        try { data = await res.json(); } catch { /* a 204 has no body */ }
        return { ok: res.ok, status: res.status, data };
      }

      async function loadFactsPin() {
        try {
          const r = await pinRequest("GET", "");
          if (r.ok && r.data) factsPin = r.data;
        } catch {
          // the facts routes refuse a locked tab themselves (403 facts_locked), whatever this read says
        }
        return factsPin;
      }

      function pinErrorText(data) {
        const error = data && data.error;
        const key = `pin.err_${error}`;
        const text = t(key);
        if (error === "account_locked") return t("pin.account_locked");
        if (text === key) return t("pin.err_failed");
        return text.replace("{n}", String(data.attempts_left));
      }

      function pinMessage(text, isError) {
        const el = document.getElementById("pin-message");
        el.textContent = text;
        el.style.color = isError ? "var(--danger-text)" : "var(--text-muted)";
      }

      function pinWhen(iso) {
        return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      }

      function renderPinReset() {
        const due = factsPin.reset_due_at;
        const text = due ? t("pin.reset_pending").replace("{time}", pinWhen(due)) : "";
        for (const prefix of ["pin", "facts-pin"]) {
          const note = document.getElementById(`${prefix}-reset-note`);
          note.textContent = text;
          note.classList.toggle("hidden", !due);
          document.getElementById(`${prefix}-reset-cancel`).classList.toggle("hidden", !due);
          document.getElementById(`${prefix}-forgot`).classList.toggle("hidden", !!due);
        }
      }

      function renderPinSettings() {
        const has = factsPin.has_pin;
        document.getElementById("pin-set-form").classList.toggle("hidden", has);
        document.getElementById("pin-manage").classList.toggle("hidden", !has);
        document.getElementById("pin-state").textContent = t(has ? "pin.state_set" : "pin.state_none");
        renderPinReset();
      }

      async function loadFactsPinSettings() {
        await loadFactsPin();
        renderPinSettings();
      }

      // "Account locked": the duress code or the fifth wrong code in a row. Every session is gone with the lock.
      function onAccountLocked() {
        clearPinFields();
        pinMessage(t("pin.account_locked"), true);
        document.getElementById("facts-pin-error").textContent = t("pin.account_locked");
        document.getElementById("facts-pin-input").disabled = true;
        document.getElementById("facts-pin-unlock").disabled = true;
        document.getElementById("section-memory").classList.add("pin-locked");
      }

      function pinFailed(r) {
        if (r.status === 423) { onAccountLocked(); return; }
        pinMessage(pinErrorText(r.data), true);
      }

      async function afterPinChange(r, doneKey, offerLogout) {
        clearPinFields();
        if (!r.ok) { pinFailed(r); return; }
        pinMessage(t(doneKey), false);
        document.getElementById("pin-logout-offer").classList.toggle("hidden", !offerLogout);
        await loadFactsPinSettings();
      }

      async function submitPinSet() {
        const r = await pinRequest("POST", "/set", {
          pin: pinValue("pin-new"), pin_confirm: pinValue("pin-new2"),
          duress: pinValue("pin-duress"), duress_confirm: pinValue("pin-duress2"),
        });
        await afterPinChange(r, "pin.done_set", false);
      }

      async function submitPinChange() {
        const r = await pinRequest("POST", "/change", {
          current: pinValue("pin-current-change"),
          pin: pinValue("pin-new-c"), pin_confirm: pinValue("pin-new2-c"),
          duress: pinValue("pin-duress-c"), duress_confirm: pinValue("pin-duress2-c"),
        });
        await afterPinChange(r, "pin.done_changed", true);
      }

      async function submitPinRemove() {
        const r = await pinRequest("POST", "/remove", { current: pinValue("pin-current-remove") });
        await afterPinChange(r, "pin.done_removed", false);
      }

      // A forgotten PIN: the reset takes 24 hours (the server says when), needs a fresh Google sign-in, can be cancelled.
      async function forgotPin() {
        if (!confirm(t("pin.reset_confirm"))) return;
        const r = await pinRequest("POST", "/reset");
        if (!r.ok) {
          if (r.status === 423) { onAccountLocked(); return; }
          const text = pinErrorText(r.data);
          pinMessage(text, true);
          document.getElementById("facts-pin-error").textContent = text;
          return;
        }
        pinMessage(t("pin.done_reset"), false);
        document.getElementById("pin-logout-offer").classList.remove("hidden");
        await loadFactsPin();
        renderPinSettings();
      }

      async function cancelPinReset() {
        const r = await pinRequest("POST", "/reset/cancel");
        if (!r.ok) { pinFailed(r); return; }
        pinMessage(t("pin.done_cancelled"), false);
        await loadFactsPin();
        renderPinSettings();
      }

      /* The Memory tab's gate: locked while a PIN is set and this session is not unlocked. */
      function showFactsGate() {
        document.getElementById("facts-pin-error").textContent = "";
        document.getElementById("section-memory").classList.add("pin-locked");
        renderPinReset();
      }

      function hideFactsGate() {
        document.getElementById("section-memory").classList.remove("pin-locked");
      }

      async function enterMemory() {
        await loadFactsPin();
        if (_activeSection !== "memory") return;
        if (factsPin.has_pin && !factsPin.unlocked) { showFactsGate(); return; }
        hideFactsGate();
        if (factsMode === "idle") browseInit();
      }

      async function unlockFacts() {
        const r = await pinRequest("POST", "/unlock", { pin: pinValue("facts-pin-input") });
        clearPinFields();
        if (r.status === 423) { onAccountLocked(); return; }
        if (!r.ok) {
          document.getElementById("facts-pin-error").textContent = pinErrorText(r.data);
          return;
        }
        factsPin.unlocked = true;
        hideFactsGate();
        if (factsMode === "idle") browseInit();
      }

      // A facts call that answers 403 facts_locked (the unlock ended, or another tab relocked): back to the gate.
      async function factsFetch(url, opts) {
        const res = await authFetch(url, opts);
        if (res.status === 403) {
          let data = null;
          try { data = await res.clone().json(); } catch { /* not the guard's answer */ }
          if (data && data.error === "facts_locked") {
            factsPin = { ...factsPin, has_pin: true, unlocked: false };
            forgetFacts();
            if (_activeSection === "memory") showFactsGate();
          }
        }
        return res;
      }

      // Nothing of the facts stays on the page once the tab is locked or left.
      function forgetFacts() {
        factsMode = "idle";
        document.getElementById("facts-container").innerHTML = "";
      }

      // Leaving the tab or closing the page relocks the session (best effort: the 15-minute end is the guarantee).
      function relockFacts() {
        if (!factsPin.has_pin || !factsPin.unlocked) return;
        factsPin.unlocked = false;
        forgetFacts();
        try {
          navigator.sendBeacon(`${API}/api/user/facts-pin/relock`, new Blob([], { type: "application/json" }));
        } catch { /* the unlock ends by itself */ }
      }

      window.addEventListener("pagehide", relockFacts);

      /* ── Settings ────────────────────────────────────── */
      async function loadSettings() {
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
            document.getElementById("location-input").value = locData.location || "";
          }
        } catch { /* silent */ }
        await loadLanguage();
        await loadVoiceLanguages();
        await loadDeepResearch();
      }

      async function loadDeepResearch() {
        try {
          const res = await authFetch(`${API}/api/user/deep-research`, { credentials: "include" });
          if (!res.ok) return;
          const data = await res.json();
          document.getElementById("dr-second-pass").checked = data.second_pass || false;
        } catch { /* silent */ }
      }

      async function saveDeepResearch() {
        const secondPass = document.getElementById("dr-second-pass").checked;
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
          showToast("Failed to save deep research settings", "error");
        }
      }

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
          showToast(t("toast.tz_saved"), "success");
        } catch {
          showToast(t("toast.tz_failed"), "error");
        }
      }

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
          showToast("Location saved", "success");
        } catch {
          showToast("Failed to save location", "error");
        }
      }

      /* ── Language settings ───────────────────────────── */
      let _langUiSelected = "";
      let _langMirrorSelected = true;

      function setLangUI(lang) {
        _langUiSelected = lang;
        const sel = document.getElementById("lang-ui-select");
        if (sel) sel.value = lang;
        applyTranslations(lang);
      }

      function setLangMirror(mirror) {
        _langMirrorSelected = mirror;
        const mirrorBtn = document.getElementById("lang-mirror-btn");
        const fixedBtn = document.getElementById("lang-fixed-btn");
        if (mirrorBtn) mirrorBtn.classList.toggle("active", mirror);
        if (fixedBtn) fixedBtn.classList.toggle("active", !mirror);
      }

      /* ── Voice message languages ──────────────────────── */
      /* A checkbox per language, not a multi-select: any number may be spoken, and the
         provider treats the list as *possible* languages with no stated precedence, so
         there is no order for the user to arrange. */

      async function loadVoiceLanguages() {
        try {
          const res = await authFetch(`${API}/api/user/voice-languages`, { credentials: "include" });
          if (!res.ok) return;
          const data = await res.json();
          const selected = new Set(data.voice_languages || []);
          document.querySelectorAll(".voice-lang").forEach((cb) => {
            cb.checked = selected.has(cb.value);
          });
        } catch { /* non-fatal */ }
      }

      async function saveVoiceLanguages() {
        const codes = Array.from(document.querySelectorAll(".voice-lang:checked")).map((cb) => cb.value);
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
            return;
          }
          showToast(t("toast.voice_saved"), "success");
        } catch {
          showToast(t("toast.voice_failed"), "error");
        }
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
            return;
          }
          showToast(t("toast.lang_saved"), "success");
        } catch {
          showToast(t("toast.lang_failed"), "error");
        }
      }

      /* ── Bot Reminders ────────────────────────────────── */
      let _remindersData = [];

      async function loadReminders() {
        const list = document.getElementById("reminders-list");
        try {
          const res = await authFetch(`${API}/api/user/reminders`, { credentials: "include" });
          if (res.status === 501) { list.textContent = "Reminders not available."; return; }
          if (!res.ok) throw new Error();
          const data = await res.json();
          _remindersData = data.reminders || [];
          renderReminders();
        } catch {
          list.textContent = "Failed to load reminders.";
        }
      }

      function renderReminders() {
        const list = document.getElementById("reminders-list");
        if (!_remindersData.length) {
          list.innerHTML = `<p style="color:var(--text-subtle);font-style:italic">No reminders yet.</p>`;
          return;
        }
        list.innerHTML = _remindersData.map(r => {
          const due = r.due ? new Date(r.due).toLocaleString(undefined, {dateStyle:"medium",timeStyle:"short"}) : "—";
          const rec = r.recurrence ? `🔁 ${esc(r.recurrence_label || r.recurrence)}` : "once";
          const instr = esc(r.instruction || "").replace(/\n/g, "<br>");
          // AGENT_WORKSPACE_RFC §8.3: one read-only line; edited through chat.
          const fileLine = r.workspace_file
            ? `<div style="font-size:13px;color:var(--text-muted);margin-top:4px">workspace file: workspace:${esc(r.workspace_file)}</div>`
            : "";
          return `<div style="background:var(--bg-hover);border-radius:var(--radius-sm);padding:14px;margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
              <div style="flex:1">
                <div style="font-weight:600;color:var(--text-main);font-size:15px">${esc(r.text)}</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px">${due} · ${rec}</div>
                ${fileLine}
                <div style="font-size:13px;color:var(--text-subtle);margin-top:6px;line-height:1.5">${instr}</div>
              </div>
              <div style="display:flex;gap:6px;flex-shrink:0">
                <button class="btn btn-secondary" style="padding:4px 10px;font-size:13px" data-note-id="${escapeHtml(r.note_id)}" onclick="editReminder(this.dataset.noteId)">Edit</button>
                <button class="btn btn-danger" style="padding:4px 10px;font-size:13px" data-note-id="${escapeHtml(r.note_id)}" onclick="deleteReminder(this.dataset.noteId)">Delete</button>
              </div>
            </div>
          </div>`;
        }).join("");
      }

      function esc(s) { return escapeHtml(s); }

      function showReminderForm(note) {
        document.getElementById("rem-edit-id").value = note ? note.note_id : "";
        document.getElementById("rem-text").value = note ? note.text : "";
        document.getElementById("rem-instruction").value = note ? note.instruction : "";
        if (note && note.due) {
          const d = new Date(note.due);
          const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
          document.getElementById("rem-due").value = local;
        } else {
          document.getElementById("rem-due").value = "";
        }
        const rule = note?.recurrence || "";
        const sel = document.getElementById("rem-recurrence");
        const preset = [...sel.options].some(o => o.value === rule && rule);
        sel.value = rule ? (preset ? rule : "__custom__") : "";
        document.getElementById("rem-rrule").value = preset ? "" : rule;
        onRecurrenceChange();
        document.getElementById("reminder-form").style.display = "block";
      }

      function onRecurrenceChange() {
        const custom = document.getElementById("rem-recurrence").value === "__custom__";
        document.getElementById("rem-rrule-row").style.display = custom ? "block" : "none";
      }

      function hideReminderForm() {
        document.getElementById("reminder-form").style.display = "none";
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
        if (!text || !instruction || !due) { showToast("Fill in all fields", "error"); return; }
        if (recSel === "__custom__" && !recurrence) { showToast("Enter a recurrence rule", "error"); return; }
        // Always sent: an empty string is how the API is told to stop repeating.
        const body = { text, instruction, due, recurrence };
        try {
          const url = editId ? `${API}/api/user/reminders/${editId}` : `${API}/api/user/reminders`;
          const method = editId ? "PUT" : "POST";
          const res = await authFetch(url, { method, credentials: "include", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
          if (!res.ok) { const e = await res.json().catch(()=>({})); showToast(e.error || "Failed", "error"); return; }
          hideReminderForm();
          showToast(editId ? "Reminder updated" : "Reminder created", "success");
          await loadReminders();
        } catch { showToast("Failed to save reminder", "error"); }
      }

      async function deleteReminder(noteId) {
        if (!confirm("Delete this reminder?")) return;
        try {
          const res = await authFetch(`${API}/api/user/reminders/${noteId}`, { method: "DELETE", credentials: "include" });
          if (!res.ok) { showToast("Failed to delete", "error"); return; }
          showToast("Reminder deleted", "success");
          await loadReminders();
        } catch { showToast("Failed to delete", "error"); }
      }

      /* ── Facts state ─────────────────────────────────── */
      let factsMode = "idle",
        factsState = "current",
        browseCursor = null,
        browseDomain = "all";

      // Store the skeleton template so we don't lose it when overwriting container
      let _memorySkeletonHtml = "";

      async function browseInit() {
        if (!_memorySkeletonHtml) {
          const skel = document.getElementById("memory-content-placeholder");
          if (skel) _memorySkeletonHtml = skel.outerHTML;
        }
        document.getElementById("facts-container").innerHTML =
          _memorySkeletonHtml;
        browseCursor = null;
        browseDomain = "all";
        factsMode = "browse";
        renderStateChips();
        loadReviewQuestions();
        await loadFactDomains();
        if (factsState === "closed") await closedLoad();
        else await browseLoad(true);
      }

      /* ── Fact states and the review's questions ──────── */
      const FACT_STATE_KEYS = {
        current: "memory.state_current",
        stale: "memory.state_stale",
        closed: "memory.state_closed",
      };

      function renderStateChips() {
        document.getElementById("state-chips").innerHTML = Object.keys(
          FACT_STATE_KEYS,
        )
          .map(
            (st) =>
              `<button data-state="${st}" onclick="setFactsState(this.dataset.state)" class="chip ${st === factsState ? "active" : ""}">${escapeHtml(t(FACT_STATE_KEYS[st]))}</button>`,
          )
          .join("");
      }

      function setFactsState(st) {
        if (!FACT_STATE_KEYS[st] || st === factsState) return;
        factsState = st;
        browseCursor = null;
        browseDomain = "all";
        allFacts = [];
        renderStateChips();
        document
          .getElementById("domain-chips")
          .classList.toggle("hidden", st === "closed");
        if (st === "closed") closedLoad();
        else browseLoad(true);
      }

      function shortDate(iso) {
        return iso
          ? new Date(iso).toLocaleDateString(_currentLang, {
              day: "numeric",
              month: "short",
              year: "numeric",
            })
          : "";
      }

      async function closedLoad() {
        const container = document.getElementById("facts-container");
        const countEl = document.getElementById("facts-count");
        document.getElementById("facts-pagination").innerHTML = "";
        try {
          const res = await factsFetch(`${API}/api/user/facts/recently-closed`, {
            credentials: "include",
          });
          if (!res.ok) throw new Error("Failed to load facts");
          const data = await res.json();
          const facts = data.facts || [];
          document.getElementById("domain-chips").classList.add("hidden");
          if (facts.length === 0) {
            container.innerHTML = `<div style="grid-column: 1/-1; text-align:center; padding: 64px 20px;">
                                     <h3 style="color:var(--text-muted); font-weight:500;">${escapeHtml(t("memory.closed_empty").replace("{days}", data.window_days))}</h3>
                                   </div>`;
            countEl.textContent = "";
            return;
          }
          container.innerHTML = facts
            .map((f) => {
              const why = t(
                f.state === "invalidated"
                  ? "memory.closed_invalidated"
                  : "memory.closed_archived",
              );
              const when = t("memory.closed_on").replace(
                "{date}",
                shortDate(f.closed_at),
              );
              return `
        <div class="card fact-card reveal visible">
          <div class="card-body" style="display:flex; flex-direction:column; height:100%;">
            <div class="fact-text">${escapeHtml(f.text)}</div>
            <div class="fact-meta">
              <span class="fact-domain-badge">${escapeHtml(why)}</span>
              <span style="color:var(--text-muted); font-size:13px;">${escapeHtml(when)}</span>
            </div>
          </div>
        </div>`;
            })
            .join("");
          countEl.textContent = `${facts.length}`;
        } catch (e) {
          showToast("Error loading facts: " + e.message, "error");
        }
      }

      async function loadReviewQuestions() {
        const box = document.getElementById("review-questions");
        try {
          const res = await factsFetch(`${API}/api/user/facts/review-queue`, {
            credentials: "include",
          });
          if (!res.ok) {
            box.classList.add("hidden");
            return;
          }
          const data = await res.json();
          reviewQuestions = data.questions || [];
          if (!reviewBound) {
            box.addEventListener("click", onReviewClick);
            box.addEventListener("change", onReviewChange);
            reviewBound = true;
          }
          renderReviewQuestions(data.questions || []);
        } catch {
          box.classList.add("hidden");
        }
      }

      function reviewStatusLine(q) {
        if (q.status === "current") return t("questions.status_current");
        if (q.status === "sent")
          return t("questions.status_sent").replace(
            "{date}",
            shortDate(q.asked_at),
          );
        return `${t("questions.status_waiting")}, ${t("questions.waiting_since").replace("{date}", shortDate(q.waiting_since))}`;
      }

      const REVIEW_PAGE = 10;
      let reviewQuestions = [],
        reviewResults = {},
        reviewBound = false,
        reviewShown = { questions: REVIEW_PAGE, invoices: REVIEW_PAGE };

      function answerButtons(options, questionId) {
        const q = questionId
          ? ` data-question-id="${escapeHtml(questionId)}"`
          : "";
        return options
          .map(
            (o) =>
              `<button data-answer="${escapeHtml(o.answer)}"${q} class="btn btn-secondary btn-sm">${escapeHtml(o.label)}</button>`,
          )
          .join("");
      }

      function checkedReviewIds(group) {
        const box = document.getElementById("review-questions");
        return Array.from(
          box.querySelectorAll(
            `input[type=checkbox][data-group="${group}"][data-question-id]:checked`,
          ),
        ).map((c) => c.dataset.questionId);
      }

      function bulkBarContent(group) {
        const inGroup = reviewQuestions.filter((q) => q.group === group);
        const checked = checkedReviewIds(group)
          .map((id) => inGroup.find((q) => q.question_id === id))
          .filter(Boolean);
        const kinds = new Set(checked.map((q) => q.kind));
        if (kinds.size > 1)
          return `<span style="color:var(--text-muted);">${escapeHtml(t("questions.one_kind"))}</span>`;
        const first = checked[0] || inGroup[0];
        return `<span style="color:var(--text-muted);">${escapeHtml(t("questions.mark_selected"))}</span>${answerButtons(first ? first.options : [], "")}`;
      }

      function refreshBulkBar(group) {
        const bar = document
          .getElementById("review-questions")
          .querySelector(`div[data-group="${group}"]`);
        if (bar) bar.innerHTML = bulkBarContent(group);
      }

      function reviewRow(q) {
        const domain = q.primary_domain
          ? `<span class="fact-domain-badge">${escapeHtml(domainLabel(q.primary_domain))}</span>`
          : "";
        const result = reviewResults[q.question_id]
          ? `<div style="color:var(--text-muted); font-size:13px; margin-top:8px;">${escapeHtml(t("questions.result_" + reviewResults[q.question_id]))}</div>`
          : "";
        return `
        <div class="card fact-card" style="min-height:0; margin-bottom:16px;">
          <div class="card-body">
            <label style="display:flex; gap:12px; align-items:flex-start; cursor:pointer;">
              <input type="checkbox" data-question-id="${escapeHtml(q.question_id)}" data-group="${escapeHtml(q.group)}" aria-label="${escapeHtml(t("questions.select"))}" style="margin-top:8px;" />
              <span class="fact-text">${escapeHtml(q.text)}</span>
            </label>
            <div class="fact-meta">
              ${domain}
              <span style="color:var(--text-muted); font-size:13px;">${escapeHtml(reviewStatusLine(q))}</span>
            </div>
            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:12px;">${answerButtons(q.options, q.question_id)}</div>
            ${result}
          </div>
        </div>`;
      }

      function reviewSection(group, title, subtitle) {
        const qs = reviewQuestions.filter((q) => q.group === group);
        if (qs.length === 0) return "";
        const shown = qs.slice(0, reviewShown[group]);
        const sub = subtitle
          ? `<p style="color:var(--text-muted); margin-bottom:16px;">${escapeHtml(subtitle)}</p>`
          : "";
        return `
        <div style="margin-bottom: 40px;">
          <h2 style="margin-bottom:4px;" data-count-title="${escapeHtml(group)}">${escapeHtml(title)} (${qs.length})</h2>
          ${sub}
          <div data-group="${escapeHtml(group)}" style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:16px;">${bulkBarContent(group)}</div>
          <div data-rows="${escapeHtml(group)}">${shown.map(reviewRow).join("")}</div>
          <div data-more="${escapeHtml(group)}">${reviewMoreButton(group)}</div>
        </div>`;
      }

      function reviewMoreButton(group) {
        const hidden = reviewQuestions.filter((q) => q.group === group).length - reviewShown[group];
        return hidden > 0
          ? `<button data-show-more="${escapeHtml(group)}" class="btn btn-secondary">${escapeHtml(t("questions.show_more"))} (${hidden})</button>`
          : "";
      }

      const REVIEW_TITLES = {
        questions: "questions.title",
        invoices: "questions.invoices_title",
      };

      function showMoreReview(group) {
        reviewShown[group] += REVIEW_PAGE;
        const box = document.getElementById("review-questions");
        const rows = box.querySelector(`div[data-rows="${group}"]`);
        const shownIds = new Set(
          Array.from(rows.querySelectorAll("input[data-question-id]")).map((c) => c.dataset.questionId),
        );
        rows.insertAdjacentHTML(
          "beforeend",
          reviewQuestions
            .filter((q) => q.group === group && !shownIds.has(q.question_id))
            .slice(0, reviewShown[group] - shownIds.size)
            .map(reviewRow)
            .join(""),
        );
        box.querySelector(`div[data-more="${group}"]`).innerHTML = reviewMoreButton(group);
        box.dataset.rendered = "";
      }

      // Answered rows leave one by one; the rest of the list is not repainted
      function removeReviewRows(ids) {
        const box = document.getElementById("review-questions");
        const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const gone = new Set(ids);
        const groups = new Set(reviewQuestions.filter((q) => gone.has(q.question_id)).map((q) => q.group));
        reviewQuestions = reviewQuestions.filter((q) => !gone.has(q.question_id));
        ids.forEach((id) => {
          const input = box.querySelector(`input[data-question-id="${CSS.escape(id)}"]`);
          const row = input && input.closest(".fact-card");
          if (!row) return;
          const drop = () => row.remove();
          if (calm) return drop();
          row.style.overflow = "hidden";
          row.style.maxHeight = row.offsetHeight + "px";
          row.style.transition = "max-height .25s ease, opacity .25s ease, margin .25s ease";
          requestAnimationFrame(() => {
            row.style.maxHeight = "0";
            row.style.opacity = "0";
            row.style.marginBottom = "0";
          });
          setTimeout(drop, 280);
        });
        groups.forEach((group) => {
          const left = reviewQuestions.filter((q) => q.group === group);
          const section = box.querySelector(`div[data-rows="${group}"]`).parentElement;
          if (left.length === 0) {
            setTimeout(() => section.remove(), calm ? 0 : 280);
            return;
          }
          const title = section.querySelector(`h2[data-count-title="${group}"]`);
          title.textContent = `${t(REVIEW_TITLES[group])} (${left.length})`;
          // keep the page full: the next hidden questions take the freed places
          const rows = section.querySelector(`div[data-rows="${group}"]`);
          const present = new Set(
            Array.from(rows.querySelectorAll("input[data-question-id]")).map((c) => c.dataset.questionId),
          );
          const room = reviewShown[group] - (present.size - ids.filter((id) => present.has(id)).length);
          rows.insertAdjacentHTML(
            "beforeend",
            left.filter((q) => !present.has(q.question_id)).slice(0, Math.max(room, 0)).map(reviewRow).join(""),
          );
          section.querySelector(`div[data-more="${group}"]`).innerHTML = reviewMoreButton(group);
          refreshBulkBar(group);
        });
        if (reviewQuestions.length === 0) setTimeout(() => box.classList.add("hidden"), calm ? 0 : 280);
        box.dataset.rendered = "";
      }

      function renderReviewQuestions(questions) {
        const box = document.getElementById("review-questions");
        if (questions.length === 0) {
          box.classList.add("hidden");
          box.innerHTML = "";
          delete box.dataset.rendered;
          return;
        }
        const html =
          reviewSection("questions", t("questions.title"), t("questions.subtitle")) +
          reviewSection("invoices", t("questions.invoices_title"), "");
        box.classList.remove("hidden");
        // Same markup = nothing to repaint; a changed list keeps the owner's ticked rows
        if (box.dataset.rendered === html) {
          box.querySelectorAll("button").forEach((b) => (b.disabled = false));
          return;
        }
        const ticked = new Set([
          ...checkedReviewIds("questions"),
          ...checkedReviewIds("invoices"),
        ]);
        box.innerHTML = html;
        box.dataset.rendered = html;
        box.querySelectorAll("input[type=checkbox][data-question-id]").forEach((c) => {
          c.checked = ticked.has(c.dataset.questionId);
        });
        ["questions", "invoices"].forEach(refreshBulkBar);
      }

      function onReviewChange(ev) {
        const group = ev.target.dataset && ev.target.dataset.group;
        if (group) refreshBulkBar(group);
      }

      async function onReviewClick(ev) {
        const more = ev.target.closest("button[data-show-more]");
        if (more) return showMoreReview(more.dataset.showMore);
        const btn = ev.target.closest("button[data-answer]");
        if (!btn) return;
        const bar = btn.closest("div[data-group]");
        const ids = btn.dataset.questionId
          ? [btn.dataset.questionId]
          : bar
            ? checkedReviewIds(bar.dataset.group)
            : [];
        if (ids.length === 0) {
          showToast(t("questions.none_selected"), "error");
          return;
        }
        await sendReviewAnswers(
          ids.map((id) => ({ question_id: id, answer: btn.dataset.answer })),
        );
      }

      async function sendReviewAnswers(answers) {
        const box = document.getElementById("review-questions");
        box
          .querySelectorAll("button")
          .forEach((b) => (b.disabled = true));
        try {
          const res = await factsFetch(`${API}/api/user/facts/review-answers`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ answers }),
          });
          if (!res.ok) throw new Error("answers refused");
          const data = await res.json();
          reviewResults = {};
          let applied = false;
          let changed = false;
          (data.results || []).forEach((r) => {
            if (r.result === "applied") applied = true;
            else reviewResults[r.question_id] = r.result;
            if (r.result === "changed") changed = true;
          });
          if (changed) showToast(t("questions.result_changed"), "error");
          const done = (data.results || []).filter((r) => r.result === "applied").map((r) => r.question_id);
          if (done.length === (data.results || []).length) {
            box.querySelectorAll("button").forEach((b) => (b.disabled = false));
            removeReviewRows(done);
          } else {
            await loadReviewQuestions();
          }
          if (applied || changed) {
            if (factsState === "closed") await closedLoad();
            else await browseLoad(true, true);
          }
        } catch (e) {
          showToast(t("questions.failed"), "error");
          await loadReviewQuestions();
        }
      }

      async function browseLoad(isFirst = false, keepList = false) {
        // keepList: refresh in place, the cards stay until the new page arrives (no skeleton flash)
        if (isFirst && !keepList) {
          document.getElementById("facts-container").innerHTML =
            _memorySkeletonHtml;
        }

        try {
          const dp =
            browseDomain !== "all"
              ? `&domain=${encodeURIComponent(browseDomain)}`
              : "";
          const cp = browseCursor
            ? `&cursor=${encodeURIComponent(browseCursor)}`
            : "";
          const sp = factsState === "stale" ? "&state=stale" : "";
          const res = await factsFetch(
            `${API}/api/user/facts/browse?limit=50${sp}${dp}${cp}`,
            { credentials: "include" },
          );
          if (!res.ok) throw new Error("Failed to load facts");
          const data = await res.json();

          document.getElementById("back-to-browse-btn").classList.add("hidden");
          document.getElementById("domain-chips").classList.remove("hidden");

          const newFacts = data.facts || [];
          // A refresh that finds the same first page leaves the cards and the paging alone
          if (
            keepList &&
            JSON.stringify(allFacts.slice(0, newFacts.length)) === JSON.stringify(newFacts)
          )
            return;
          if (isFirst) allFacts = newFacts;
          else allFacts = allFacts.concat(newFacts);

          browseCursor = data.next_cursor || null;

          renderFacts(newFacts, isFirst, keepList);
          renderDomainChips();
        } catch (e) {
          showToast("Error loading facts: " + e.message, "error");
        }
      }

      async function browseNextPage() {
        if (!browseCursor) return;
        await browseLoad(false);
      }

      function setDomainFilter(domain) {
        browseDomain = domain;
        browseCursor = null;
        allFacts = [];

        // Ensure styling updates visually right away
        document
          .querySelectorAll("#domain-chips .chip")
          .forEach((c) => c.classList.remove("active"));
        const chip = document.getElementById(`chip-${domain}`);
        if (chip) chip.classList.add("active");

        browseLoad(true);
      }

      function backToBrowse() {
        document.getElementById("facts-search").value = "";
        document.getElementById("domain-chips").classList.remove("hidden");
        document.getElementById("back-to-browse-btn").classList.add("hidden");
        browseInit();
      }

      async function runSearch() {
        const query = document.getElementById("facts-search").value.trim();
        if (!query) {
          backToBrowse();
          return;
        }

        document.getElementById("facts-container").innerHTML =
          _memorySkeletonHtml;
        factsMode = "search";

        try {
          const res = await factsFetch(`${API}/api/user/facts/search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ query }),
          });
          if (!res.ok) throw new Error("Search failed");
          const data = await res.json();
          allFacts = data.facts || [];
          document.getElementById("domain-chips").classList.add("hidden");
          document
            .getElementById("back-to-browse-btn")
            .classList.remove("hidden");

          renderFacts(allFacts, true);
        } catch (e) {
          showToast("Search error: " + e.message, "error");
        }
      }

      function renderFacts(facts, isFirstLoad, instant = false) {
        const container = document.getElementById("facts-container");
        const countEl = document.getElementById("facts-count");
        const pageEl = document.getElementById("facts-pagination");

        if (isFirstLoad && facts.length === 0) {
          container.innerHTML = `<div style="grid-column: 1/-1; text-align:center; padding: 64px 20px;">
                                   <h3 style="color:var(--text-muted); font-weight:500;">Nothing found in memory.</h3>
                                 </div>`;
          countEl.textContent = "";
          pageEl.innerHTML = "";
          return;
        }

        const factsHtml = facts
          .map((f, i) => {
            const date = f.created_at
              ? new Date(f.created_at).toLocaleDateString("en-US", {
                  day: "numeric",
                  month: "short",
                  year: "numeric",
                })
              : "";
            const badge = factBadge(f);
            const domain = badge
              ? `<span class="fact-domain-badge">${escapeHtml(badge)}</span>`
              : "";
            const factIndex = allFacts.findIndex((fact) => fact.id === f.id);

            return `
        <div class="card fact-card reveal${instant ? " visible" : ""}" style="transition-delay: ${instant ? 0 : Math.min(i, 8) * 40}ms;">
          <div class="card-body" style="display:flex; flex-direction:column; height:100%;">
            <div class="fact-text">${escapeHtml(f.text)}</div>
            
            <div class="fact-meta">
              ${domain}
              
              <div class="fact-actions">
                <button data-idx="${factIndex}" onclick="openCorrectModal(this.dataset.idx)" class="btn btn-secondary btn-sm">Edit</button>
                <button data-idx="${factIndex}" onclick="openInvalidModal(this.dataset.idx)" class="btn btn-danger btn-sm">Delete</button>
              </div>
            </div>
          </div>
        </div>`;
          })
          .join("");

        if (isFirstLoad) container.innerHTML = factsHtml;
        else container.insertAdjacentHTML("beforeend", factsHtml);

        observeElements();

        countEl.textContent = `Showing ${allFacts.length} facts`;
        pageEl.innerHTML = browseCursor
          ? `<button onclick="browseNextPage()" class="btn btn-secondary">Load more</button>`
          : "";
      }

      function renderDomainChips() {
        const chips = [
          { label: "All", value: "all" },
          ...factDomains.map((d) => ({ label: d.label, value: d.key })),
        ];
        document.getElementById("domain-chips").innerHTML = chips
          .map(
            (c) =>
              `<button id="chip-${escapeHtml(c.value)}" data-domain="${escapeHtml(c.value)}" onclick="setDomainFilter(this.dataset.domain)" class="chip ${c.value === browseDomain ? "active" : ""}">${escapeHtml(c.label)}</button>`,
          )
          .join("");
      }

      /* ── Integrations ────────────────────────────────── */
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

      function renderPlatforms(platforms) {
        const container = document.getElementById("platforms-container");
        container.innerHTML = `
          ${platformCard("slack", platforms.slack, "Slack", t("platform.slack_desc"), "U0AC...")}
          ${platformCard("telegram", platforms.telegram, "Telegram", t("platform.telegram_desc"), "123456789")}
        `;
        observeElements();
      }

      function platformCard(id, linkedId, label, description, placeholder) {
        const linked = !!linkedId;
        return `
        <div class="card integration-card reveal">
          <div class="integration-icon">
            ${id === "slack" ? `<svg width="32" height="32" viewBox="0 0 24 24" fill="#E01E5A"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.523-2.522v-2.522h2.523zM15.165 17.688a2.527 2.527 0 0 1-2.523-2.523 2.526 2.526 0 0 1 2.523-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>` : `<svg width="32" height="32" viewBox="0 0 24 24" fill="#0088cc"><path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221l-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.446 1.394c-.14.18-.357.295-.6.295-.002 0-.003 0-.005 0l.213-3.054 5.56-5.022c.24-.213-.054-.334-.373-.121l-6.869 4.326-2.96-.924c-.64-.203-.658-.64.135-.954l11.566-4.458c.538-.196 1.006.128.832.94z"/></svg>`}
          </div>
          <h3 class="integration-title">${label}</h3>
          <p class="integration-description">${description}</p>

          <div class="integration-status">
            <div class="status-dot ${linked ? "connected" : "disconnected"}"></div>
            <span>${linked ? `${t("platform.connected")}: ${escapeHtml(linkedId)}` : t("platform.not_connected")}</span>
          </div>

          <div class="integration-actions">
            ${
              linked
                ? `<button onclick="unlinkPlatform('${id}')" class="btn btn-secondary">${t("platform.disconnect")}</button>`
                : `<input id="input-${id}" type="text" placeholder="${placeholder}" class="inp" />
                   <button onclick="linkPlatform('${id}', '${label}')" class="btn btn-black">${t("platform.link_account")}</button>`
            }
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

      // An <a> button to `href`, or nothing when the integration has no connector here.
      function connectButton(href, label) {
        return href ? `<a href="${escapeHtml(href)}" class="btn btn-black">${label}</a>` : "";
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

      function renderGmailStatus(data) {
        const container = document.getElementById("gmail-container");

        const linked = data.connected;
        // R1: the saved connection is gone while Gmail was connected before (SECRETS_AT_REST_RFC §6.2)
        const missing = !linked && !!data.reconnect_needed;
        const needsReconnect = (linked && !!data.needs_reconnect) || missing;
        const indexActive = linked && !!data.indexing_active;
        const activeJobId = data.active_job_id || null;

        let statusText = needsReconnect
          ? t("gmail.needs_reconnect")
          : linked ? t("platform.connected") : t("platform.not_connected");
        const statusDotClass = needsReconnect
          ? "needs-reconnect"
          : linked ? "connected" : "disconnected";
        const reconnectHtml = needsReconnect
          ? `<div style="margin-bottom:16px; padding:12px 16px; border-radius:12px; border:1px solid var(--accent-danger); color:var(--accent-danger); font-size:14px;">${missing ? t("gmail.reconnect_hint_missing") : t("gmail.reconnect_hint")}</div>`
          : "";

        let metaHtml = "";
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
            <div style="font-size:14px; color:var(--text-muted); margin-bottom:16px;">
              <b>Coverage:</b> ${cov}
              ${data.indexed_through ? `<br><a href="#" onclick="deleteGmailData(); return false;" style="color:var(--accent-danger); font-size:13px; margin-top:4px; display:inline-block;">Delete Data</a>` : ""}
            </div>
          `;
        }

        let autoIndexHtml = "";
        let dailyReviewHtml = "";
        if (linked) {
          const makeHourOptions = (selectedHour) =>
            Array.from({length: 24}, (_, h) =>
              `<option value="${h}" ${h === selectedHour ? "selected" : ""}>${String(h).padStart(2,"0")}:00</option>`
            ).join("");

          const aiEnabled = !!data.auto_index_enabled;
          const aiHour = data.auto_index_hour ?? 8;
          autoIndexHtml = `
            <div style="margin-bottom:16px; padding:12px 16px; background:var(--bg-active); border-radius:12px;">
              <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
                <label style="font-size:14px; font-weight:500; cursor:pointer;" for="ai-toggle">${t("gmail.auto_index")}</label>
                <input type="checkbox" id="ai-toggle" ${aiEnabled ? "checked" : ""}
                  onchange="document.getElementById('ai-hour-row').style.display=this.checked?'flex':'none'; saveGmailAutoIndex(this.checked, parseInt(document.getElementById('ai-hour').value))"
                  style="width:18px;height:18px;cursor:pointer;">
              </div>
              <div id="ai-hour-row" style="display:${aiEnabled ? "flex" : "none"}; align-items:center; gap:8px; margin-top:10px;">
                <span style="font-size:13px; color:var(--text-muted);">${t("gmail.run_at")}</span>
                <select id="ai-hour" onchange="saveGmailAutoIndex(true, parseInt(this.value))"
                  style="font-size:13px; padding:4px 8px; border-radius:8px; border:1px solid var(--border); background:var(--bg-card); color:var(--text-primary);">
                  ${makeHourOptions(aiHour)}
                </select>
                <span style="font-size:13px; color:var(--text-muted);">${t("gmail.in_your_tz")}</span>
              </div>
            </div>
          `;

          const drEnabled = !!data.daily_review_enabled;
          const drHour = data.daily_review_hour ?? 9;
          dailyReviewHtml = `
            <div style="margin-bottom:16px; padding:12px 16px; background:var(--bg-active); border-radius:12px;">
              <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
                <label style="font-size:14px; font-weight:500; cursor:pointer;" for="dr-toggle">${t("gmail.daily_review")}</label>
                <input type="checkbox" id="dr-toggle" ${drEnabled ? "checked" : ""}
                  onchange="document.getElementById('dr-hour-row').style.display=this.checked?'flex':'none'; saveDailyReview(this.checked, parseInt(document.getElementById('dr-hour').value))"
                  style="width:18px;height:18px;cursor:pointer;">
              </div>
              <div id="dr-hour-row" style="display:${drEnabled ? "flex" : "none"}; align-items:center; gap:8px; margin-top:10px;">
                <span style="font-size:13px; color:var(--text-muted);">${t("gmail.run_at")}</span>
                <select id="dr-hour" onchange="saveDailyReview(true, parseInt(this.value))"
                  style="font-size:13px; padding:4px 8px; border-radius:8px; border:1px solid var(--border); background:var(--bg-card); color:var(--text-primary);">
                  ${makeHourOptions(drHour)}
                </select>
                <span style="font-size:13px; color:var(--text-muted);">${t("gmail.in_your_tz")}</span>
              </div>
            </div>
          `;
        }

        let actionButtons = missing
          ? connectButton(data.connect_href, t("gmail.reconnect"))
          : !linked
          ? connectButton(data.connect_href, t("platform.connect_gmail"))
          : needsReconnect
          ? `${connectButton(data.reconnect_href, t("gmail.reconnect"))}
             <button onclick="disconnectGmail()" class="btn btn-secondary">${t("platform.disconnect")}</button>`
          : `<button onclick="triggerGmailIndex()" class="btn btn-primary" ${indexActive ? "disabled" : ""}>${indexActive ? t("platform.indexing") : t("platform.index_emails")}</button>
             ${indexActive && activeJobId ? `<button data-job-id="${escapeHtml(activeJobId)}" onclick="cancelGmailIndex(this.dataset.jobId, this)" class="btn btn-secondary">${t("platform.cancel")}</button>` : ""}
             <button onclick="disconnectGmail()" class="btn btn-secondary">${t("platform.disconnect")}</button>`;

        container.innerHTML = `
          <div class="card integration-card gmail-card reveal">
            <div class="integration-icon">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#EA4335" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>
            </div>
            <h3 class="integration-title">Gmail</h3>
            <p class="integration-description">${t("platform.gmail_desc")}</p>

            <div class="integration-status">
              <div class="status-dot ${statusDotClass}"></div>
              <span>${statusText}</span>
            </div>

            ${reconnectHtml}
            ${metaHtml}
            ${autoIndexHtml}
            ${dailyReviewHtml}

            <div class="integration-actions">
              ${actionButtons}
            </div>
          </div>
        `;
        observeElements();
      }

      function triggerGmailIndex() {
        renderModal(
          "index-gmail",
          "Index Gmail",
          `<div style="margin-bottom: 24px;">
            <label style="margin-bottom: 12px; font-size:18px;">Index Mode</label>
            <div style="display:flex;flex-direction:column;gap:16px; background:var(--bg-active); padding:16px; border-radius:16px;">
              <label style="display:flex;align-items:center;gap:12px;font-weight:500;cursor:pointer; font-size:16px; margin:0;">
                <input type="radio" name="index-mode" value="incremental" checked onchange="updateIndexModal()" style="width:20px;height:20px;"> Incremental (New only)
              </label>
              <label style="display:flex;align-items:center;gap:12px;font-weight:500;cursor:pointer; font-size:16px; margin:0;">
                <input type="radio" name="index-mode" value="backfill" onchange="updateIndexModal()" style="width:20px;height:20px;"> Backfill (To specific date)
              </label>
            </div>
          </div>
          <div id="backfill-date-row" style="display:none; margin-bottom: 24px;">
            <label for="backfill-until-input">Index until date</label>
            <input type="date" id="backfill-until-input" class="inp" />
          </div>
          <div id="index-pages-row" style="margin-bottom: 24px;">
            <label for="index-pages-input">Max pages to fetch</label>
            <input type="number" id="index-pages-input" class="inp" value="1" min="1" placeholder="Empty = fetch all" />
          </div>`,
          `<button onclick="closeModal()" class="btn btn-secondary">Cancel</button>
           <button id="modal-index-btn" onclick="submitIndexModal()" class="btn btn-primary">Start Indexing</button>`,
        );
      }

      function updateIndexModal() {
        const mode = document.querySelector(
          'input[name="index-mode"]:checked',
        ).value;
        document.getElementById("backfill-date-row").style.display =
          mode === "backfill" ? "block" : "none";
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

      async function loadDriveStatus() {
        try {
          const res = await authFetch(`${API}/api/drive/connections`, {
            credentials: "include",
          });
          if (!res.ok) throw new Error();
          const data = await res.json();
          data.dr_copy = await loadDeepResearchCopy();
          data.add_kinds = (await loadConnectors()).filter((c) => c.integration === "drive");
          for (const row of data.connections) {
            row.reconnect_href = row.reconnect
              ? `/auth/connect/${encodeURIComponent(row.kind)}?credential=${encodeURIComponent(row.reconnect.credential_id)}`
              : "";
            if (row.indexing) {
              // the index block of a row carries what its disclosure needs
              row.indexing.connection_id = row.connection_id;
              row.indexing.transcribe_enabled = row.transcribe_enabled;
              row.indexing.mirror_root = data.mirror_root;
              // The search disclosure mentions the copies while the copy is on (they are indexed like any file).
              row.indexing.dr_copy = data.dr_copy === true;
            }
          }
          // the poll runs while any drive's first build is running
          const scopeBusy = (ix) => { const sp = ix.scope_progress || {}; return !!(sp.removing_outside || sp.removing > 0 || sp.walking); };
          data.indexing = { building: data.connections.some((row) => row.indexing && (row.indexing.building || scopeBusy(row.indexing))) };
          const snapshot = JSON.stringify(data);
          // Re-render only when the data changed: the poll must not twitch the UI.
          if (snapshot !== _driveStatusSnapshot) {
            _driveStatusSnapshot = snapshot;
            driveData = data;
            renderDriveStatus(data);
          }
          scheduleDriveStatusPoll(!!(data.indexing && data.indexing.building));
        } catch {}
      }

      // While the first build runs the card's numbers move: re-poll every 2 min, only while the card shows
      // building, the Drives section is open and the tab is visible.
      const DRIVE_STATUS_POLL_MS = 120000;
      let _driveStatusSnapshot = null;
      let _driveStatusTimer = null;
      function scheduleDriveStatusPoll(building) {
        if (_driveStatusTimer) clearTimeout(_driveStatusTimer);
        _driveStatusTimer = null;
        if (!building) return;
        _driveStatusTimer = setTimeout(() => {
          _driveStatusTimer = null;
          if (document.visibilityState === "visible" && _activeSection === "drives") {
            loadDriveStatus();
          } else {
            scheduleDriveStatusPoll(true);
          }
        }, DRIVE_STATUS_POLL_MS);
      }

      // "Copy deep research reports to my drive" (decisions/deep_research_drive_copy.md): true/false, or
      // null when the setting could not be read (the switch is then not shown).
      async function loadDeepResearchCopy() {
        try {
          const res = await authFetch(`${API}/api/drive/deep-research-copy`, {
            credentials: "include",
          });
          if (!res.ok) return null;
          return !!(await res.json()).enabled;
        } catch {
          return null;
        }
      }

      let driveData = null;   // the last connection list: a local change redraws from it without a new read
      const driveUi = {};     // connection_id -> {open, tab, picker, foldersBeforeWhole, nameError, nameWarning, scopeError, folderError, confirm}
      let driveAddError = "";
      const driveUiOf = (id) => (driveUi[id] = driveUi[id] || {});
      const redrawDrive = () => { if (driveData) renderDriveStatus(driveData); };
      const driveRowOf = (id) => (driveData ? driveData.connections.find((c) => c.connection_id === id) : null);

      const DRIVE_TABS = ["overview", "access", "index", "settings"];
      const DRIVE_ICONS = {
        folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
        chevR: '<path d="M9 6l6 6-6 6"/>',
        chevD: '<path d="M6 9l6 6 6-6"/>',
        lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
        x: '<path d="M6 6l12 12M18 6L6 18"/>',
        alert: '<path d="M12 4l9.5 16h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
        check: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
        pause: '<circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/>',
        drive: '<path d="M4.5 14.5L7.5 5h9l3 9.5"/><rect x="3" y="14.5" width="18" height="5" rx="1.5"/><path d="M16.5 17h.01"/>',
        search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
        eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
        shared: '<circle cx="9" cy="9" r="3"/><path d="M3.5 19c0-3 2.6-5 5.5-5s5.5 2 5.5 5"/><circle cx="17" cy="8.5" r="2.3"/><path d="M16.5 13.5c2.4.2 4 2 4 4.5"/>',
        home: '<path d="M4 10.5L12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z"/>',
        info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/>',
      };
      function driveIcon(name, cls) {
        return `<svg class="dv-ic ${cls || ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${DRIVE_ICONS[name]}</svg>`;
      }

      const driveNum = (v) => Number(v) || 0;
      const driveFmt = (n) => driveNum(n).toLocaleString();
      // A dictionary text with {placeholders}; the values are escaped (the result goes into innerHTML).
      function driveText(key, vars) {
        return t(key).replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? escapeHtml(vars[k]) : m));
      }
      // The last segment of a connection-relative folder path ("" when the path is unknown).
      function driveLast(path) {
        const parts = String(path || "").split("/").filter(Boolean);
        return parts.length ? parts[parts.length - 1] : "";
      }
      const driveFolderName = (f) => driveLast(f.path) || f.id;

      // What the drive's scope means to the page. `empty_means` comes from the server and is the only thing that tells an
      // empty scope apart: "area" (the bot's own folder is seen), "nothing" (an account-wide drive: no files are seen),
      // null (the server could not tell). A stored whole_drive on an "area" drive reads as empty.
      function driveScopeView(row) {
        const scope = row.scope || {};
        const means = scope.empty_means || null;
        const folders = scope.folders || [];
        const whole = !!scope.whole_drive && means !== "area";
        const empty = !whole && folders.length === 0;
        return { whole, folders, empty, means, nothing: empty && means === "nothing", area: empty && means === "area" };
      }

      // Everything the summary, the banners and the tabs derive from a row.
      function driveModel(row) {
        const ix = row.indexing || null;
        const sv = driveScopeView(row);
        const c = (ix && ix.counters) || {};
        const searchOn = !!(ix && ix.enabled);
        const searchable = driveNum(c.carded);
        // Waiting is `pending` only: `unresolved` (files whose parent chain is not yet checked against the scope; most
        // leave the index) has its own line, and `not_read_yet` is listed under "All counters".
        const waiting = driveNum(c.pending);
        const checking = driveNum(c.unresolved);
        const sp = (ix && ix.scope_progress) || {};
        let scopeLine = "";
        if (searchOn && !(row.reconnect)) {
          if (sp.removing_outside) scopeLine = t("drive.scope_removing_outside");
          else if (driveNum(sp.removing) > 0) scopeLine = driveText("drive.scope_removing", { n: driveFmt(driveNum(sp.removing)) });
          else if (sp.walking) {
            scopeLine = driveText("drive.scope_walking", { n: driveFmt(driveNum(sp.listed)) });
            if (driveNum(sp.blocked) > 0) scopeLine += `<div class="dv-row-sub">${driveText("drive.scope_blocked", { n: driveFmt(driveNum(sp.blocked)) })}</div>`;
          }
        }
        const cantRead = driveNum(c.inaccessible) + driveNum(c.failed);
        const building = searchOn && !!(ix.building || ix.awaiting_rebuild);
        const pct = building && searchable + waiting > 0
          ? Math.min(99, Math.round((100 * searchable) / (searchable + waiting))) : null;
        return {
          ix, sv, searchOn, searchable, waiting, checking, cantRead, building, pct, scopeLine,
          hasIndex: !!(ix && (ix.available || ix.enabled)),
          needsReconnect: !!row.reconnect,
          full: searchOn && !!ix.index_full,
          emptyIndex: searchOn && sv.nothing,
        };
      }

      // The state chip, by priority: reconnect, nothing selected, index full, indexing, connected.
      function driveChip(m) {
        if (m.needsReconnect) return { cls: "danger", text: t("drive.needs_reconnect") };
        if (m.sv.nothing) return { cls: "warn", text: t("drive.chip_nothing") };
        if (m.full) return { cls: "warn", text: t("drive.chip_full") };
        if (m.building) {
          return { cls: "info", text: m.pct !== null ? driveText("drive.chip_indexing", { pct: m.pct }) : t("drive.chip_indexing_plain") };
        }
        return { cls: "ok", text: t("platform.connected") };
      }

      // The collapsed card's third line: what the bot sees, where it saves, whether search is ready.
      function driveSummaryParts(row, m) {
        const sv = m.sv;
        const wf = row.write_folder;
        const parts = [];
        if (sv.whole) parts.push({ text: t("drive.sees_whole") });
        else if (sv.area) parts.push({ text: t("drive.sees_area") });
        else if (sv.nothing) parts.push({ text: t("drive.sees_nothing"), cls: "w" });
        else if (sv.empty) parts.push({ text: t("drive.sees_default") });
        else parts.push({ text: driveText(sv.folders.length === 1 ? "drive.sees_one" : "drive.sees_n", { n: sv.folders.length }) });
        if (!wf) parts.push({ text: t("drive.saves_none") });
        else if (wf.id === "") parts.push({ text: t(sv.means === "area" ? "drive.saves_area" : "drive.saves_top") });
        else parts.push({ text: driveLast(wf.path) ? driveText("drive.saves_to", { name: driveLast(wf.path) }) : t("drive.saves_folder") });
        if (m.hasIndex) {
          if (!m.searchOn) parts.push({ text: t("drive.search_off") });
          else if (m.needsReconnect) parts.push({ text: t("drive.search_paused"), cls: "d" });
          else if (m.emptyIndex) parts.push({ text: t("drive.search_empty"), cls: "w" });
          else if (m.building) parts.push({ text: m.pct !== null ? driveText("drive.search_pct", { pct: m.pct }) : t("drive.search_building") });
          else parts.push({ text: driveText("drive.search_count", { n: driveFmt(m.searchable) }) });
        }
        return parts;
      }

      function renderDriveStatus(data) {
        const container = document.getElementById("drive-container");
        // Not configured on this deployment: show no card rather than a dead connect button.
        if (!data.connections.length && !data.add_kinds.length) {
          container.innerHTML = "";
          return;
        }
        const linked = data.connections.length > 0;
        // The copy switch: offered only while a drive is linked; disconnecting the last drive turns it off server-side.
        // Reports are copied to the primary drive, so the switch sits in its settings; here only while no drive is primary.
        const drCopyHtml = linked && typeof data.dr_copy === "boolean" && !data.connections.some((c) => c.primary)
          ? `<div class="dv-box">${renderDeepResearchCopy(data.dr_copy)}</div>`
          : "";
        const rowsHtml = data.connections.map((row) => renderDriveConnection(row, data)).join("");
        container.innerHTML = `
          <div class="dv-head">
            <h3>${t("platform.drive_title")}</h3>
            <p>${t("platform.drive_desc")}</p>
          </div>
          ${linked ? "" : `<div class="integration-status"><div class="status-dot disconnected"></div><span>${t("platform.not_connected")}</span></div>`}
          <div class="dv-list">${rowsHtml}</div>
          ${drCopyHtml}
          ${renderAddDrive(data)}
        `;
        observeElements();
      }

      // One drive of the list (MULTI_DRIVE_RFC §11.7): a summary that opens into the problem banners and four tabs.
      function renderDriveConnection(row, data) {
        const id = row.connection_id;
        const ui = driveUiOf(id);
        const q = escapeHtml(id);
        const m = driveModel(row);
        // Collapsed to its summary; it stays open across redraws, and opens by itself when it needs a reconnect
        const open = ui.open === undefined ? m.needsReconnect : ui.open;
        const chip = driveChip(m);
        const parts = driveSummaryParts(row, m);
        const provider = [row.provider_label, row.account_label].filter(Boolean).map(escapeHtml).join(" · ");
        return `
          <article class="card dv-card${open ? " open" : ""}" id="drive-card-${q}">
            <button type="button" class="dv-sum" onclick="toggleDriveCard('${q}')" aria-expanded="${open}" aria-controls="drive-body-${q}">
              <span class="dv-prov">${driveIcon("drive")}</span>
              <span class="dv-sum-main">
                <span class="dv-sum-top">
                  <span class="dv-sum-name">${escapeHtml(row.mount_name)}</span>
                  ${row.primary ? `<span class="dv-badge">${t("drive.primary_badge")}</span>` : ""}
                  <span class="dv-chip ${chip.cls}">${chip.text}</span>
                </span>
                <span class="dv-sum-sub">${provider}</span>
                <span class="dv-sum-line">${parts.map((p) => `<span class="${p.cls || ""}">${p.text}</span>`).join("")}</span>
              </span>
              ${driveIcon("chevD", "dv-chev")}
            </button>
            ${open ? `<div class="dv-body" id="drive-body-${q}">${renderDriveBody(row, data, m, ui, q)}</div>` : ""}
          </article>`;
      }

      function renderDriveBody(row, data, m, ui, q) {
        const tabs = DRIVE_TABS.filter((k) => k !== "index" || m.hasIndex);
        const tab = tabs.includes(ui.tab) ? ui.tab : "overview";
        let panel = "";
        if (tab === "overview") panel = renderDriveOverview(row, m, q);
        else if (tab === "access") panel = renderDriveAccess(row, m, q);
        else if (tab === "index") panel = renderDriveIndexing(m.ix, true);
        else panel = renderDriveSettings(row, data, ui, q) + renderDriveDisconnect(row, data, ui, q);
        return `
          ${renderDriveBanners(row, m, ui, q)}
          <div class="dv-tabs" role="tablist" aria-label="${escapeHtml(row.mount_name)}">
            ${tabs.map((k) => `<button type="button" role="tab" id="drive-tab-${q}-${k}" aria-selected="${k === tab}" aria-controls="drive-panel-${q}"
              tabindex="${k === tab ? 0 : -1}" onclick="selectDriveTab('${q}', '${k}')" onkeydown="driveTabKey(event, '${q}')">${t("drive.tab_" + k)}</button>`).join("")}
          </div>
          <div class="dv-panel" role="tabpanel" id="drive-panel-${q}" aria-labelledby="drive-tab-${q}-${tab}">${panel}</div>`;
      }

      // The problems of the drive, at the top of the opened card, each with its action.
      function renderDriveBanners(row, m, ui, q) {
        const banners = [];
        if (m.needsReconnect) {
          const keeps = !!(m.ix && m.ix.enabled && m.ix.index_kept);
          banners.push(`<div class="dv-alert danger">${driveIcon("alert")}<div class="dv-alert-main">
            <div class="dv-alert-title">${t("drive.banner_reconnect_title")}</div>
            <div class="dv-alert-text">${keeps ? t("drive.reconnect_hint") : t("drive.reconnect_hint_neutral")}</div>
            <div class="dv-alert-actions">${connectButton(row.reconnect_href, t("drive.reconnect"))}</div></div></div>`);
        } else if (m.sv.nothing) {
          banners.push(`<div class="dv-alert warn">${driveIcon("eye")}<div class="dv-alert-main">
            <div class="dv-alert-title">${t("drive.banner_empty_title")}</div>
            <div class="dv-alert-text">${t("drive.banner_empty_text")}${m.searchOn ? " " + t("drive.banner_empty_search") : ""}</div>
            <div class="dv-alert-actions">
              <button type="button" class="btn btn-primary dv-btn-sm" onclick="openDrivePicker('${q}', 'scope')">${t("drive.choose_folders")}</button>
              <button type="button" class="btn btn-secondary dv-btn-sm" onclick="toggleDriveScopeWhole('${q}', true)">${t("drive.use_whole")}</button>
            </div></div></div>`);
        }
        if (m.full) {
          banners.push(`<div class="dv-alert danger">${driveIcon("alert")}<div class="dv-alert-main">
            <div class="dv-alert-title">${t("drive.index_full")}</div>
            <div class="dv-alert-text">${t("drive.banner_full_text")}</div>
            <div class="dv-alert-actions"><button type="button" class="btn btn-secondary dv-btn-sm" ${m.needsReconnect ? "disabled" : ""}
              onclick="openDrivePicker('${q}', 'scope')">${t("drive.review_folders")}</button></div></div></div>`);
        }
        if (m.searchOn && m.ix.last_error && !m.needsReconnect) {
          const category = DRIVE_ERROR_CATEGORIES.includes(m.ix.last_error) ? m.ix.last_error : "internal";
          banners.push(`<div class="dv-alert note">${driveIcon("info", "sm")}<span>${t("drive.index_problem")}: ${t("drive.err." + category)}</span></div>`);
        }
        if (ui.scopeError) banners.push(`<div class="dv-alert danger" role="alert">${driveIcon("alert")}<div class="dv-alert-main">${escapeHtml(ui.scopeError)}</div></div>`);
        if (ui.folderError) banners.push(`<div class="dv-alert danger" role="alert">${driveIcon("alert")}<div class="dv-alert-main">${escapeHtml(ui.folderError)}</div></div>`);
        return banners.length ? `<div class="dv-alerts">${banners.join("")}</div>` : "";
      }

      function driveSeesValue(row, m) {
        const sv = m.sv;
        if (sv.whole) return t("drive.whole_title");
        if (sv.area) return t("drive.own_folder");
        if (sv.nothing) return t("drive.ov_nothing");
        if (sv.empty) return t("drive.scope_default");
        return sv.folders.map((f) => escapeHtml(driveFolderName(f))).join(", ");
      }

      // The connector access (DRIVE_MCP_CONNECTOR_RFC §7.6): nothing (the default), the whole drive, or chosen folders.
      function driveConnectorView(row) {
        const ca = row.connector_access || {};
        const mode = ca.mode === "whole" || ca.mode === "folders" ? ca.mode : "nothing";
        return { mode, folders: mode === "folders" ? (ca.folders || []) : [] };
      }

      function driveConnectorValue(row) {
        const cv = driveConnectorView(row);
        if (cv.mode === "whole") return t("drive.connector_whole");
        if (cv.mode === "folders") return driveText("drive.connector_n", { n: cv.folders.length });
        return t("drive.connector_nothing");
      }

      function driveWriteValue(row, m) {
        const wf = row.write_folder;
        if (!wf) return { icon: "lock", text: t("drive.write_none_title"), sub: t("drive.write_none_sub") };
        if (wf.id === "") return { icon: "folder", text: t(m.sv.means === "area" ? "drive.own_folder" : "drive.write_top"), sub: "" };
        return { icon: "folder", text: escapeHtml(wf.path || wf.id), sub: "" };
      }

      function renderDriveOverview(row, m, q) {
        const edit = (tab) => `<button type="button" class="dv-link" onclick="selectDriveTab('${q}', '${tab}')">${t("drive.edit")}</button>`;
        const line = (label, value, tab, warn) => `
          <div class="dv-row"><div class="dv-row-main"><div class="dv-row-sub dv-label">${label}</div>
          <div class="dv-row-title${warn ? " warn" : ""}">${value}</div></div>${edit(tab)}</div>`;
        let search = "";
        if (m.hasIndex) {
          if (!m.searchOn) search = t("drive.ov_search_off");
          else if (m.needsReconnect) search = t("drive.status_paused");
          else if (m.emptyIndex) search = t("drive.ov_search_empty");
          else if (m.scopeLine) search = m.scopeLine;
          else if (m.building) {
            search = (m.pct !== null ? driveText("drive.ov_search_building", { pct: m.pct }) + `<div class="dv-bar"><span style="width:${m.pct}%"></span></div>` : t("drive.index_building"));
          } else search = driveText("drive.ov_search_ok", { n: driveFmt(m.searchable) });
          if (m.searchOn && m.checking > 0 && !m.scopeLine) search += `<div class="dv-row-sub">${driveText("drive.index_checking", { n: driveFmt(m.checking) })}</div>`;
        }
        const w = driveWriteValue(row, m);
        return `
          <div class="dv-group">
            ${line(t("drive.sees_title"), driveSeesValue(row, m), "access", m.sv.nothing)}
            ${line(t("drive.saves_title"), w.text, "access", false)}
            ${line(t("drive.connector_title"), driveConnectorValue(row), "access", false)}
            ${m.hasIndex ? line(t("drive.ov_search"), search, "index", m.emptyIndex) : ""}
            ${line(t("drive.searched_by_default"), t(row.searched_by_default ? "drive.yes" : "drive.no"), "settings", false)}
          </div>
          ${row.note ? `<div class="dv-group"><div class="dv-quote"><div class="dv-row-sub">${t("drive.note_title")}</div>${escapeHtml(row.note)}</div></div>` : ""}`;
      }

      function renderDriveAccess(row, m, q) {
        const sv = m.sv;
        const wf = row.write_folder;
        const locked = m.needsReconnect;
        const lockAttr = locked ? ` disabled title="${escapeHtml(t("drive.reconnect_to_browse"))}"` : "";
        const wholeSub = t(sv.means === "nothing" ? "drive.whole_sub_account" : "drive.whole_sub_generic");
        let rows = "";
        if (sv.whole) {
          rows = `<div class="dv-frow">${driveIcon("drive", "fi")}<div class="dv-row-main"><div class="dv-row-title">${t("drive.whole_title")}</div>
            <div class="dv-row-sub">${wholeSub}</div></div>
            <button type="button" class="dv-icon-btn" onclick="toggleDriveScopeWhole('${q}', false)" aria-label="${escapeHtml(t("drive.stop_whole"))}">${driveIcon("x", "sm")}</button></div>`;
        } else if (sv.area) {
          rows = `<div class="dv-frow">${driveIcon("home", "fi")}<div class="dv-row-main"><div class="dv-row-title">${t("drive.own_folder")}</div>
            <div class="dv-row-sub">${t("drive.own_folder_sub")}</div></div></div>`;
        } else if (sv.nothing) {
          rows = `<div class="dv-empty-scope">${driveIcon("eye")}<span><strong>${t("drive.nothing_selected")}</strong> ${t("drive.nothing_text")}</span></div>`;
        } else if (sv.empty) {
          rows = `<div class="dv-frow">${driveIcon("home", "fi")}<div class="dv-row-main"><div class="dv-row-title">${t("drive.scope_default")}</div></div></div>`;
        } else {
          rows = sv.folders.map((f, i) => `<div class="dv-frow">${driveIcon("folder", "fi")}<div class="dv-row-main"><div class="dv-row-title">${escapeHtml(f.path || f.id)}
            ${wf && wf.id === f.id ? `<span class="dv-tag">${t("drive.saves_here_tag")}</span>` : ""}</div>
            ${f.missing ? `<div class="dv-row-sub dv-warn-text">${t("drive.folder_missing")}</div>` : ""}</div>
            <button type="button" class="dv-icon-btn" onclick="removeDriveScopeFolder('${q}', ${i})" aria-label="${escapeHtml(driveText("drive.stop_showing", { name: driveFolderName(f) }))}">${driveIcon("x", "sm")}</button></div>`).join("");
        }
        const explain = t(sv.means === "area" ? "drive.sees_explain_area" : "drive.sees_explain_account");
        const w = driveWriteValue(row, m);
        const writeLocked = locked || sv.nothing;
        return `
          <div class="dv-group">
            <div class="dv-row"><div class="dv-row-main"><div class="dv-row-title">${t("drive.sees_title")}</div><div class="dv-row-sub">${explain}</div></div>
              <button type="button" class="btn ${sv.nothing ? "btn-primary" : "btn-secondary"} dv-btn-sm" onclick="openDrivePicker('${q}', 'scope')"${lockAttr}>${t(sv.nothing ? "drive.choose_folders" : "drive.change")}</button></div>
            ${rows}
          </div>
          <div class="dv-group">
            <div class="dv-row"><div class="dv-row-main"><div class="dv-row-title">${t("drive.saves_title")}</div><div class="dv-row-sub">${t("drive.saves_explain")}</div></div>
              <button type="button" class="btn btn-secondary dv-btn-sm" onclick="openDrivePicker('${q}', 'write')"${writeLocked ? (locked ? lockAttr : " disabled") : ""}>${t("drive.change")}</button></div>
            <div class="dv-frow">${driveIcon(w.icon, "fi" + (w.icon === "lock" ? " lock" : ""))}<div class="dv-row-main"><div class="dv-row-title">${w.text}</div>
              ${w.sub ? `<div class="dv-row-sub">${w.sub}</div>` : ""}</div></div>
          </div>
          ${renderDriveConnector(row, q, lockAttr, locked)}`;
      }

      // The third block of the access tab: what the drive connector may read. Nothing / everything the bot sees /
      // chosen folders (the picker, limited to the scope); the server checks every choice.
      function renderDriveConnector(row, q, lockAttr, locked) {
        const cv = driveConnectorView(row);
        const ui = driveUiOf(row.connection_id);
        const pick = (mode, key) => `<button type="button" class="btn ${cv.mode === mode ? "btn-primary" : "btn-secondary"} dv-btn-sm"
          aria-pressed="${cv.mode === mode}" ${mode === "folders" ? `onclick="openDrivePicker('${q}', 'connector')"${lockAttr}`
          : `onclick="saveDriveConnectorAccess('${q}', '${mode}', [], false)"${locked ? " disabled" : ""}`}>${t(key)}</button>`;
        let current = "";
        if (cv.mode === "whole") current = `<div class="dv-frow">${driveIcon("drive", "fi")}<div class="dv-row-main"><div class="dv-row-title">${t("drive.connector_whole")}</div></div></div>`;
        else if (cv.mode === "folders") current = cv.folders.map((f) => `<div class="dv-frow">${driveIcon("folder", "fi")}<div class="dv-row-main"><div class="dv-row-title">${escapeHtml(f.path || f.id)}</div></div></div>`).join("");
        else current = `<div class="dv-frow">${driveIcon("lock", "fi lock")}<div class="dv-row-main"><div class="dv-row-title">${t("drive.connector_nothing")}</div></div></div>`;
        return `
          <div class="dv-group" id="drive-connector-${q}">
            <div class="dv-row"><div class="dv-row-main"><div class="dv-row-title">${t("drive.connector_title")}</div><div class="dv-row-sub">${t("drive.connector_explain")}</div></div></div>
            ${current}
            <div class="dv-btns">${pick("nothing", "drive.connector_nothing")}${pick("whole", "drive.connector_whole")}${pick("folders", "drive.connector_choose")}</div>
            ${ui.connectorError ? `<div class="dv-msg err" role="alert">${escapeHtml(ui.connectorError)}</div>` : ""}
          </div>`;
      }

      function renderDriveSettings(row, data, ui, q) {
        return `
          <div class="dv-group">
            <div class="dv-field"><label for="drive-name-${q}">${t("drive.name")}</label>
              <div class="dv-inline"><input id="drive-name-${q}" type="text" value="${escapeHtml(row.mount_name)}" maxlength="64">
                <button type="button" class="btn btn-secondary dv-btn-sm" onclick="renameDrive('${q}')">${t("drive.save")}</button></div>
              ${ui.nameError ? `<div class="dv-msg err" role="alert">${escapeHtml(ui.nameError)}</div>` : ""}
              ${ui.nameWarning ? `<div class="dv-msg warn">${escapeHtml(ui.nameWarning)}</div>` : ""}</div>
            <div class="dv-row"><div class="dv-row-main"><div class="dv-row-title">${t("drive.primary_title")}</div>
              <div class="dv-row-sub">${t(row.primary ? "drive.primary_yes" : "drive.primary_no")}</div></div>
              ${row.primary ? "" : `<button type="button" class="btn btn-secondary dv-btn-sm" onclick="makeDrivePrimary('${q}')">${t("drive.make_primary")}</button>`}</div>
            <div class="dv-row"><div class="dv-row-main"><label class="dv-row-title" for="drive-sbd-${q}">${t("drive.searched_by_default")}</label>
              <div class="dv-row-sub">${t("drive.searched_by_default_sub")}</div></div>
              <input type="checkbox" class="dv-switch" id="drive-sbd-${q}" ${row.searched_by_default ? "checked" : ""} onchange="saveDriveSearchedByDefault(this, '${q}')"></div>
            ${row.primary && typeof data.dr_copy === "boolean" ? `<div class="dv-row dv-stack">${renderDeepResearchCopy(data.dr_copy)}</div>` : ""}
            ${row.transcribe_enabled ? `<div class="dv-row"><div class="dv-row-main"><div class="dv-row-title">${t("drive.transcribe_title")}</div></div><span class="dv-row-value">${t("drive.on")}</span></div>` : ""}
          </div>
          ${row.note ? `<div class="dv-group"><div class="dv-quote"><div class="dv-row-sub">${t("drive.note_title")}</div>${escapeHtml(row.note)}</div></div>` : ""}`;
      }

      // Disconnect: a quiet link at the bottom of the settings, then its confirmation.
      function renderDriveDisconnect(row, data, ui, q) {
        const heirs = data.connections.filter((c) => c.connection_id !== row.connection_id && c.state === "active" && c.write_folder);
        if (!ui.confirm) {
          return `<div class="dv-disc"><button type="button" class="dv-link danger" onclick="askDisconnectDrive('${q}')">${driveText("drive.disconnect_named", { name: row.mount_name })}</button></div>`;
        }
        return `<div class="dv-disc"><div class="dv-confirm"><p>${escapeHtml(t("drive.disconnect_confirm").replace("{name}", row.mount_name))}</p>
          ${row.primary && heirs.length
            ? `<div class="dv-field"><label for="drive-heir-${q}">${t("drive.heir_pick")}</label>
               <select id="drive-heir-${q}">${heirs.map((h) => `<option value="${escapeHtml(h.connection_id)}">${escapeHtml(h.mount_name)}</option>`).join("")}</select></div>`
            : ""}
          <div class="dv-btns"><button type="button" class="btn btn-danger dv-btn-sm" onclick="disconnectDrive('${q}')">${t("platform.disconnect")}</button>
          <button type="button" class="btn btn-secondary dv-btn-sm" onclick="cancelDisconnectDrive('${q}')">${t("drive.cancel")}</button></div></div></div>`;
      }

      function toggleDriveCard(id) {
        const row = driveRowOf(id);
        if (!row) return;
        const ui = driveUiOf(id);
        ui.open = !(ui.open === undefined ? !!row.reconnect : ui.open);
        redrawDrive();
      }

      // The open tab is kept per connection (driveUi), so a redraw or a poll never changes it.
      function selectDriveTab(id, tab) {
        driveUiOf(id).tab = tab;
        redrawDrive();
        const el = document.getElementById(`drive-tab-${id}-${tab}`);
        if (el) el.focus();
      }

      function driveTabKey(event, id) {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        const row = driveRowOf(id);
        if (!row) return;
        const tabs = DRIVE_TABS.filter((k) => k !== "index" || driveModel(row).hasIndex);
        const ui = driveUiOf(id);
        const at = Math.max(0, tabs.indexOf(ui.tab || "overview"));
        event.preventDefault();
        selectDriveTab(id, tabs[(at + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]);
      }

      // "Add a drive": the kinds come from /api/connectors (no kind is named here); the typed name is kept across the
      // provider round trip and given to the new connection when the page returns (finishAddedDrive).
      function renderAddDrive(data) {
        const buttons = data.add_kinds.map((c) =>
          `<button type="button" class="btn btn-black dv-btn-sm" onclick="addDrive('${escapeHtml(c.kind)}')">${escapeHtml(c.label)}</button>`).join("");
        return `
          <section class="card dv-add" aria-labelledby="drive-add-title">
            <h3 id="drive-add-title">${t("drive.add_title")}</h3>
            <div class="dv-field">
              <label for="drive-add-name">${t("drive.name")}</label>
              <input id="drive-add-name" type="text" value="${escapeHtml(data.default_name || "")}" maxlength="64">
            </div>
            <div class="dv-btns">${buttons}</div>
            ${driveAddError ? `<div class="dv-msg err" role="alert">${escapeHtml(driveAddError)}</div>` : ""}
            <p class="dv-hint">${t("drive.add_hint")}</p>
          </section>`;
      }

      async function driveCall(method, path, body) {
        const res = await authFetch(`${API}${path}`, {
          method,
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        let data = {};
        try { data = await res.json(); } catch {}
        return { ok: res.ok, data };
      }

      const DRIVE_ADD_KEY = "driveAdd";
      function addDrive(kind) {
        const name = document.getElementById("drive-add-name").value;
        const ids = (driveData ? driveData.connections : []).map((c) => c.connection_id);
        try {
          sessionStorage.setItem(DRIVE_ADD_KEY, JSON.stringify({ kind, name, ids, at: Date.now() }));
        } catch {}
        window.location.href = `/auth/connect/${encodeURIComponent(kind)}?intent=add`;
      }

      // On return from an add (?connected=<kind>): the connection whose id was not in the stored set is renamed to the
      // typed name; a refused name shows on its row (the connection exists with its default name meanwhile).
      async function finishAddedDrive(qs) {
        let pending = null;
        try { pending = JSON.parse(sessionStorage.getItem(DRIVE_ADD_KEY) || "null"); } catch {}
        if (!pending) return;
        if (!qs.get("connected") && !qs.get("connect_error")) return;
        try { sessionStorage.removeItem(DRIVE_ADD_KEY); } catch {}
        if (qs.get("connected") !== pending.kind || Date.now() - pending.at > 30 * 60 * 1000) return;
        try {
          const res = await authFetch(`${API}/api/drive/connections`, { credentials: "include" });
          if (!res.ok) return;
          const fresh = (await res.json()).connections.find((c) => !pending.ids.includes(c.connection_id));
          if (!fresh || !pending.name || pending.name === fresh.mount_name) return;
          const out = await driveCall("PUT", `/api/drive/connections/${encodeURIComponent(fresh.connection_id)}/name`, { name: pending.name });
          const ui = driveUiOf(fresh.connection_id);
          if (!out.ok) ui.nameError = out.data.error || t("toast.drive_failed");
          else if (out.data.warning) ui.nameWarning = out.data.warning;
          if (ui.nameError || ui.nameWarning) { ui.open = true; ui.tab = "settings"; }   // the message sits in the settings tab
        } catch {}
      }

      async function renameDrive(id) {
        const ui = driveUiOf(id);
        const name = document.getElementById(`drive-name-${id}`).value;
        const out = await driveCall("PUT", `/api/drive/connections/${encodeURIComponent(id)}/name`, { name });
        ui.nameError = out.ok ? "" : (out.data.error || t("toast.drive_failed"));
        ui.nameWarning = out.ok ? (out.data.warning || "") : "";
        if (out.ok) { _driveStatusSnapshot = null; await loadDriveStatus(); } else redrawDrive();
      }

      async function makeDrivePrimary(id) {
        const out = await driveCall("PUT", "/api/drive/primary", { connection_id: id });
        if (!out.ok) showToast(out.data.error || t("toast.drive_failed"), "error");
        _driveStatusSnapshot = null;
        await loadDriveStatus();
      }

      async function saveDriveSearchedByDefault(input, id) {
        const enabled = input.checked;
        const out = await driveCall("PUT", `/api/drive/connections/${encodeURIComponent(id)}/searched-by-default`, { enabled });
        if (!out.ok) { input.checked = !enabled; showToast(out.data.error || t("toast.drive_failed"), "error"); return; }
        showToast(t("toast.drive_saved"), "success");
        _driveStatusSnapshot = null;
        await loadDriveStatus();
      }

      // `inPicker`: the save was made from the picker dialog; a refusal then shows in the dialog (which stays open)
      // instead of on the card.
      async function saveDriveWriteFolder(id, value, inPicker) {
        const ui = driveUiOf(id);
        const folderId = value === "__none" ? null : value === "__root" ? "" : value;
        const out = await driveCall("PUT", `/api/drive/connections/${encodeURIComponent(id)}/write-folder`, { folder_id: folderId });
        const error = out.ok ? "" : (out.data.error || t("toast.drive_failed"));
        if (!out.ok && inPicker && ui.picker) {
          ui.picker.error = error;
          return false;
        }
        ui.folderError = error;
        if (out.ok) ui.picker = null;
        _driveStatusSnapshot = null;
        if (out.ok) await loadDriveStatus(); else redrawDrive();
        return out.ok;
      }

      function driveScopeOf(id) {
        return driveData.connections.find((c) => c.connection_id === id).scope;
      }

      // The server keeps no folders under "whole drive", so the page remembers them (until it is reloaded) and
      // unticking the box brings them back.
      async function toggleDriveScopeWhole(id, whole) {
        if (!whole && driveScopeOf(id).whole_drive && !confirm(t("drive.scope_narrow_warning"))) return;
        const ui = driveUiOf(id);
        const ids = driveScopeOf(id).folders.map((f) => f.id);
        if (whole) ui.foldersBeforeWhole = ids;
        const saved = await saveDriveScope(id, whole, whole ? [] : (ui.foldersBeforeWhole || ids));
        if (saved && !whole) ui.foldersBeforeWhole = null;
      }

      async function removeDriveScopeFolder(id, index) {
        await saveDriveScope(id, false, driveScopeOf(id).folders.filter((_, i) => i !== index).map((f) => f.id));
      }

      async function saveDriveScope(id, wholeDrive, folderIds, inPicker) {
        const ui = driveUiOf(id);
        const out = await driveCall("PUT", `/api/drive/connections/${encodeURIComponent(id)}/scope`,
          { whole_drive: wholeDrive, folder_ids: folderIds });
        const error = out.ok ? "" : (out.data.error || t("toast.drive_failed"));
        if (!out.ok && inPicker && ui.picker) {
          ui.picker.error = error;
          return false;
        }
        ui.scopeError = error;
        if (out.ok) { ui.picker = null; showToast(t("toast.drive_saved"), "success"); }
        _driveStatusSnapshot = null;
        if (out.ok) await loadDriveStatus(); else redrawDrive();
        return out.ok;
      }

      // Set the connector access: mode "nothing" | "whole" | "folders" (with `folderIds`). A refusal shows the server's
      // line in the picker when it is open, else under the block.
      async function saveDriveConnectorAccess(id, mode, folderIds, inPicker) {
        const ui = driveUiOf(id);
        const out = await driveCall("PUT", `/api/drive/connections/${encodeURIComponent(id)}/connector-access`,
          mode === "folders" ? { mode, folder_ids: folderIds } : { mode });
        const error = out.ok ? "" : (out.data.error || t("toast.drive_failed"));
        if (!out.ok && inPicker && ui.picker) {
          ui.picker.error = error;
          return false;
        }
        ui.connectorError = error;
        if (out.ok) { ui.picker = null; showToast(t("toast.drive_saved"), "success"); }
        _driveStatusSnapshot = null;
        if (out.ok) await loadDriveStatus(); else redrawDrive();
        return out.ok;
      }

      // ── The folder picker: a dialog outside the card (#drive-picker-root), one per page ──────────────────────────
      // State lives in driveUi[id].picker. The scope picker edits a draft (ticked folders + whole drive) and saves it
      // with its Save (n) button; closing with a pending draft asks first. The write picker works the same way.
      let drivePickerId = null;       // the connection whose dialog is open
      let drivePickerOpener = null;   // the element that opened it, focused again on close

      function drivePickerRows(p) {
        const list = !p.trail.length && p.source === "outer" ? p.outer : p.folders;
        const f = p.filter.trim().toLowerCase();
        return f ? list.filter((x) => String(x.name).toLowerCase().includes(f)) : list;
      }

      // How many things the dialog would change; the Save (n) number.
      function drivePickerChanges(p) {
        if (p.purpose === "write") return p.write === p.init.write ? 0 : 1;
        if (p.purpose === "connector") {
          const picked = p.sel.map((s) => s.id);
          return picked.filter((x) => !p.init.ids.includes(x)).length + p.init.ids.filter((x) => !picked.includes(x)).length;
        }
        const now = p.sel.map((s) => s.id);
        let n = now.filter((x) => !p.init.ids.includes(x)).length + p.init.ids.filter((x) => !now.includes(x)).length;
        if (p.whole !== p.init.whole) n += 1;
        return n;
      }

      // A path is inside the scope when it equals a scope folder's path or lies under it; a folder whose path is
      // unknown ("") is taken as allowing everything (the server decides and says so).
      function drivePathInScope(sv, path) {
        if (sv.whole || sv.area) return true;
        return sv.folders.some((s) => !s.path || path === s.path || path.startsWith(s.path + "/"));
      }

      async function openDrivePicker(id, purpose) {
        const row = driveRowOf(id);
        if (!row) return;
        const sv = driveScopeView(row);
        const wf = row.write_folder;
        const chosen = purpose === "connector" ? driveConnectorView(row).folders : sv.folders;
        const sel = chosen.map((f) => ({ id: f.id, path: f.path || "" }));
        const write = wf ? wf.id : null;
        drivePickerOpener = document.activeElement;
        drivePickerId = id;
        driveUiOf(id).picker = {
          purpose, source: "root", trail: [], folders: [], outer: [], top: null, loading: true, filter: "", error: "",
          confirm: false, busy: false, req: 0, sel, whole: sv.whole, area: sv.area, stash: null, write, writePath: wf ? wf.path || "" : "",
          init: { ids: sel.map((s) => s.id), whole: sv.whole, write },
        };
        document.body.classList.add("dv-modal-open");
        renderDrivePicker(id);
        await loadDrivePickerFolders(id);
      }

      async function loadDrivePickerFolders(id) {
        const p = driveUiOf(id).picker;
        if (!p) return;
        const parent = p.trail.length ? p.trail[p.trail.length - 1].id : "";
        if (!parent && p.top) {
          p.folders = p.top.folders;
          p.outer = p.top.outer;
          p.loading = false;
          renderDrivePicker(id);
          return;
        }
        p.loading = true;
        p.req += 1;
        const mine = p.req;
        renderDrivePicker(id);
        const base = `/api/drive/connections/${encodeURIComponent(id)}/folders`;
        const out = await driveCall("GET", parent ? `${base}?parent=${encodeURIComponent(parent)}` : base);
        if (driveUiOf(id).picker !== p || p.req !== mine) return;   // closed, or a newer listing is on its way
        p.folders = out.ok ? out.data.folders : [];
        p.outer = out.ok && !parent ? (out.data.outer || []) : [];
        if (out.ok && !parent) p.top = { folders: p.folders, outer: p.outer };
        p.error = out.ok ? "" : (out.data.error || t("toast.drive_failed"));
        p.loading = false;
        renderDrivePicker(id);
      }

      function setDrivePickerSource(id, source) {
        const p = driveUiOf(id).picker;
        p.source = source;
        p.trail = [];
        p.filter = "";
        p.confirm = false;
        p.preview = null;
        loadDrivePickerFolders(id);
      }

      function openDrivePickerFolder(id, index) {
        const p = driveUiOf(id).picker;
        const f = drivePickerRows(p)[index];
        if (!f) return;
        p.trail.push({ id: f.id, name: f.name });
        p.filter = "";
        p.confirm = false;
        p.preview = null;
        loadDrivePickerFolders(id);
      }

      function driveCrumb(id, index) {
        const p = driveUiOf(id).picker;
        if (index >= p.trail.length) return;
        p.trail = p.trail.slice(0, index);
        p.filter = "";
        p.confirm = false;
        p.preview = null;
        loadDrivePickerFolders(id);
      }

      function setDrivePickerFilter(id, value) {
        const p = driveUiOf(id).picker;
        p.filter = value;
        renderDrivePickerPart(id, "list");
      }

      function toggleDrivePickerFolder(id, index) {
        const p = driveUiOf(id).picker;
        const f = drivePickerRows(p)[index];
        if (!f) return;
        const at = p.sel.findIndex((s) => s.id === f.id);
        if (at >= 0) p.sel.splice(at, 1);
        else {
          const path = f.path || "";
          if (path) p.sel = p.sel.filter((s) => !(s.path && s.path.startsWith(path + "/")));   // the parent covers them
          p.sel.push({ id: f.id, path });
        }
        p.confirm = false;
        p.preview = null;
        p.error = "";
        renderDrivePicker(id);
      }

      function removeDrivePickerChip(id, index) {
        const p = driveUiOf(id).picker;
        p.sel.splice(index, 1);
        p.confirm = false;
        p.preview = null;
        p.error = "";
        renderDrivePicker(id);
      }

      function toggleDrivePickerWhole(id, on) {
        const p = driveUiOf(id).picker;
        p.whole = on;
        if (on) {
          if (p.sel.length) p.stash = p.sel;
          p.sel = [];
        } else if (p.stash && !p.sel.length) {
          p.sel = p.stash;
          p.stash = null;
        }
        p.confirm = false;
        p.preview = null;
        p.error = "";
        renderDrivePicker(id);
      }

      // `index` -1: nowhere (read-only), -2: the drive's top level / own folder; otherwise a row of the list.
      function setDrivePickerWrite(id, index) {
        const p = driveUiOf(id).picker;
        if (index === -1) { p.write = null; p.writePath = ""; }
        else if (index === -2) { p.write = ""; p.writePath = ""; }
        else {
          const f = drivePickerRows(p)[index];
          if (!f) return;
          p.write = f.id;
          p.writePath = f.path || "";
        }
        p.confirm = false;
        p.preview = null;
        p.error = "";
        renderDrivePicker(id);
      }

      // `added`: the owner pressed Add on the size confirmation, so the save goes ahead without counting again.
      async function saveDrivePicker(id, added) {
        const ui = driveUiOf(id);
        const p = ui.picker;
        if (!p || p.busy || drivePickerChanges(p) === 0) return;
        // A drive on its default area (an app folder) leaving it for chosen folders: the default folder's
        // files leave the index; ask first, and a decline saves nothing.
        if (!added && p.purpose === "scope" && p.area && !p.whole && p.sel.length > 0 && !confirm(t("drive.scope_narrow_area_warning"))) return;
        p.busy = true;
        p.error = "";
        if (p.purpose === "scope" && !added) {
          // New folders (or the whole drive) are counted first, so the owner sees the size before the indexer spends money.
          p.counting = true;
          renderDrivePickerPart(id, "foot");
          const out = await driveCall("POST", `/api/drive/connections/${encodeURIComponent(id)}/scope/preview`,
            { whole_drive: p.whole, folder_ids: p.sel.map((s) => s.id) });
          p.counting = false;
          if (ui.picker !== p) return;                            // closed while counting
          if (!(out.ok && out.data.new === false)) {
            p.busy = false;
            p.preview = out.ok && out.data && out.data.files ? out.data : { failed: true };   // a failed count still lets the owner add
            renderDrivePickerPart(id, "foot");
            const add = document.getElementById("drive-pk-add");
            if (add) add.focus();
            return;
          }
        }
        p.preview = null;
        renderDrivePickerPart(id, "foot");
        if (p.purpose === "write") {
          await saveDriveWriteFolder(id, p.write === null ? "__none" : p.write === "" ? "__root" : p.write, true);
        } else if (p.purpose === "connector") {
          const ids = p.sel.map((s) => s.id);
          await saveDriveConnectorAccess(id, ids.length ? "folders" : "nothing", ids, true);
        } else {
          await saveDriveScope(id, p.whole, p.sel.map((s) => s.id), true);
        }
        if (!ui.picker) { closeDrivePickerDialog(); return; }   // saved
        p.busy = false;                                          // refused: the dialog stays, with the server's line
        p.confirm = false;
        renderDrivePicker(id);
      }

      // Cancel on the size confirmation: nothing is saved, the picker stays open with its selection.
      function cancelDrivePickerPreview(id) {
        const p = driveUiOf(id).picker;
        if (!p) return;
        p.preview = null;
        renderDrivePickerPart(id, "foot");
      }

      function requestCloseDrivePicker(id) {
        const p = driveUiOf(id).picker;
        if (!p) return;
        if (p.preview) { cancelDrivePickerPreview(id); return; }
        if (drivePickerChanges(p) > 0 && !p.busy) {
          p.confirm = true;
          renderDrivePickerPart(id, "foot");
          const keep = document.getElementById("drive-pk-save-confirm");
          if (keep) keep.focus();
        } else closeDrivePicker(id);
      }

      function resumeDrivePicker(id) {
        driveUiOf(id).picker.confirm = false;
        renderDrivePickerPart(id, "foot");
      }

      function closeDrivePicker(id) {
        driveUiOf(id).picker = null;
        closeDrivePickerDialog();
      }

      function closeDrivePickerDialog() {
        drivePickerId = null;
        const root = document.getElementById("drive-picker-root");
        if (root) root.innerHTML = "";
        document.body.classList.remove("dv-modal-open");
        if (drivePickerOpener && document.body.contains(drivePickerOpener)) drivePickerOpener.focus();
        drivePickerOpener = null;
      }

      function renderDrivePicker(id) {
        const p = driveUiOf(id).picker;
        const row = driveRowOf(id);
        const root = document.getElementById("drive-picker-root");
        if (!p || !row || !root) return;
        if (!document.getElementById("drive-pk-dialog")) {
          const q = escapeHtml(id);
          const sv = driveScopeView(row);
          const subKey = p.purpose === "write" ? "drive.pk_sub_write" : p.purpose === "connector" ? "drive.connector_explain"
            : sv.means === "area" ? "drive.pk_sub_area" : "drive.pk_sub_account";
          root.innerHTML = `
            <div class="dv-overlay" onclick="if (event.target === this) requestCloseDrivePicker('${q}')">
              <div class="dv-sheet" id="drive-pk-dialog" role="dialog" aria-modal="true" aria-labelledby="drive-pk-title">
                <header class="dv-pk-head"><div><h2 id="drive-pk-title">${t(p.purpose === "write" ? "drive.pk_title_write" : p.purpose === "connector" ? "drive.connector_title" : "drive.pk_title_scope")}</h2>
                  <p class="dv-pk-sub">${escapeHtml(row.mount_name)} · ${t(subKey)}</p></div>
                  <button type="button" class="dv-icon-btn" id="drive-pk-close" onclick="requestCloseDrivePicker('${q}')" aria-label="${escapeHtml(t("drive.close"))}">${driveIcon("x")}</button></header>
                <aside class="dv-pk-side" id="drive-pk-side"></aside>
                <div class="dv-pk-main">
                  <nav class="dv-pk-crumbs" id="drive-pk-crumbs" aria-label="${escapeHtml(t("drive.pk_path"))}"></nav>
                  <div class="dv-pk-filter">${driveIcon("search", "sm")}<input type="text" id="drive-pk-filter" autocomplete="off"
                    placeholder="${escapeHtml(t("drive.pk_filter"))}" aria-label="${escapeHtml(t("drive.pk_filter"))}" oninput="setDrivePickerFilter('${q}', this.value)"></div>
                  <div class="dv-pk-list" id="drive-pk-list"></div>
                </div>
                <footer class="dv-pk-foot" id="drive-pk-foot"></footer>
              </div></div>`;
          const close = document.getElementById("drive-pk-close");
          if (close) close.focus();
        }
        for (const part of ["side", "crumbs", "list", "foot"]) renderDrivePickerPart(id, part);
      }

      function renderDrivePickerPart(id, part) {
        const p = driveUiOf(id).picker;
        const row = driveRowOf(id);
        const el = document.getElementById(`drive-pk-${part}`);
        if (!p || !row || !el) return;
        const q = escapeHtml(id);
        const sv = driveScopeView(row);
        if (part === "side") {
          const rootSrc = sv.means === "area"
            ? { label: t("drive.pk_root_area"), sub: t("drive.pk_root_area_sub"), icon: "home" }
            : { label: t("drive.pk_root"), sub: t("drive.pk_root_sub"), icon: "drive" };
          const sources = [{ key: "root", ...rootSrc }];
          if (p.outer.length || p.source === "outer") sources.push({ key: "outer", label: t("drive.picker_outer"), sub: t("drive.pk_outer_sub"), icon: "shared" });
          let h = sources.map((s) => `<button type="button" class="dv-pk-src" aria-pressed="${p.source === s.key}" onclick="setDrivePickerSource('${q}', '${s.key}')">
            ${driveIcon(s.icon, "sm")}<span>${s.label}<small>${s.sub}</small></span></button>`).join("");
          if (p.purpose !== "connector") h += '<div class="dv-pk-divider"></div>';
          if (p.purpose === "connector") {
            // no special choice here: "Everything the bot sees" is a button of the block
          } else if (p.purpose === "scope") {
            if (sv.means === "area") h += `<p class="dv-pk-info">${t("drive.pk_area_info")}</p>`;
            else h += `<label class="dv-pk-special"><input type="checkbox" ${p.whole ? "checked" : ""} onchange="toggleDrivePickerWhole('${q}', this.checked)">
              <span>${t("drive.whole_title")}<small>${t(sv.means === "nothing" ? "drive.whole_sub_account" : "drive.whole_sub_generic")}</small></span></label>`;
          } else {
            const rootOk = sv.means === "area" ? sv.empty : sv.whole;
            h += `<label class="dv-pk-special"><input type="radio" name="drive-pk-w" ${p.write === null ? "checked" : ""} onchange="setDrivePickerWrite('${q}', -1)">
              <span>${t("drive.pk_write_none")}<small>${t("drive.pk_write_none_sub")}</small></span></label>
              <label class="dv-pk-special${rootOk ? "" : " off"}"><input type="radio" name="drive-pk-w" ${p.write === "" ? "checked" : ""} ${rootOk ? "" : "disabled"} onchange="setDrivePickerWrite('${q}', -2)">
              <span>${t(sv.means === "area" ? "drive.own_folder" : "drive.write_top")}<small>${t(rootOk ? "drive.pk_write_root_sub" : "drive.pk_write_root_needs")}</small></span></label>`;
          }
          el.innerHTML = h;
        } else if (part === "crumbs") {
          const first = p.source === "outer" ? t("drive.picker_outer") : t(sv.means === "area" ? "drive.pk_root_area" : "drive.pk_root");
          const items = [first].concat(p.trail.map((s) => escapeHtml(s.name)));
          el.innerHTML = items.map((n, i) => (i ? driveIcon("chevR", "sm") : "")
            + `<button type="button" class="dv-crumb" onclick="driveCrumb('${q}', ${i})"${i === items.length - 1 ? ' aria-current="page"' : ""}>${n}</button>`).join("");
        } else if (part === "list") {
          if (p.loading) {
            el.innerHTML = `<div class="dv-pk-loading" role="status">${t("drive.pk_loading")}</div>${'<div class="dv-skel"></div>'.repeat(5)}`;
            return;
          }
          const rows = drivePickerRows(p);
          if (!rows.length) {
            el.innerHTML = `<div class="dv-pk-empty">${p.filter.trim() ? driveText("drive.pk_no_match", { q: p.filter }) : t("drive.no_folders")}</div>`;
            return;
          }
          el.innerHTML = rows.map((f, i) => (p.purpose === "write" ? drivePickerWriteRow(p, sv, f, i, q)
            : p.purpose === "connector" ? drivePickerConnectorRow(p, sv, f, i, q) : drivePickerScopeRow(p, f, i, q))).join("");
        } else if (part === "foot") {
          el.innerHTML = drivePickerFoot(p, sv, q);
        }
      }

      function drivePickerScopeRow(p, f, i, q) {
        const path = f.path || "";
        const picked = p.sel.some((s) => s.id === f.id);
        const through = !picked && path ? p.sel.find((s) => s.path && path.startsWith(s.path + "/")) : null;
        const inside = path ? p.sel.filter((s) => s.path && s.path.startsWith(path + "/")).length : 0;
        const covered = p.whole || !!through;
        let sub = "";
        if (p.whole) sub = t("drive.pk_included_whole");
        else if (through) sub = driveText("drive.pk_included_through", { name: driveLast(through.path) });
        else if (inside) sub = driveText("drive.pk_inside", { n: inside });
        return `<div class="dv-prow">
          <input type="checkbox" ${picked || covered ? "checked" : ""} ${covered ? "disabled" : ""} onchange="toggleDrivePickerFolder('${q}', ${i})"
            aria-label="${escapeHtml(driveText("drive.pk_show", { name: f.name }))}">
          <button type="button" class="dv-prow-btn" onclick="openDrivePickerFolder('${q}', ${i})">${driveIcon("folder", "fi")}
            <span class="dv-prow-text"><span class="dv-prow-name">${escapeHtml(f.name)}</span>${sub ? `<span class="dv-prow-sub">${sub}</span>` : ""}</span>
            ${driveIcon("chevR", "dv-chevr")}</button></div>`;
      }

      // A connector folder must lie inside the scope: a folder outside it is not selectable, only opened when it leads
      // to a scope folder (as the write picker); a folder under a picked one is covered by it.
      function drivePickerConnectorRow(p, sv, f, i, q) {
        const path = f.path || "";
        const allowed = drivePathInScope(sv, path);
        const leads = !allowed && path && sv.folders.some((s) => s.path && s.path.startsWith(path + "/"));
        const navigable = allowed || leads;
        const picked = p.sel.some((s) => s.id === f.id);
        const through = !picked && path ? p.sel.find((s) => s.path && path.startsWith(s.path + "/")) : null;
        let sub = "";
        if (!allowed) sub = t(leads ? "drive.pk_contains" : "drive.pk_not_visible");
        else if (through) sub = driveText("drive.pk_included_through", { name: driveLast(through.path) });
        return `<div class="dv-prow${!allowed && !leads ? " dim" : ""}">
          <input type="checkbox" ${picked || through ? "checked" : ""} ${allowed && !through ? "" : "disabled"} onchange="toggleDrivePickerFolder('${q}', ${i})"
            aria-label="${escapeHtml(driveText("drive.pk_show", { name: f.name }))}">
          <button type="button" class="dv-prow-btn" ${navigable ? `onclick="openDrivePickerFolder('${q}', ${i})"` : "disabled"}>${driveIcon("folder", "fi")}
            <span class="dv-prow-text"><span class="dv-prow-name">${escapeHtml(f.name)}</span>${sub ? `<span class="dv-prow-sub">${sub}</span>` : ""}</span>
            ${navigable ? driveIcon("chevR", "dv-chevr") : ""}</button></div>`;
      }

      function drivePickerWriteRow(p, sv, f, i, q) {
        const path = f.path || "";
        const allowed = drivePathInScope(sv, path);
        const leads = !allowed && path && sv.folders.some((s) => s.path && s.path.startsWith(path + "/"));
        const navigable = allowed || leads;
        let sub = "";
        if (!allowed) sub = t(leads ? "drive.pk_contains" : "drive.pk_not_visible");
        const inside = p.write && p.writePath && p.write !== f.id && path && p.writePath.startsWith(path + "/");
        return `<div class="dv-prow${!allowed && !leads ? " dim" : ""}">
          <input type="radio" name="drive-pk-w" ${p.write === f.id ? "checked" : ""} ${allowed ? "" : "disabled"} onchange="setDrivePickerWrite('${q}', ${i})"
            aria-label="${escapeHtml(driveText("drive.pk_save_to", { name: f.name }))}">
          <button type="button" class="dv-prow-btn" ${navigable ? `onclick="openDrivePickerFolder('${q}', ${i})"` : "disabled"}>${driveIcon("folder", "fi")}
            <span class="dv-prow-text"><span class="dv-prow-name">${escapeHtml(f.name)}</span>${sub ? `<span class="dv-prow-sub">${sub}</span>` : ""}</span>
            ${inside ? `<span class="dv-pill save">${t("drive.pk_saves_inside")}</span>` : ""}${navigable ? driveIcon("chevR", "dv-chevr") : ""}</button></div>`;
      }

      // What the owner's addition would bring into the index (POST .../scope/preview), shown before the save.
      function drivePickerPreviewText(pv) {
        if (pv.failed) return `<span>${t("drive.pk_add_failed")}</span>`;
        const f = driveText(pv.folders === 1 ? "drive.pk_add_folders_one" : "drive.pk_add_folders_many", { n: pv.folders });
        const head = pv.capped ? driveText("drive.pk_add_total_capped", { n: pv.total, f })
          : driveText(pv.total === 1 ? "drive.pk_add_total_one" : "drive.pk_add_total_many", { n: pv.total, f });
        const kinds = [["documents", "drive.pk_add_documents"], ["images", "drive.pk_add_images"],
          ["recordings", "drive.pk_add_recordings"], ["other", "drive.pk_add_other"]]
          .filter(([k]) => (pv.files[k] || 0) > 0).map(([k, key]) => driveText(key, { n: pv.files[k] }));
        return `<span><strong>${head}</strong>${kinds.length ? "<br>" + kinds.join(" · ") : ""}${pv.large ? `<br>${t("drive.pk_add_warn")}` : ""}</span>`;
      }

      function drivePickerFoot(p, sv, q) {
        const n = drivePickerChanges(p);
        if (p.preview && n > 0) {
          return `<div class="dv-pk-confirm" role="alert"><span><strong>${t("drive.pk_add_title")}</strong><br>${drivePickerPreviewText(p.preview)}</span>
            <div class="dv-btns"><button type="button" class="btn btn-secondary dv-btn-sm" onclick="cancelDrivePickerPreview('${q}')">${t("drive.cancel")}</button>
            <button type="button" class="btn btn-primary dv-btn-sm" id="drive-pk-add" ${p.busy ? "disabled" : ""} onclick="saveDrivePicker('${q}', true)">${t("drive.pk_add_btn")}</button></div></div>`;
        }
        if (p.confirm && n > 0) {
          return `<div class="dv-pk-confirm" role="alert"><span>${driveText(n === 1 ? "drive.pk_confirm_one" : "drive.pk_confirm_many", { n })}</span>
            <div class="dv-btns"><button type="button" class="dv-link" onclick="resumeDrivePicker('${q}')">${t("drive.pk_keep")}</button>
            <button type="button" class="btn btn-secondary dv-btn-sm" onclick="closeDrivePicker('${q}')">${t("drive.pk_discard")}</button>
            <button type="button" class="btn btn-primary dv-btn-sm" id="drive-pk-save-confirm" ${p.busy ? "disabled" : ""} onclick="saveDrivePicker('${q}')">${t("drive.save")}</button></div></div>`;
        }
        let tray = "";
        if (p.purpose === "connector") {
          const chips = p.sel.map((s, i) => `<span class="dv-tchip">${escapeHtml(driveLast(s.path) || s.id)}<button type="button" onclick="removeDrivePickerChip('${q}', ${i})"
            aria-label="${escapeHtml(driveText("drive.pk_remove", { name: s.path || s.id }))}">${driveIcon("x", "sm")}</button></span>`);
          tray = chips.length ? chips.join("") : t("drive.connector_nothing");
        } else if (p.purpose === "scope") {
          const chips = [];
          if (p.whole) chips.push(`<span class="dv-tchip">${t("drive.whole_title")}<button type="button" onclick="toggleDrivePickerWhole('${q}', false)" aria-label="${escapeHtml(t("drive.stop_whole"))}">${driveIcon("x", "sm")}</button></span>`);
          p.sel.forEach((s, i) => chips.push(`<span class="dv-tchip">${escapeHtml(driveLast(s.path) || s.id)}<button type="button" onclick="removeDrivePickerChip('${q}', ${i})"
            aria-label="${escapeHtml(driveText("drive.pk_remove", { name: s.path || s.id }))}">${driveIcon("x", "sm")}</button></span>`));
          tray = chips.length ? chips.join("")
            : sv.means === "area" ? t("drive.pk_tray_empty_area") : `<span class="dv-warn-text">${t("drive.pk_tray_empty")}</span>`;
        } else {
          const value = p.write === null ? t("drive.write_none_title")
            : p.write === "" ? t(sv.means === "area" ? "drive.own_folder" : "drive.write_top") : escapeHtml(p.writePath || p.write);
          tray = t("drive.pk_saves_to").replace("{v}", `<strong>${value}</strong>`);
        }
        const narrow = p.purpose === "scope" && !p.whole && n > 0 && sv.whole;
        return `<div class="dv-tray" aria-label="${escapeHtml(t("drive.pk_selected"))}">${tray}</div>
          ${narrow ? `<div class="dv-msg dv-warn-text" role="alert">${t("drive.scope_narrow_warning")}</div>` : ""}
          ${p.error ? `<div class="dv-msg err" role="alert">${escapeHtml(p.error)}</div>` : ""}
          <div class="dv-foot-row"><div class="dv-pk-actions"><button type="button" class="btn btn-secondary dv-btn-sm" onclick="closeDrivePicker('${q}')">${t("drive.cancel")}</button>
          <button type="button" class="btn btn-primary dv-btn-sm" ${n && !p.busy ? "" : "disabled"} onclick="saveDrivePicker('${q}')">${p.counting ? t("drive.pk_add_counting") : t("drive.save") + (n ? " (" + n + ")" : "")}</button></div></div>`;
      }

      // Esc and a click outside ask before dropping a draft; Tab stays inside the dialog.
      document.addEventListener("keydown", (e) => {
        if (!drivePickerId) return;
        if (e.key === "Escape") {
          e.preventDefault();
          requestCloseDrivePicker(drivePickerId);
        } else if (e.key === "Tab") {
          const dialog = document.getElementById("drive-pk-dialog");
          if (!dialog) return;
          const items = Array.from(dialog.querySelectorAll("button:not([disabled]), input:not([disabled])"));
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      });

      function askDisconnectDrive(id) {
        driveUiOf(id).confirm = true;
        redrawDrive();
      }

      function cancelDisconnectDrive(id) {
        driveUiOf(id).confirm = false;
        redrawDrive();
      }

      // DriveSyncState.last_error categories (USER_DRIVE_INDEXING_RFC §3.5); shown localised, never raw.
      const DRIVE_ERROR_CATEGORIES = ["reconnect_needed", "throttled", "provider_error", "internal"];

      // DriveSyncState.counters keys (records per state, §6), in display order. Only a key listed here is
      // ever shown (the enrichment stats and `chunks` of the stored counters are not).
      const DRIVE_COUNTER_LABELS = {
        carded: "drive.index_searchable",
        indexed: "drive.index_indexed",
        metadata_only: "drive.index_metadata_only",
        pending: "drive.index_pending",
        unresolved: "drive.index_unresolved",
        not_read_yet: "drive.index_unread",   // waiting only; the unreadable files are listed by name below
        inaccessible: "drive.index_inaccessible",   // files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2)
        failed: "drive.index_failed",
        deleting: "drive.index_deleting",
        profile_current: "drive.index_described",   // files with a current description
        passages_current: "drive.index_readable_content",   // files readable by their content
      };

      let driveMirrorRoot = false;   // the user has a _Transcripts folder: the opt-out confirmation names it
      // The "Search index" tab. The row's model (reconnect, empty scope, progress) is derived from the list the page holds.
      function renderDriveIndexing(ix, linked) {
        const enabled = !!ix.enabled;
        driveMirrorRoot = !!ix.mirror_root;
        const model = driveModel(driveRowOf(ix.connection_id) || { indexing: ix });
        const q = escapeHtml(ix.connection_id);
        const c = ix.counters || {};
        const num = (v) => Number(v) || 0;
        const usd = (v) => `$${num(v).toFixed(2)}`;
        let details = "";
        if (enabled) {
          const last = ix.last_success_at ? new Date(ix.last_success_at).toLocaleString() : t("drive.index_never");
          // the status row: paused, nothing to index, building (with its bar), or up to date
          let status = "";
          if (model.needsReconnect) {
            status = `<div class="dv-status paused">${driveIcon("pause")}${t("drive.status_paused")}</div>`;
          } else if (model.emptyIndex) {
            status = `<div class="dv-status warn">${driveIcon("alert")}${t("drive.search_empty")}</div>
              <div class="dv-row-sub">${t("drive.status_empty_sub")}</div>
              <div class="dv-alert-actions"><button type="button" class="btn btn-primary dv-btn-sm" onclick="openDrivePicker('${q}', 'scope')">${t("drive.choose_folders")}</button></div>`;
          } else if (ix.awaiting_rebuild) {
            status = `<div class="dv-status">${t("drive.index_awaiting_rebuild")}</div>`;
          } else if (model.scopeLine) {
            status = `<div class="dv-status">${model.scopeLine}</div>`;
          } else if (ix.building) {
            status = `<div class="dv-status">${t("drive.index_building")}${model.pct !== null && model.pct !== undefined ? " " + model.pct + "%" : ""}</div>
              ${model.pct !== null && model.pct !== undefined ? `<div class="dv-bar"><span style="width:${Math.max(model.pct, 2)}%"></span></div>` : ""}`;
          } else {
            status = `<div class="dv-status ok">${driveIcon("check")}${t("drive.status_ok")}</div>`;
          }
          const checking = num(c.unresolved);
          if (checking > 0 && !model.needsReconnect && !model.scopeLine) status += `<div class="dv-row-sub">${driveText("drive.index_checking", { n: driveFmt(checking) })}</div>`;
          if (!model.needsReconnect || ix.last_success_at) status += `<div class="dv-row-sub">${t("drive.index_last_sync")}: ${escapeHtml(last)}</div>`;

          let tiles = "";
          let more = "";
          if (!model.emptyIndex) {
            // Three tiles. Waiting is `pending` only; `unresolved` has its own line above, `not_read_yet` is under
            // "All counters".
            const cant = num(c.inaccessible) + num(c.failed);
            tiles = `<div class="dv-stats">
              <div class="dv-stat"><div class="dv-stat-v">${driveFmt(c.carded)}</div><div class="dv-stat-l">${t("drive.index_searchable")}</div></div>
              <div class="dv-stat"><div class="dv-stat-v">${driveFmt(c.pending)}</div><div class="dv-stat-l">${t("drive.index_pending")}</div></div>
              <div class="dv-stat${cant ? " warn" : ""}"><div class="dv-stat-v">${driveFmt(cant)}</div><div class="dv-stat-l">${t("drive.index_cant_read")}</div></div></div>`;
            // Only keys with a label are shown (no raw-key fallback): the enrichment stats and `chunks` stay out.
            const lines = [["carded", c.carded, false], ["chunks_readable", ix.readable_chunks, false]];
            for (const key of ["indexed", "metadata_only", "pending", "unresolved", "not_read_yet", "inaccessible", "failed", "deleting"]) {
              lines.push([key, c[key], false]);
            }
            lines.push(["profile_current", c.profile_current, false]);
            lines.push(["passages_current", c.passages_current, false]);
            const labels = Object.assign({ chunks_readable: "drive.index_chunks" }, DRIVE_COUNTER_LABELS);
            more = `<details class="dv-more"><summary>${driveIcon("chevR", "sm")}${t("drive.all_counters")}</summary>
              <div class="dv-more-body"><dl class="dv-kv">${lines.map(([key, v, attn]) =>
                `<dt${attn ? ' class="attn"' : ""}>${t(labels[key])}</dt><dd class="${attn ? "attn" : num(v) ? "" : "zero"}">${driveFmt(v)}</dd>`).join("")}</dl></div></details>`;
          }
          const unreadable = ix.unreadable || {};
          const unreadableFiles = Array.isArray(unreadable.files) ? unreadable.files : [];
          const unreadableMore = num(unreadable.total) - unreadableFiles.length;
          if (num(unreadable.total) > 0) {
            more += `<details class="dv-more"><summary>${driveIcon("chevR", "sm")}${t("drive.index_unreadable_title")}<span class="cnt">${num(unreadable.total)}</span></summary>
              <div class="dv-more-body"><ul class="dv-unread">
                ${unreadableFiles.map((f) => {
                  const known = ["no_text", "machine_text", "too_large", "user_chunk_cap", "failed"].includes(f.reason);
                  const why = t("drive.unreadable_reason." + (known ? f.reason : "failed"));
                  const link = typeof f.web_url === "string" && f.web_url.startsWith("https://");
                  const name = link
                    ? `<a href="${escapeHtml(f.web_url)}" target="_blank" rel="noopener">${escapeHtml(f.name)}</a>`
                    : escapeHtml(f.name);
                  return `<li>${name}<span>${why}</span></li>`;
                }).join("")}
                ${unreadableMore > 0 ? `<li><span>${t("drive.index_unreadable_more").replace("{n}", unreadableMore)}</span></li>` : ""}
              </ul></div></details>`;
          }
          // §25.4.5: this month's model dollars per class against its cap, and the initial build's progress
          const spent = ix.model_usd_month || {};
          const caps = ix.model_usd_caps || {};
          const meter = (label, value, cap, done) => {
            const pct = cap > 0 ? Math.min(100, Math.round((100 * value) / cap)) : 0;
            const text = done ? driveText("drive.finished_spent", { spent: usd(value) }) : driveText("drive.spent_of", { spent: usd(value), cap: usd(cap) });
            return `<div class="dv-meter"><span>${label}</span><span class="v">${text}</span><div class="dv-bar${done ? " ok" : pct >= 80 ? " warn" : ""}"><span style="width:${done ? 100 : pct}%"></span></div></div>`;
          };
          const classes = ["reader", "triage", "profile", "passage", "transcribe"].filter((k) => num(spent[k]) > 0 || num(caps[k]) > 0);
          const build = ix.initial_build || {};
          const hasBuild = num(build.spent_usd) > 0 || num(build.allowance_usd) > 0;
          const meters = (classes.length ? `<div class="dv-meters"><div class="dv-meters-title">${t("drive.spend_title")}</div>
              ${classes.map((k) => meter(t("drive.index_usd_" + k), num(spent[k]), num(caps[k]), false)).join("")}</div>` : "")
            + (hasBuild ? `<div class="dv-meters">${meter(t("drive.initial_build_label"), num(build.spent_usd), num(build.allowance_usd), !!build.closed)}</div>` : "");
          details = `<div class="dv-group"><div class="dv-row"><div class="dv-row-main">${status}</div></div></div>
            ${tiles}
            ${more ? `<div class="dv-group">${more}</div>` : ""}
            ${meters ? `<div class="dv-group">${meters}</div>` : ""}`;
        }
        return `
          <div class="dv-group">
            <div class="dv-row"><div class="dv-row-main"><label class="dv-row-title" for="drive-index-toggle-${q}">${t("drive.index_title")}</label>
              <div class="dv-row-sub">${t("drive.index_sub")}</div></div>
              <input type="checkbox" class="dv-switch" id="drive-index-toggle-${q}" ${enabled ? "checked" : ""}
                onchange="saveDriveIndexing(this, '${q}')"></div>
            <div class="dv-row-disc"><details class="disclosure"><summary>${t("drive.disclosure_summary")}</summary><p>${t("drive.index_disclosure")}${ix.dr_copy ? " " + t("drive.index_disclosure_dr_copy") : ""}${ix.transcribe_enabled ? " " + t("drive.transcribe_disclosure") : ""}${ix.mirror_root ? " " + t("drive.mirror_clause") : ""}</p></details></div>
            ${enabled ? "" : `<div class="dv-row"><span class="dv-row-value">${t("drive.index_search_off")}</span></div>`}
          </div>
          ${details}`;
      }

      function renderDeepResearchCopy(enabled) {
        return `
            <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
              <label style="font-size:14px; font-weight:500; cursor:pointer;" for="drive-dr-copy-toggle">${t("drive.dr_copy_title")}</label>
              <input type="checkbox" id="drive-dr-copy-toggle" ${enabled ? "checked" : ""}
                onchange="saveDeepResearchCopy(this)" style="width:18px;height:18px;cursor:pointer;">
            </div>
            <details class="disclosure"><summary>${t("drive.disclosure_summary")}</summary><p>${t("drive.dr_copy_disclosure")}</p></details>`;
      }

      async function saveDeepResearchCopy(input) {
        const enabled = input.checked;
        try {
          const res = await authFetch(`${API}/api/drive/deep-research-copy`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          });
          if (!res.ok) throw new Error();
          showToast(t(enabled ? "toast.drive_dr_copy_on" : "toast.drive_dr_copy_off"), "success");
          loadDriveStatus();
        } catch {
          input.checked = !enabled;
          showToast(t("toast.drive_dr_copy_failed"), "error");
        }
      }

      async function saveDriveIndexing(input, id) {
        const enabled = input.checked;
        // Turning off deletes the index (§6): an explicit confirmation first.
        if (!enabled && !confirm(t("drive.index_off_confirm") + (driveMirrorRoot ? " " + t("drive.mirror_clause") : ""))) {
          input.checked = true;
          return;
        }
        try {
          const res = await authFetch(`${API}/api/drive/connections/${encodeURIComponent(id)}/indexing`, {
            method: "PUT",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          });
          if (!res.ok) throw new Error();
          showToast(t(enabled ? "toast.drive_index_on" : "toast.drive_index_off"), "success");
          loadDriveStatus();
        } catch {
          input.checked = !enabled;
          showToast(t("toast.drive_index_failed"), "error");
        }
      }

      async function disconnectDrive(id) {
        const heirInput = document.getElementById(`drive-heir-${id}`);
        const body = heirInput ? { heir_id: heirInput.value } : undefined;
        try {
          const out = await driveCall("DELETE", `/api/drive/connections/${encodeURIComponent(id)}`, body);
          if (!out.ok) throw new Error(out.data.error || "");
          driveUiOf(id).confirm = false;
          showToast(t("toast.drive_disconnected"), "success");
        } catch (e) {
          showToast(e.message || t("toast.drive_failed"), "error");
        }
        _driveStatusSnapshot = null;
        loadDriveStatus();
      }

      function renderServiceStatus(id, name, description, data) {
        // Specifically targets tasks-container
        const container = document.getElementById("tasks-container");

        const linked = data.connected;
        // R1: the saved connection is gone while a primary list shows a past connection (SECRETS_AT_REST_RFC §6.2)
        const missing = !linked && !!data.reconnect_needed;

        let statusText = missing ? t("todo.needs_reconnect") : linked ? t("platform.connected") : t("platform.not_connected");
        const statusDotClass = missing ? "needs-reconnect" : linked ? "connected" : "disconnected";
        const reconnectHtml = missing
          ? `<div style="margin-bottom:16px; padding:12px 16px; border-radius:12px; border:1px solid var(--accent-danger); color:var(--accent-danger); font-size:14px;">${t("todo.reconnect_hint_missing")}</div>`
          : "";

        const actionButtons = linked
          ? `<button onclick="disconnectService('${id}')" class="btn btn-secondary">${t("platform.disconnect_account")}</button>`
          : missing
          ? connectButton(data.connect_href, t("todo.reconnect"))
          : connectButton(data.connect_href, t("platform.connect_todo"));

        container.innerHTML = `
          <div class="card integration-card tasks-card reveal">
            <div class="integration-icon">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#0078D4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"></polyline><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path></svg>
            </div>
            <h3 class="integration-title">${name}</h3>
            <p class="integration-description">${description}</p>
            
            <div class="integration-status">
              <div class="status-dot ${statusDotClass}"></div>
              <span>${statusText}</span>
            </div>

            ${reconnectHtml}

            <div class="integration-actions">
              ${actionButtons}
            </div>
          </div>
        `;
        observeElements();
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

      /* ── Team invites ────────────────────────────────── */
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
        <div class="invite-code-box reveal">
          <p style="margin-bottom:8px; font-weight:600; color:var(--text-main);">Share this code</p>
          <div class="code-display">${escapeHtml(data.code)}</div>
          <p style="font-size: 14px; margin-bottom: 8px; color:var(--text-muted);">Copy it now, it will not be shown again.</p>
          <p style="font-size: 14px; margin-bottom: 24px;">Valid until ${expiry}</p>
          
          <div class="invite-link-wrapper">
            <input type="text" id="deep-link-input" value="${escapeHtml(deepLink)}" readonly style="flex:1; background:transparent; border:none; padding:12px 20px; font-size:16px; outline:none; color:var(--text-main);" />
            <button onclick="copyDeepLink()" class="btn btn-black" style="padding: 10px 24px;">Copy Link</button>
          </div>
        </div>`;
          setTimeout(
            () => el.querySelector(".reveal").classList.add("visible"),
            50,
          );
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
            list.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 64px 24px; font-size:18px;">No codes generated yet.</div>`;
            return;
          }
          list.innerHTML = `
        <table class="codes-table">
          <thead><tr><th>Code</th><th>Status</th><th>Created</th><th>Used</th></tr></thead>
          <tbody>${data.invites
            .map(
              (c) => `
            <tr>
              <td><span style="font-family: ui-monospace, SFMono-Regular, monospace; font-weight: 600;">${escapeHtml(c.hint ? "\u2026" + c.hint : "\u2014")}</span></td>
              <td><span class="status-badge ${c.is_valid ? "active" : "used"}">${c.is_valid ? "Active" : "Used"}</span></td>
              <td>${new Date(c.created_at).toLocaleDateString()}</td>
              <td>${c.used_at ? new Date(c.used_at).toLocaleDateString() : "—"}</td>
            </tr>`,
            )
            .join("")}
          </tbody>
        </table>`;
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

      /* ── Modals & Toasts ──────────────────────────────── */
      let _modalFactIdx = null;

      function renderModal(id, title, body, footer) {
        const container = document.getElementById("modal-container");
        container.innerHTML = `
        <div class="modal-overlay" id="${id}-overlay" onclick="closeModal()">
          <div class="modal-dialog" id="${id}-dialog" onclick="event.stopPropagation()">
            <div class="modal-header"><h3 class="modal-title">${title}</h3></div>
            <div class="modal-body">${body}</div>
            <div class="modal-footer">${footer}</div>
          </div>
        </div>`;

        // Trigger animation
        setTimeout(() => {
          document.querySelector(".modal-overlay").classList.add("visible");
        }, 10);

        document.addEventListener("keydown", handleEsc, true);
      }

      function closeModal() {
        const overlay = document.querySelector(".modal-overlay");
        if (overlay) {
          overlay.classList.remove("visible");
          setTimeout(() => {
            document.getElementById("modal-container").innerHTML = "";
          }, 300); // Wait for transition
        }
        document.removeEventListener("keydown", handleEsc, true);
      }

      function handleEsc(e) {
        if (e.key === "Escape") closeModal();
      }

      function openInvalidModal(factIdx) {
        _modalFactIdx = factIdx;
        const fact = allFacts[factIdx];
        const body = `<p style="margin-bottom:16px; font-size:18px;">Are you sure you want to delete this fact? This cannot be undone.</p>
                      <div style="padding: 24px; background-color: var(--bg-active); border-radius: 16px; color: var(--text-muted); font-size: 16px;">${escapeHtml(fact.text)}</div>`;
        const footer = `<button onclick="closeModal()" class="btn btn-secondary">Cancel</button>
                        <button id="modal-invalid-btn" onclick="invalidateFactDirect()" class="btn btn-danger">Delete Fact</button>`;
        renderModal("invalid-fact", "Delete Fact", body, footer);
      }

      async function invalidateFactDirect() {
        const fact = allFacts[_modalFactIdx];
        const btn = document.getElementById("modal-invalid-btn");
        btn.textContent = "Deleting...";
        btn.disabled = true;
        try {
          const res = await factsFetch(
            `${API}/api/user/facts/${fact.id}/invalidate`,
            { method: "POST", credentials: "include" },
          );
          if (res.status === 409) {
            closeModal();
            showToast("This fact changed in the meantime; the list was reloaded.", "error");
            await browseInit();
            return;
          }
          if (!res.ok) throw new Error();
          closeModal();
          allFacts.splice(_modalFactIdx, 1);
          renderFacts(allFacts, true);
          showToast("Fact deleted", "success");
        } catch {
          btn.textContent = "Delete";
          btn.disabled = false;
          showToast("Error deleting", "error");
        }
      }

      function openCorrectModal(factIdx) {
        _modalFactIdx = factIdx;
        const fact = allFacts[factIdx];
        const body = `<div style="margin-bottom:24px;">
                        <label>Current version</label>
                        <div style="padding: 20px; background-color: var(--bg-active); border-radius: 16px; color: var(--text-muted); font-size: 16px;">${escapeHtml(fact.text)}</div>
                      </div>
                      <div style="margin-bottom:0;">
                        <label for="modal-correct-new">Correct version</label>
                        <textarea id="modal-correct-new" rows="4" class="inp" placeholder="Enter the correct version..."></textarea>
                      </div>`;
        const footer = `<button onclick="closeModal()" class="btn btn-secondary">Cancel</button>
                        <button onclick="copyCorrectMessage()" class="btn btn-primary">Copy correction</button>`;
        renderModal("correct-fact", "Correct Fact", body, footer);
        setTimeout(
          () => document.getElementById("modal-correct-new").focus(),
          100,
        );
      }

      function copyCorrectMessage() {
        const oldText = allFacts[_modalFactIdx].text;
        const newText = document
          .getElementById("modal-correct-new")
          .value.trim();
        if (!newText) {
          showToast("Enter a correction", "error");
          return;
        }
        const msg = `I found this fact in my memory database and it needs correction.\nCurrent: "${oldText}"\nCorrect: "${newText}"\nPlease update it.`;
        navigator.clipboard.writeText(msg).then(() => {
          closeModal();
          showToast("Message copied! Paste to chat.", "success");
        });
      }

      function showToast(msg, type = "success") {
        const container = document.getElementById("toast-container");
        const toast = document.createElement("div");
        toast.className = `toast ${type}`;

        const icon =
          type === "success"
            ? `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`
            : `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;

        toast.innerHTML = `${icon} <span></span>`;
        toast.querySelector("span").textContent = msg;
        container.appendChild(toast);

        // Animate in
        setTimeout(() => toast.classList.add("visible"), 10);

        // Animate out
        setTimeout(() => {
          toast.classList.remove("visible");
          setTimeout(() => toast.remove(), 400);
        }, 4000);
      }


      // Numeric entities built from the char code: no entity literal in the source that an editor could decode
      // back into the raw character (which is how this function once became a no-op).
      function escapeHtml(str) {
        return String(str ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");
      }

      async function logout() {
        await fetch(`${API}/auth/logout`, {
          method: "POST",
          credentials: "include",
        });
        window.location.href = "/";
      }
