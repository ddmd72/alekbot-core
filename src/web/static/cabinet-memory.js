/* The Cabinet's Memory section (CABINET_REDESIGN_RFC §5.2): the PIN gate, the review questions collapsed into one row,
   search, the state segmented control, the life-area select with counts (GET /api/user/facts/domain-counts), the
   facts as rows that open to Edit and Delete, and the two fact modals. Loaded after cabinet-sections.js; the plain
   scripts share one global scope (the PIN state and helpers it uses live in cabinet-sections.js).
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
        document.getElementById("memory-inbox-body").innerHTML = "";
        document.getElementById("memory-inbox").classList.add("hidden");
        inboxSettings = null;
        inboxApps = [];
        document.getElementById("inbox-apps").innerHTML = "";
        document.getElementById("memory-inbox-settings").classList.add("hidden");
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
        areaCounts = null;
        renderStateChips();
        renderAreaSelect();
        loadReviewQuestions();
        loadMemoryInbox();
        loadInboxSettings();
        loadAreaCounts();
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

      // The state switch: a segmented control (current / stale / closed).
      function renderStateChips() {
        const seg = document.getElementById("state-chips");
        seg.setAttribute("aria-label", t("memory.state_label"));
        seg.innerHTML = Object.keys(FACT_STATE_KEYS)
          .map(
            (st) =>
              `<button type="button" data-state="${st}" onclick="setFactsState(this.dataset.state)" class="${st === factsState ? "on" : ""}" aria-pressed="${st === factsState}">${escapeHtml(t(FACT_STATE_KEYS[st]))}</button>`,
          )
          .join("");
      }

      function setFactsState(st) {
        if (!FACT_STATE_KEYS[st] || st === factsState) return;
        factsState = st;
        browseCursor = null;
        browseDomain = "all";
        allFacts = [];
        areaCounts = null;
        if (factsMode === "search") {
          // a state chosen over search results lists that state again, as before the search
          factsMode = "browse";
          document.getElementById("back-to-browse-btn").classList.add("hidden");
        }
        renderStateChips();
        renderAreaSelect();   // hidden for "closed", as the chips were
        if (st === "closed") closedLoad();
        else {
          loadAreaCounts();
          browseLoad(true);
        }
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
          renderAreaSelect();
          if (facts.length === 0) {
            container.innerHTML = `<div class="c-mem-empty"><p class="c-hint">${escapeHtml(t("memory.closed_empty").replace("{days}", data.window_days))}</p></div>`;
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
              const badge = factBadge(f);
              const domain = badge
                ? `<span class="fact-domain-badge">${escapeHtml(badge)}</span>`
                : "";
              // read-only: a closed fact has no actions
              return `
        <div class="c-mem-item c-mem-static">
          <span class="c-mem-text">${escapeHtml(f.text)}</span>
          <span class="c-mem-meta">${domain}<span class="c-mem-why">${escapeHtml(why)}</span><span>${escapeHtml(when)}</span></span>
        </div>`;
            })
            .join("");
          countEl.textContent = t("memory.shown").replace("{n}", facts.length);
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
            renderReviewRow();
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
          renderReviewRow();
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
        renderReviewRow();
      }

      function renderReviewQuestions(questions) {
        const box = document.getElementById("review-questions");
        if (questions.length === 0) {
          box.classList.add("hidden");
          box.innerHTML = "";
          delete box.dataset.rendered;
          renderReviewRow();
          return;
        }
        const html =
          reviewSection("questions", t("questions.title"), t("questions.subtitle")) +
          reviewSection("invoices", t("questions.invoices_title"), "");
        box.classList.remove("hidden");
        renderReviewRow();
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
            else {
              loadAreaCounts();
              await browseLoad(true, true);
            }
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
          renderAreaSelect();
        } catch (e) {
          showToast("Error loading facts: " + e.message, "error");
        }
      }

      async function browseNextPage() {
        if (!browseCursor) return;
        await browseLoad(false);
      }

      // One life area (a registry key) or "all": chosen in the area list or by its Reset link.
      function setDomainFilter(domain) {
        browseDomain = domain;
        browseCursor = null;
        allFacts = [];
        closeAreaList();
        renderAreaSelect();
        browseLoad(true);
      }

      function backToBrowse() {
        document.getElementById("facts-search").value = "";
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
          renderAreaSelect();   // a search spans every area: the select steps aside
          document
            .getElementById("back-to-browse-btn")
            .classList.remove("hidden");

          renderFacts(allFacts, true);
        } catch (e) {
          showToast("Search error: " + e.message, "error");
        }
      }

      // The facts as rows (text, area tag, date); a row opens to the two actions of a fact: Edit and Delete.
      function renderFacts(facts, isFirstLoad, instant = false) {
        const container = document.getElementById("facts-container");
        const countEl = document.getElementById("facts-count");
        const pageEl = document.getElementById("facts-pagination");

        if (isFirstLoad && facts.length === 0) {
          container.innerHTML = `<div class="c-mem-empty"><p class="c-hint">${escapeHtml(t("memory.empty"))}</p></div>`;
          countEl.textContent = "";
          pageEl.innerHTML = "";
          return;
        }

        const factsHtml = facts
          .map((f) => {
            const date = shortDate(f.created_at);
            const badge = factBadge(f);
            const domain = badge
              ? `<span class="fact-domain-badge">${escapeHtml(badge)}</span>`
              : "";
            const factIndex = allFacts.findIndex((fact) => fact.id === f.id);

            return `
        <div class="c-mem-item reveal${instant ? " visible" : ""}" data-idx="${factIndex}">
          <button type="button" class="c-mem-fact" data-idx="${factIndex}" aria-expanded="false" onclick="toggleFactRow(this.dataset.idx)">
            <span class="c-mem-text">${escapeHtml(f.text)}</span>
            <span class="c-mem-meta">${domain}<span>${escapeHtml(date)}</span></span>
          </button>
          <div class="c-row-body c-mem-body">
            <p class="c-hint">${escapeHtml(t("memory.fact_hint"))}</p>
            <div class="c-btns">
              <button type="button" data-idx="${factIndex}" onclick="openCorrectModal(this.dataset.idx)" class="c-btn c-sec">${escapeHtml(t("memory.edit"))}</button>
              <button type="button" data-idx="${factIndex}" onclick="openInvalidModal(this.dataset.idx)" class="c-btn c-dng">${escapeHtml(t("memory.delete"))}</button>
            </div>
          </div>
        </div>`;
          })
          .join("");

        if (isFirstLoad) container.innerHTML = factsHtml;
        else container.insertAdjacentHTML("beforeend", factsHtml);

        observeElements();
        renderFactsFooter();
      }

      // Under the list: how many are shown (of how many the chosen area holds, once the counts are in) and "Show more"
      // while the browse list has a next page. Search results have no next page.
      function renderFactsFooter() {
        if (factsMode === "idle" || (factsState === "closed" && factsMode !== "search")) return;   // closedLoad writes its own
        const countEl = document.getElementById("facts-count");
        const pageEl = document.getElementById("facts-pagination");
        if (allFacts.length === 0) return;
        const total = factsMode === "browse" ? areaCount(browseDomain) : null;
        countEl.textContent = total === null || total < allFacts.length
          ? t("memory.shown").replace("{n}", allFacts.length)
          : t("memory.shown_of").replace("{n}", allFacts.length).replace("{total}", total);
        pageEl.innerHTML = factsMode === "browse" && browseCursor
          ? `<button type="button" onclick="browseNextPage()" class="c-lnk">${escapeHtml(t("memory.more"))}</button>`
          : "";
      }

      // One fact row open at a time; its body holds Edit and Delete.
      function toggleFactRow(idx) {
        const list = document.getElementById("facts-container");
        list.querySelectorAll(".c-mem-item.open").forEach((item) => {
          if (item.dataset.idx !== String(idx)) setFactRowOpen(item, false);
        });
        const item = list.querySelector(`.c-mem-item[data-idx="${CSS.escape(String(idx))}"]`);
        if (item) setFactRowOpen(item, !item.classList.contains("open"));
      }

      function setFactRowOpen(item, open) {
        item.classList.toggle("open", open);
        item.querySelector(".c-mem-fact").setAttribute("aria-expanded", String(open));
      }

      /* ── Life areas: one select with counts (CABINET_REDESIGN_RFC §5.2) ── */
      // GET /api/user/facts/domain-counts of the state on screen: {state, total, counts}; null until it is read (or when
      // it failed: the list then offers every area, without counts).
      let areaCounts = null,
        areaListOpen = false;

      async function loadAreaCounts() {
        const state = factsState;
        if (state === "closed") return;
        let read = null;
        try {
          const res = await factsFetch(
            `${API}/api/user/facts/domain-counts?state=${encodeURIComponent(state)}`,
            { credentials: "include" },
          );
          if (res.ok) {
            const data = await res.json();
            read = { state, total: data.total || 0, counts: data.counts || {} };
          }
        } catch {
          // the select works without counts
        }
        if (state !== factsState) return;   // another state was chosen while this read was out
        areaCounts = read;
        renderAreaSelect();
        renderFactsFooter();
      }

      // The number of facts of an area ("all": the total) in the state on screen; null while the counts are unknown.
      function areaCount(key) {
        if (!areaCounts || areaCounts.state !== factsState) return null;
        return key === "all" ? areaCounts.total : areaCounts.counts[key] || 0;
      }

      // The list: only the areas with facts in this state, the fullest first. Without counts: every area, in order.
      function areaOptions() {
        const known = [...factDomains.map((d) => ({ label: d.label, value: d.key }))];
        if (areaCount("all") === null) return { shown: known, hidden: 0 };
        const shown = known
          .filter((c) => areaCount(c.value) > 0)
          .sort((a, b) => areaCount(b.value) - areaCount(a.value));
        return { shown, hidden: known.length - shown.length };
      }

      function renderAreaSelect() {
        const away = factsState === "closed" || factsMode === "search";
        document.getElementById("area-select").classList.toggle("hidden", away);
        const reset = document.getElementById("area-reset");
        reset.textContent = t("memory.area_reset");
        reset.classList.toggle("hidden", away || browseDomain === "all");
        if (away) closeAreaList();
        document.getElementById("area-select-btn").classList.toggle("set", browseDomain !== "all");
        document.getElementById("area-select-label").textContent =
          browseDomain === "all" ? t("memory.all_areas") : domainLabel(browseDomain);
        const n = areaCount(browseDomain);
        document.getElementById("area-select-count").textContent = n === null ? "" : `· ${n}`;
        if (areaListOpen) renderAreaList();
      }

      function renderAreaList() {
        const { shown, hidden } = areaOptions();
        const filter = document.getElementById("area-filter");
        filter.placeholder = t("memory.area_filter");
        filter.setAttribute("aria-label", t("memory.area_filter"));
        const q = filter.value.trim().toLowerCase();
        const all = { label: t("memory.all_areas"), value: "all" };
        const rows = [all, ...shown.filter((c) => !q || c.label.toLowerCase().includes(q))];
        document.getElementById("area-list").innerHTML = rows
          .map((c) => {
            const n = areaCount(c.value);
            const on = c.value === browseDomain;
            return `<button type="button" role="option" aria-selected="${on}" class="c-pick-opt${on ? " on" : ""}" data-domain="${escapeHtml(c.value)}" onclick="setDomainFilter(this.dataset.domain)"><span>${escapeHtml(c.label)}</span><span class="c-pick-count">${n === null ? "" : n}</span></button>`;
          })
          .join("");
        document.getElementById("area-note").textContent = t("memory.areas_overlap");
        const foot = document.getElementById("area-foot");
        foot.textContent = hidden > 0 ? t("memory.areas_hidden").replace("{n}", hidden) : "";
        foot.classList.toggle("hidden", hidden === 0);
      }

      function toggleAreaList() {
        if (areaListOpen) closeAreaList();
        else openAreaList();
      }

      function openAreaList() {
        areaListOpen = true;
        const filter = document.getElementById("area-filter");
        filter.value = "";
        renderAreaList();
        document.getElementById("area-select").classList.add("open");
        document.getElementById("area-pop").classList.remove("hidden");
        document.getElementById("area-select-btn").setAttribute("aria-expanded", "true");
        // a phone would raise its keyboard over the list: the filter is focused on a pointer screen only
        if (!window.matchMedia("(pointer: coarse)").matches) filter.focus();
      }

      function closeAreaList() {
        if (!areaListOpen) return;
        areaListOpen = false;
        document.getElementById("area-select").classList.remove("open");
        document.getElementById("area-pop").classList.add("hidden");
        document.getElementById("area-select-btn").setAttribute("aria-expanded", "false");
      }

      function filterAreas() {
        renderAreaList();
      }

      // A click outside the select, or Escape, closes its list.
      document.addEventListener("click", (e) => {
        if (areaListOpen && !e.target.closest("#area-select")) closeAreaList();
      });
      document.addEventListener("keydown", (e) => {
        if (areaListOpen && e.key === "Escape") closeAreaList();
      });

      /* ── The review questions, collapsed into one row at the top ── */
      // The row shows how many questions wait; it opens to the question UI above (#review-questions).
      function renderReviewRow() {
        const wrap = document.getElementById("memory-review");
        const box = document.getElementById("review-questions");
        const n = reviewQuestions.length;
        wrap.classList.toggle("hidden", n === 0 || box.classList.contains("hidden"));
        document.getElementById("memory-review-count").textContent = String(n);
        document.getElementById("memory-review-title").textContent = t("memory.review_row");
      }

      function toggleMemoryReview() {
        const wrap = document.getElementById("memory-review");
        const open = !wrap.classList.contains("open");
        wrap.classList.toggle("open", open);
        const row = document.getElementById("memory-review-row");
        row.classList.toggle("open", open);
        row.setAttribute("aria-expanded", String(open));
      }

      /* ── The memory inbox: what the user's outside assistants sent, one card at a time ── */
      // The row shows how many candidates wait for the owner and opens to the first one. Each card is answered alone:
      // there is no selection and no "all". "True" sends only what the owner edited; memory still decides what to keep.
      let inboxCandidates = [],
        inboxConfirmed = 0,
        inboxBasis = "inferred",
        inboxBusy = false;
      const INBOX_BASES = ["said", "document", "inferred"];

      async function loadMemoryInbox() {
        try {
          const res = await factsFetch(`${API}/api/user/memory-inbox`, { credentials: "include" });
          if (res.ok) {
            const data = await res.json();
            inboxCandidates = data.candidates || [];
            inboxConfirmed = data.confirmed || 0;
          } else {
            inboxCandidates = [];
            inboxConfirmed = 0;
          }
        } catch {
          inboxCandidates = [];
          inboxConfirmed = 0;
        }
        renderMemoryInbox();
      }

      function renderMemoryInbox() {
        const wrap = document.getElementById("memory-inbox");
        const n = factsMode === "idle" ? 0 : inboxCandidates.length;   // nothing of the inbox shows while the tab is locked
        wrap.classList.toggle("hidden", n === 0);
        document.getElementById("memory-inbox-count").textContent = String(n);
        document.getElementById("memory-inbox-title").textContent = t("memory.inbox_row");
        const body = document.getElementById("memory-inbox-body");
        if (n === 0) {
          body.innerHTML = "";
          return;
        }
        inboxBasis = INBOX_BASES.includes(inboxCandidates[0].basis) ? inboxCandidates[0].basis : "inferred";
        body.innerHTML = inboxCardHtml(inboxCandidates[0]);
      }

      function toggleMemoryInbox() {
        const wrap = document.getElementById("memory-inbox");
        const open = !wrap.classList.contains("open");
        wrap.classList.toggle("open", open);
        const row = document.getElementById("memory-inbox-row");
        row.classList.toggle("open", open);
        row.setAttribute("aria-expanded", String(open));
      }

      function inboxWord(prefix, value) {
        const key = prefix + value;
        const word = t(key);
        return word === key ? String(value) : word;
      }

      // The draft classification in words: what memory would likely make of the candidate (advisory; memory decides).
      function inboxClassWords(draft) {
        const d = draft || {};
        const parts = [];
        if (d.directive) parts.push(t("memory.inbox_directive"));
        parts.push(inboxWord("memory.inbox_temporal_", d.temporal_class), inboxWord("memory.inbox_priority_", d.context_priority));
        const e = d.expiry || {};
        if (e.kind === "date") parts.push(t("memory.inbox_expiry_date").replace("{date}", () => shortDate(e.date)));
        else if (e.kind === "horizon") parts.push(t("memory.inbox_expiry_horizon").replace("{days}", () => e.days));
        else parts.push(t("memory.inbox_expiry_none"));
        return t("memory.inbox_class").replace("{what}", () => parts.join(", "));
      }

      function inboxRelationLine(rel) {
        const r = rel || { kind: "new" };
        const key = "memory.inbox_rel_" + r.kind;
        const text = t(key) === key ? t("memory.inbox_rel_new") : t(key);
        return text.replace("{fact}", () => r.fact_text || "");
      }

      function inboxBasisButtons() {
        return INBOX_BASES.map(
          (b) =>
            `<button type="button" data-basis="${b}" onclick="setInboxBasis(this.dataset.basis)" class="${b === inboxBasis ? "on" : ""}" aria-pressed="${b === inboxBasis}">${escapeHtml(t("memory.inbox_basis_" + b))}</button>`,
        ).join("");
      }

      function setInboxBasis(basis) {
        if (!INBOX_BASES.includes(basis)) return;
        inboxBasis = basis;
        document.getElementById("inbox-basis").innerHTML = inboxBasisButtons();
      }

      function inboxCardHtml(c) {
        const from = t("memory.inbox_from")
          .replace("{source}", () => c.source || "")
          .replace("{date}", () => shortDate(c.received));
        const confirmed = inboxConfirmed > 0 ? `<p class="c-hint">${escapeHtml(t("memory.inbox_confirmed").replace("{n}", () => inboxConfirmed))}</p>` : "";
        const raw = (c.raw || [])
          .map((r) => {
            const meta = [t("memory.inbox_basis_" + r.basis), r.said_when, r.happens_when, shortDate(r.received)]
              .filter((x) => x && x !== "memory.inbox_basis_" + r.basis)
              .join(" · ");
            return `<blockquote><p>${escapeHtml(r.information)}</p><p class="c-hint">${escapeHtml(meta)}</p></blockquote>`;
          })
          .join("");
        return `
        <div class="c-inbox-card" data-candidate="${escapeHtml(c.candidate_id)}">
          <p class="c-hint">${escapeHtml(from)}</p>
          <label>${escapeHtml(t("memory.inbox_field_text"))}
            <textarea id="inbox-text" class="c-input" rows="3">${escapeHtml(c.text)}</textarea></label>
          <label>${escapeHtml(t("memory.inbox_field_happens"))}
            <input id="inbox-happens" type="text" class="c-input" value="${escapeHtml(c.happens_when)}" /></label>
          <label>${escapeHtml(t("memory.inbox_field_said"))}
            <input id="inbox-said" type="text" class="c-input" value="${escapeHtml(c.said_when)}" /></label>
          <div>
            <span class="c-hint">${escapeHtml(t("memory.inbox_field_basis"))}</span>
            <div id="inbox-basis" class="c-seg" role="group" aria-label="${escapeHtml(t("memory.inbox_field_basis"))}">${inboxBasisButtons()}</div>
          </div>
          <p class="c-hint">${escapeHtml(inboxClassWords(c.draft))}</p>
          <p>${escapeHtml(inboxRelationLine(c.relation))}</p>
          <details><summary>${escapeHtml(t("memory.inbox_raw"))}</summary>${raw}</details>
          <div class="c-btns">
            <button type="button" id="inbox-true" onclick="answerInbox('confirm')" class="c-btn c-pri">${escapeHtml(t("memory.inbox_true"))}</button>
            <button type="button" id="inbox-not-true" onclick="answerInbox('reject')" class="c-btn c-sec">${escapeHtml(t("memory.inbox_not_true"))}</button>
          </div>
          <p class="c-hint">${escapeHtml(t("memory.inbox_note"))}</p>
          ${confirmed}
        </div>`;
      }

      // Only the fields the owner changed; trimmed as the server reads them.
      function inboxEdits(c) {
        const edits = {};
        const text = document.getElementById("inbox-text").value.trim();
        const happens = document.getElementById("inbox-happens").value.trim();
        const said = document.getElementById("inbox-said").value.trim();
        if (text !== c.text.trim()) edits.text = text;
        if (happens !== c.happens_when.trim()) edits.happens_when = happens;
        if (said !== c.said_when.trim()) edits.said_when = said;
        if (inboxBasis !== c.basis) edits.basis = inboxBasis;
        return edits;
      }

      // "True" (confirm, with the edits if any) or "Not true" (reject) on the card on screen; the card leaves the list.
      async function answerInbox(verb) {
        const c = inboxCandidates[0];
        if (!c || inboxBusy) return;
        inboxBusy = true;
        document.getElementById("inbox-true").disabled = true;
        document.getElementById("inbox-not-true").disabled = true;
        try {
          const init = { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" } };
          if (verb === "confirm") init.body = JSON.stringify(inboxEdits(c));
          const res = await factsFetch(`${API}/api/user/memory-inbox/${encodeURIComponent(c.candidate_id)}/${verb}`, init);
          if (res.ok) {
            inboxCandidates.shift();
            if (verb === "confirm") inboxConfirmed += 1;
            renderMemoryInbox();
            return;
          }
          let error = "";
          try {
            error = (await res.json()).error || "";
          } catch { /* no body */ }
          if (error === "facts_locked") return;
          if (res.status === 400) {
            showToast(t(error === "empty_text" ? "memory.inbox_err_empty" : error === "too_long" ? "memory.inbox_err_too_long" : "memory.inbox_failed"), "error");
          } else if (res.status === 403 || res.status === 404 || res.status === 409) {
            showToast(t("memory.inbox_err_gone"), "error");
            await loadMemoryInbox();
          } else {
            showToast(t("memory.inbox_failed"), "error");
          }
        } catch {
          showToast(t("memory.inbox_failed"), "error");
        } finally {
          inboxBusy = false;
          const yes = document.getElementById("inbox-true");
          const no = document.getElementById("inbox-not-true");
          if (yes) yes.disabled = false;
          if (no) no.disabled = false;
        }
      }

      /* ── The memory inbox's settings: triage frequency and connected apps ── */
      // Shown whenever the Memory tab is unlocked, not only while candidates wait. The frequency saves on change; a
      // revoke asks first and stops the app's next submission at once.
      let inboxSettings = null,
        inboxApps = [];

      async function loadInboxSettings() {
        try {
          const [s, a] = await Promise.all([
            factsFetch(`${API}/api/user/memory-inbox/settings`, { credentials: "include" }),
            factsFetch(`${API}/api/user/memory-inbox/apps`, { credentials: "include" }),
          ]);
          inboxSettings = s.ok ? await s.json() : null;
          inboxApps = a.ok ? (await a.json()).apps || [] : [];
        } catch {
          inboxSettings = null;
          inboxApps = [];
        }
        renderInboxSettings();
      }

      function inboxWhen(iso) {
        return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : t("memory.inbox_settings_unknown");
      }

      function renderInboxSettings() {
        const wrap = document.getElementById("memory-inbox-settings");
        const show = factsMode !== "idle" && inboxSettings !== null;   // nothing shows while the tab is locked
        wrap.classList.toggle("hidden", !show);
        const list = document.getElementById("inbox-apps");
        if (!show) {
          list.innerHTML = "";
          return;
        }
        document.getElementById("inbox-settings-title").textContent = t("memory.inbox_settings_title");
        document.getElementById("inbox-interval-label").textContent = t("memory.inbox_settings_interval");
        document.getElementById("inbox-apps-title").textContent = t("memory.inbox_settings_apps");
        document.getElementById("inbox-apps-hint").textContent = t("memory.inbox_settings_hint");
        const select = document.getElementById("inbox-interval");
        select.innerHTML = (inboxSettings.choices || []).map((d) =>
          `<option value="${d}">${escapeHtml(t("memory.inbox_settings_every_" + d))}</option>`).join("");
        select.value = String(inboxSettings.triage_interval_days);
        list.innerHTML = inboxApps.length ? inboxApps.map((app) => `<div class="c-mini-s">
            ${escapeHtml(app.client_name)} · ${escapeHtml(t("memory.inbox_settings_granted"))}: ${escapeHtml(inboxWhen(app.granted_at))} ·
            ${escapeHtml(t("memory.inbox_settings_refreshed"))}: ${escapeHtml(inboxWhen(app.last_refresh))}
            <button type="button" class="c-lnk c-lnk-danger" data-grant="${escapeHtml(app.grant_id)}" onclick="revokeInboxApp(this.dataset.grant)">${escapeHtml(t("memory.inbox_settings_revoke"))}</button>
          </div>`).join("") : `<div class="c-mini-s">${escapeHtml(t("memory.inbox_settings_none"))}</div>`;
      }

      async function saveInboxInterval() {
        const note = document.getElementById("inbox-interval-note");
        const days = Number(document.getElementById("inbox-interval").value);
        try {
          const res = await factsFetch(`${API}/api/user/memory-inbox/settings`, {
            method: "PUT", credentials: "include", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ triage_interval_days: days }),
          });
          if (res.ok) {
            inboxSettings = await res.json();
            note.textContent = t("memory.inbox_settings_saved");
            return;
          }
        } catch { /* shown below */ }
        note.textContent = t("memory.inbox_settings_failed");
        if (inboxSettings) document.getElementById("inbox-interval").value = String(inboxSettings.triage_interval_days);
      }

      async function revokeInboxApp(grantId) {
        if (!confirm(t("memory.inbox_settings_revoke_confirm"))) return;
        try {
          const res = await factsFetch(`${API}/api/user/memory-inbox/apps/${encodeURIComponent(grantId)}`,
            { method: "DELETE", credentials: "include" });
          if (res.ok) {
            showToast(t("memory.inbox_settings_revoked"), "success");
            await loadInboxSettings();
            return;
          }
        } catch { /* shown below */ }
        showToast(t("memory.inbox_settings_failed"), "error");
      }

      // The language changed while Memory is open: its controls are drawn again in it.
      function renderMemoryControls() {
        renderStateChips();
        renderAreaSelect();
        renderReviewRow();
        renderMemoryInbox();
        renderInboxSettings();
      }

      let _modalFactIdx = null;

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
          loadAreaCounts();
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
