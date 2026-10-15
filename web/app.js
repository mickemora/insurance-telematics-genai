/* Telematics Desk: drivers, claims, and the Bedrock Agent copilot.
   Plain JavaScript, no build step. All requests go to /api/* on the same CloudFront
   origin; CloudFront adds the API key, so it never reaches the browser. */
(function () {
  "use strict";

  const main = document.getElementById("main");
  const FACTOR_LABELS = {
    hard_braking: "Hard braking", rapid_acceleration: "Rapid acceleration", harsh_cornering: "Harsh cornering",
    speeding: "Speeding", phone_handling: "Phone handling", late_night: "Late-night driving", general: "Keep it up",
  };
  const BAND_LABELS = { excellent: "Excellent", good: "Good", fair: "Fair", needs_attention: "Needs attention" };
  const STATUS_LABELS = {
    detected: "Detected", awaiting_statement: "Waiting for member", drafting: "Drafting",
    draft_ready_for_adjuster: "Draft ready", closed_false_alarm: "Closed: false alarm",
  };
  const VERDICT_CHIPS = {
    consistent: ["go", "Statement matches data"], partially_consistent: ["caution", "Partly matches data"],
    inconsistent: ["stop", "Statement contradicts data"], no_statement: ["", "No statement"],
  };
  const state = { drivers: null, claims: null, chart: null, polls: new Set(), copilot: { session: null, turns: [] } };

  // ------------------------------------------------------------------ helpers
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 1) => (n === null || n === undefined ? "–" : Number(n).toFixed(d).replace(/\.0$/, ""));

  async function api(path, options = {}) {
    const resp = await fetch("/api" + path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    let body = {};
    try { body = await resp.json(); } catch (e) { /* empty body */ }
    if (!resp.ok) {
      const err = new Error(body.message || `Request failed (${resp.status})`);
      err.status = resp.status;
      throw err;
    }
    return body;
  }

  function stopPolls() {
    state.polls.forEach((id) => clearTimeout(id));
    state.polls.clear();
  }

  function poll(fn, ms) {
    const id = setTimeout(async () => { state.polls.delete(id); await fn(); }, ms);
    state.polls.add(id);
  }

  function delta(change) {
    if (change === null || change === undefined) return '<span class="delta-flat">first week</span>';
    if (Math.abs(change) < 3) return `<span class="delta-flat">steady (${change > 0 ? "+" : ""}${fmt(change)})</span>`;
    return change > 0
      ? `<span class="delta-up">up ${fmt(change)}</span>`
      : `<span class="delta-down">down ${fmt(Math.abs(change))}</span>`;
  }

  function plate(score, band, mini) {
    return `<span class="plate ${mini ? "mini" : ""} ${esc(band)}" role="img" aria-label="Score ${fmt(score, 0)}">
      ${mini ? "" : '<span class="plate-label">SCORE</span>'}
      <span class="plate-value">${fmt(score, 0)}</span></span>`;
  }

  function setTab(name) {
    document.querySelectorAll(".tabs a").forEach((a) => {
      if (a.dataset.tab === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  function renderError(target, err) {
    target.innerHTML = `<div class="empty"><strong>Couldn't load this</strong>${esc(err.message)}.
      If the stack was just deployed, give CloudFront a minute and reload.</div>`;
  }

  // simple, safe markdown: paragraphs, bullet lists, **bold**
  function md(text) {
    const lines = esc(text).split(/\n/);
    let html = "", list = false;
    for (const raw of lines) {
      const line = raw.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      const bullet = line.match(/^\s*[-*•]\s+(.*)/);
      if (bullet) {
        if (!list) { html += "<ul>"; list = true; }
        html += `<li>${bullet[1]}</li>`;
      } else {
        if (list) { html += "</ul>"; list = false; }
        if (line.trim()) html += `<p>${line}</p>`;
      }
    }
    return html + (list ? "</ul>" : "");
  }

  // ------------------------------------------------------------------ drivers
  async function showDrivers(driverId) {
    setTab("drivers");
    if (!document.getElementById("driver-list")) {
      main.innerHTML = `<div class="split">
        <section class="panel" aria-labelledby="drivers-h">
          <div class="panel-head"><h1 id="drivers-h">Drivers</h1><p>Latest complete week, compared with the week before</p></div>
          <div id="driver-list"><div class="loading">Loading drivers…</div></div>
        </section>
        <section class="panel detail" id="driver-detail" aria-live="polite"></section>
      </div>`;
    }
    try {
      if (!state.drivers) state.drivers = (await api("/drivers")).drivers;
    } catch (err) { return renderError(document.getElementById("driver-list"), err); }
    renderDriverList(driverId);
    const detail = document.getElementById("driver-detail");
    if (!driverId) {
      detail.innerHTML = `<div class="empty"><strong>Pick a driver</strong>See their weekly score, what cost them points, and an AI explanation they could read in the app.</div>`;
      return;
    }
    detail.innerHTML = '<div class="loading">Loading driver…</div>';
    try {
      renderDriver(detail, await api(`/drivers/${encodeURIComponent(driverId)}/score`));
      revealOnSmallScreens(detail);
    } catch (err) { renderError(detail, err); }
  }

  function renderDriverList(selected) {
    const rows = state.drivers.map((d) => {
      const w = d.latest_week || {};
      return `<tr class="row" tabindex="0" data-href="#/drivers/${esc(d.driver_id)}" aria-selected="${d.driver_id === selected}">
        <td><span class="who">${esc(d.name)}</span><span class="sub">${esc(d.driver_id)}</span></td>
        <td class="hide-sm">${esc(d.home_metro)}</td>
        <td>${w.score !== undefined ? plate(w.score, w.band, true) : "–"}</td>
        <td>${delta(d.score_change)}</td>
      </tr>`;
    }).join("");
    document.getElementById("driver-list").innerHTML = state.drivers.length
      ? `<table class="list"><thead><tr><th>Driver</th><th class="hide-sm">Home</th><th>Score</th><th>Change</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<div class="empty"><strong>No drivers yet</strong>Run <code>make generate</code> to create synthetic drivers and trips.</div>`;
  }

  function renderDriver(el, d) {
    const w = d.latest_week || {};
    const v = d.vehicle || {};
    const pens = Object.entries(w.penalties || {}).sort((a, b) => b[1] - a[1]);
    const maxCap = 25;
    const factors = pens.map(([k, p], i) => `<div class="factor ${i === 0 && p >= 1 ? "top" : ""}">
        <span>${FACTOR_LABELS[k]}</span>
        <span class="bar" aria-hidden="true"><span style="width:${Math.min(100, (p / maxCap) * 100)}%"></span></span>
        <span class="pts">${p >= 0.5 ? "−" + fmt(p) : "0"}</span></div>`).join("");
    const trips = (d.recent_trips || []).map((t) => {
      const top = Object.entries(t.penalties).sort((a, b) => b[1] - a[1])[0];
      const when = new Date(t.local_start + "Z");
      const label = when.toLocaleString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" });
      return `<tr><td>${esc(label)}</td><td class="num">${fmt(t.miles)} mi</td>
        <td>${plate(t.score, t.band, true)}</td>
        <td>${top && top[1] >= 1 ? esc(FACTOR_LABELS[top[0]]) : '<span class="sub">clean trip</span>'}
          ${t.possible_crash ? ' <span class="chip stop">possible crash</span>' : ""}</td></tr>`;
    }).join("");

    el.innerHTML = `<div class="panel-body">
      <div class="driver-hero">
        ${plate(w.score, w.band)}
        <div>
          <h2>${esc(d.name)}</h2>
          <p>${esc(d.driver_id)}, ${esc([v.year, v.make, v.model].filter(Boolean).join(" "))}, ${esc(d.home_metro)}</p>
          <p class="change">Week ${esc(w.week_id)}: <strong>${esc(BAND_LABELS[w.band] || "")}</strong>, ${delta(d.score_change)} from the week before</p>
        </div>
      </div>

      <div class="section">
        <button class="explain-btn" id="explain-btn" type="button">Explain this score with AI</button>
        <div id="explanation"></div>
      </div>

      <div class="section"><h3>Weekly score</h3><div class="chart-box"><canvas id="week-chart" aria-label="Weekly score chart"></canvas></div></div>
      <div class="section"><h3>Points lost this week</h3><div class="factors">${factors}</div></div>
      <div class="section"><h3>Recent trips</h3>
        <table class="list trips"><thead><tr><th>Started</th><th class="num">Distance</th><th>Score</th><th>Main issue</th></tr></thead><tbody>${trips}</tbody></table>
      </div>
    </div>`;

    drawWeeks(d.weeks || []);
    document.getElementById("explain-btn").addEventListener("click", () => explainDriver(d.driver_id, false));
  }

  function drawWeeks(weeks) {
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    const canvas = document.getElementById("week-chart");
    if (!canvas || typeof Chart === "undefined") return;
    state.chart = new Chart(canvas, {
      type: "line",
      data: {
        labels: weeks.map((w) => w.week_id.replace(/^\d{4}-/, "")),
        datasets: [{ data: weeks.map((w) => w.score), borderColor: "#14213d", backgroundColor: "#14213d",
          pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5, tension: 0.25 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `Score ${fmt(c.parsed.y)}` } } },
        scales: {
          y: { min: 0, max: 100, ticks: { stepSize: 25, font: { family: "Overpass" } }, grid: { color: "#edf0f4" } },
          x: { ticks: { font: { family: "Overpass" } }, grid: { display: false } },
        },
      },
    });
  }

  async function explainDriver(driverId, refresh) {
    const btn = document.getElementById("explain-btn");
    const out = document.getElementById("explanation");
    btn.disabled = true;
    out.innerHTML = '<p class="thinking"><span class="dash"></span>Reading the week and drafting an explanation…</p>';
    try {
      const e = await api(`/drivers/${encodeURIComponent(driverId)}/explanation${refresh ? "?refresh=true" : ""}`);
      const tips = e.tips.map((t) => `<li><span><span class="factor-name">${esc(FACTOR_LABELS[t.factor] || t.factor)}</span>${esc(t.tip)}</span></li>`).join("");
      const u = e.usage || {};
      out.innerHTML = `<div class="explanation">
          <h4>${esc(e.headline)}</h4>
          <p>${esc(e.summary)}</p>
          <ol class="tips">${tips}</ol>
        </div>
        <div class="provenance">
          <span>${e.source === "bedrock" ? "Written by " + esc(e.model_id) : "Template fallback: " + esc(e.fallback_reason)}</span>
          <span>Guardrail ${esc(e.guardrail)}</span>
          ${u.inputTokens ? `<span>${u.inputTokens} in / ${u.outputTokens} out tokens</span>` : ""}
          <span>${e.cached ? "Cached" : esc(e.latency_ms) + " ms"}</span>
          <button class="link-btn" type="button" id="regen">Regenerate</button>
        </div>
        <details class="facts"><summary>What the model saw</summary><pre>${esc(JSON.stringify(e.facts, null, 2))}</pre></details>`;
      document.getElementById("regen").addEventListener("click", () => explainDriver(driverId, true));
    } catch (err) {
      out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ claims
  async function showClaims(claimId) {
    setTab("claims");
    if (!document.getElementById("claim-list")) {
      main.innerHTML = `<div class="split">
        <section class="panel" aria-labelledby="claims-h">
          <div class="panel-head"><h1 id="claims-h">Claims</h1><p>Opened automatically when a phone detects a possible crash</p></div>
          <div id="claim-list"><div class="loading">Loading claims…</div></div>
        </section>
        <section class="panel detail" id="claim-detail" aria-live="polite"></section>
      </div>`;
    }
    await refreshClaimList(claimId);
    const detail = document.getElementById("claim-detail");
    if (!claimId) {
      detail.innerHTML = `<div class="empty"><strong>Pick a claim</strong>Follow a detected crash from triage to the member's statement to an AI-drafted first notice of loss.</div>`;
      return;
    }
    detail.innerHTML = '<div class="loading">Loading claim…</div>';
    await loadClaim(claimId);
    revealOnSmallScreens(detail);
  }

  async function refreshClaimList(selected) {
    const list = document.getElementById("claim-list");
    try {
      state.claims = (await api("/claims")).claims;
    } catch (err) { return renderError(list, err); }
    if (!state.claims.length) {
      list.innerHTML = `<div class="empty"><strong>No claims yet</strong>Run <code>make generate</code>. Planted crashes open claims within about a minute.</div>`;
      return;
    }
    list.innerHTML = `<table class="list"><thead><tr><th>Claim</th><th class="hide-sm">Detected</th><th>Status</th></tr></thead><tbody>${
      state.claims.map((c) => `<tr class="row" tabindex="0" data-href="#/claims/${esc(c.claim_id)}" aria-selected="${c.claim_id === selected}">
        <td><span class="who">${esc(c.member || "Member")}</span><span class="sub">${esc(c.claim_id)}, ${esc(c.triage_label)}</span></td>
        <td class="hide-sm">${esc(c.detected_at_local)}</td>
        <td>${statusChip(c.status)}${c.verdict ? "<br>" + verdictChip(c.verdict) : ""}</td></tr>`).join("")
    }</tbody></table>`;
  }

  function statusChip(status) {
    const cls = { draft_ready_for_adjuster: "go", awaiting_statement: "lane", drafting: "lane", closed_false_alarm: "" }[status] ?? "";
    return `<span class="chip ${cls}">${esc(STATUS_LABELS[status] || status)}</span>`;
  }
  function verdictChip(verdict) {
    const [cls, label] = VERDICT_CHIPS[verdict] || ["", verdict];
    return `<span class="chip ${cls}">${esc(label)}</span>`;
  }

  async function loadClaim(claimId, wasWaiting) {
    const detail = document.getElementById("claim-detail");
    try {
      const c = await api(`/claims/${encodeURIComponent(claimId)}`);
      renderClaim(detail, c);
      const waiting = c.status === "drafting" || c.status === "detected";
      if (waiting) poll(() => loadClaim(claimId, true), 3000);
      else if (wasWaiting) refreshClaimList(claimId);
    } catch (err) { renderError(detail, err); }
  }

  function revealOnSmallScreens(el) {
    if (window.matchMedia("(max-width: 980px)").matches) el.scrollIntoView({ block: "start" });
  }

  function consoleLink(executionArn) {
    if (!executionArn) return "";
    const region = executionArn.split(":")[3];
    const url = `https://${region}.console.aws.amazon.com/states/home?region=${region}#/v2/executions/details/${encodeURIComponent(executionArn)}`;
    return `<a href="${esc(url)}" target="_blank" rel="noopener">Open this run in Step Functions</a>`;
  }

  function renderClaim(el, c) {
    const f = c.crash_facts, i = f.impact, loc = f.location;
    const where = loc.address || `${loc.lat}, ${loc.lon}`;
    const timeline = f.timeline.map((r) => `<li class="${r.when.includes("impact)") ? "impact" : ""}">
        <span class="when">${esc(r.when)}</span><span class="what">${esc(r.what)}</span></li>`).join("");
    const checks = (c.statement_checks || []).map((k) => `<li>
        <span class="chip ${k.consistent ? "go" : "stop"}">${k.consistent ? "Matches" : "Differs"}</span>
        <span><strong>${esc(k.aspect)}:</strong> member says ${esc(k.statement_says)}; data shows ${esc(k.data_shows)}</span></li>`).join("");

    let statementBlock = "";
    if (c.status === "awaiting_statement") {
      statementBlock = `<div class="section statement-form"><h3>Member statement</h3>
        <label for="stmt" class="sub">Type what the member said, or fill an example to test the cross-check.</label>
        <textarea id="stmt" maxlength="1000"></textarea>
        <div class="form-row">
          <button class="btn" type="button" data-fill="true">Fill truthful example</button>
          <button class="btn" type="button" data-fill="false">Fill inconsistent example</button>
          <button class="btn primary" type="button" id="send-stmt">Send statement</button>
          <span id="stmt-status" class="sub" role="status"></span>
        </div></div>`;
    } else if (c.statement) {
      statementBlock = `<div class="section"><h3>Member statement</h3><p class="quote">“${esc(c.statement)}”</p>
        ${checks ? `<ul class="checks">${checks}</ul>` : ""}</div>`;
    }

    let draftBlock = "";
    if (c.status === "drafting") {
      draftBlock = '<div class="section"><p class="thinking"><span class="dash"></span>Claude is drafting the first notice of loss…</p></div>';
    } else if (c.draft) {
      const d = c.draft, sa = d.statement_assessment, u = d.usage || {};
      draftBlock = `<div class="section draft"><h4>First notice of loss: AI draft for the adjuster</h4>
        <p>${esc(d.incident_summary)}</p>
        <div class="verdict">${verdictChip(sa.verdict)}<span>${esc(sa.explanation)}</span></div>
        <div class="two-col">
          <div><h5>Questions for the adjuster</h5><ul>${d.adjuster_questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>
          <div><h5>Next steps</h5><ul>${d.next_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>
        </div>
        <div class="provenance">
          <span>${d.source === "bedrock" ? "Drafted by " + esc(d.model_id) : "Template fallback: " + esc(d.fallback_reason)}</span>
          <span>Guardrail ${esc(d.guardrail)}</span>
          ${u.inputTokens ? `<span>${u.inputTokens} in / ${u.outputTokens} out tokens</span>` : ""}
          <span>${esc(d.latency_ms)} ms</span>
        </div>
        <p class="review-note">The AI drafts; an adjuster decides. Nothing here determines fault, coverage, or payment.</p>
      </div>`;
    }

    el.innerHTML = `<div class="panel-body">
      <div class="claim-hero">
        <h2>${esc(f.member.first_name)} ${esc(f.member.last_initial)}., claim ${esc(c.claim_id)}</h2>
        <p>${esc(f.detected_at_local)} near ${esc(where)}</p>
        <p>${esc(f.member.vehicle)}, policy ${esc(f.member.policy_id)}</p>
        <div class="chips">${statusChip(c.status)}<span class="chip ${c.triage === "likely_crash" ? "stop" : c.triage === "likely_false_alarm" ? "" : "caution"}">Triage: ${esc(c.triage_label)}</span></div>
        <ul class="reasons">${c.triage_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>
      <div class="section"><h3>What the phone recorded</h3><ol class="lane-strip">${timeline}</ol></div>
      ${c.member_message ? `<div class="section"><h3>Message sent to the member</h3><p class="message">${esc(c.member_message)}</p></div>` : ""}
      ${statementBlock}
      ${draftBlock}
      <p class="review-note">${consoleLink(c.execution_id)}</p>
    </div>`;

    if (c.status === "awaiting_statement") {
      el.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", () => {
        document.getElementById("stmt").value = simulateStatement(f, b.dataset.fill === "true");
      }));
      document.getElementById("send-stmt").addEventListener("click", () => sendStatement(c.claim_id));
    }
  }

  async function sendStatement(claimId) {
    const text = document.getElementById("stmt").value.trim();
    const status = document.getElementById("stmt-status");
    if (!text) { status.textContent = "Add the member's statement first."; return; }
    document.getElementById("send-stmt").disabled = true;
    status.textContent = "Sending…";
    try {
      await api(`/claims/${encodeURIComponent(claimId)}/statement`, { method: "POST", body: JSON.stringify({ statement: text }) });
      status.textContent = "Statement sent.";
      await refreshClaimList(claimId);
      await loadClaim(claimId);
    } catch (err) {
      status.textContent = err.message;
      document.getElementById("send-stmt").disabled = false;
    }
  }

  // Mirrors lambdas/shared/python/crash.py simulate_statement()
  function simulateStatement(f, truthful) {
    const i = f.impact, before = i.speed_5s_before_mph, at = i.speed_at_impact_mph, braked = i.braking_before_impact;
    if (!truthful) {
      return at >= 12
        ? "I was only going about 5 mph and someone hit me from behind. There was nothing I could do."
        : "I was going about 35 mph when the front of my car hit the car ahead. I braked hard but it was too late.";
    }
    if (i.direction === "rear") {
      return at <= 5
        ? "I was stopped at a red light when the car behind me rear-ended me. I'm a bit shaken but okay."
        : `I was driving about ${before} mph when someone hit me from behind.`;
    }
    if (i.direction === "left" || i.direction === "right") {
      const side = i.direction === "left" ? "driver's side" : "passenger side";
      return `Another car came out of the intersection and hit the ${side} of my car. I was going about ${before} mph.${braked ? " I braked hard but couldn't avoid it." : ""}`;
    }
    return `Traffic ahead slowed suddenly. ${braked ? "I hit the brakes" : "I didn't have time to brake"}, and the front of my car hit the car ahead. I was going about ${before} mph.`;
  }

  // ------------------------------------------------------------------ copilot
  const SUGGESTIONS = [
    "Summarize DRV-0020's last 10 trips",
    "Which drivers dropped the most this week, and why?",
    "Which claims have statements that don't match the data?",
    "What should I tell DRV-0003 about their phone use?",
  ];

  function sessionId() {
    if (state.copilot.session) return state.copilot.session;
    let id = null;
    try { id = sessionStorage.getItem("copilot-session"); } catch (e) { /* storage blocked */ }
    if (!id) {
      id = "s-" + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
      try { sessionStorage.setItem("copilot-session", id); } catch (e) { /* ignore */ }
    }
    state.copilot.session = id;
    return id;
  }

  function showCopilot() {
    setTab("copilot");
    main.innerHTML = `<section class="copilot" aria-labelledby="copilot-h">
      <div class="copilot-intro">
        <div><h1 id="copilot-h">Copilot</h1>
        <p>Ask about drivers, trips, and claims. An Amazon Bedrock Agent looks the answer up with read-only tools and shows each lookup it made.</p></div>
        <button class="link-btn" type="button" id="new-chat">Start a new conversation</button>
      </div>
      <div class="panel">
        <div class="log" id="log" aria-live="polite"></div>
        <div class="suggestions" id="suggestions">${SUGGESTIONS.map((s) => `<button type="button">${esc(s)}</button>`).join("")}</div>
        <form class="ask" id="ask-form">
          <label class="hide-sm" for="ask" style="position:absolute;left:-999px">Your question</label>
          <input id="ask" autocomplete="off" maxlength="1000" placeholder="Ask about a driver, a trip, or a claim">
          <button class="btn primary" type="submit">Ask</button>
        </form>
      </div>
    </section>`;
    renderTurns();
    document.getElementById("ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = document.getElementById("ask");
      if (input.value.trim()) { ask(input.value.trim()); input.value = ""; }
    });
    document.getElementById("suggestions").addEventListener("click", (e) => {
      if (e.target.tagName === "BUTTON") ask(e.target.textContent);
    });
    document.getElementById("new-chat").addEventListener("click", () => {
      state.copilot = { session: null, turns: [] };
      try { sessionStorage.removeItem("copilot-session"); } catch (e) { /* ignore */ }
      renderTurns();
    });
    document.getElementById("ask").focus();
  }

  function renderTurns() {
    const log = document.getElementById("log");
    if (!log) return;
    if (!state.copilot.turns.length) {
      log.innerHTML = `<div class="empty"><strong>Ask your first question</strong>Try one of the suggestions below. Answers usually take 5 to 20 seconds.</div>`;
      return;
    }
    log.innerHTML = state.copilot.turns.map((t) => {
      let a;
      if (t.status === "pending") {
        a = '<p class="thinking"><span class="dash"></span>Looking it up…</p>';
      } else if (t.status === "error") {
        a = `<div class="answer"><p class="error">${esc(t.error)}</p></div>`;
      } else {
        const calls = (t.tool_calls || []).map((c) => `<li><code>${esc(c.function)}</code>(${esc(Object.entries(c.parameters || {}).map(([k, v]) => `${k}=${v}`).join(", "))})
            ${c.why ? `<span class="why">${esc(c.why)}</span>` : ""}</li>`).join("");
        const u = t.usage || {};
        a = `<div class="answer">${md(t.answer || "(no answer)")}</div>
          <div class="meta"><span>${t.tool_calls.length} lookup${t.tool_calls.length === 1 ? "" : "s"}</span>
            <span>${(t.latency_ms / 1000).toFixed(1)} s</span>
            ${u.inputTokens ? `<span>${u.inputTokens} in / ${u.outputTokens} out tokens</span>` : ""}
            ${t.guardrail === "intervened" ? '<span class="error">Guardrail intervened</span>' : ""}</div>
          ${calls ? `<details class="facts"><summary>How it answered</summary><ol class="steps">${calls}</ol></details>` : ""}`;
      }
      return `<div class="turn-q">${esc(t.question)}</div><div class="turn-a">${a}</div>`;
    }).join("");
    log.lastElementChild?.scrollIntoView({ block: "nearest" });
  }

  async function ask(question) {
    const turn = { question, status: "pending" };
    state.copilot.turns.push(turn);
    renderTurns();
    try {
      const job = await api("/copilot", { method: "POST", body: JSON.stringify({ question, session_id: sessionId() }) });
      const started = Date.now();
      const check = async () => {
        try {
          const r = await api(`/copilot/${job.job_id}`);
          if (r.status === "done") Object.assign(turn, r, { status: "done" });
          else if (r.status === "error") Object.assign(turn, { status: "error", error: r.error || "The agent failed." });
          else if (Date.now() - started > 150000) Object.assign(turn, { status: "error", error: "No answer after 150 seconds. Check the CopilotWorker logs." });
          else return poll(check, 2000);
        } catch (err) { Object.assign(turn, { status: "error", error: err.message }); }
        renderTurns();
      };
      poll(check, 1500);
    } catch (err) {
      Object.assign(turn, { status: "error", error: err.message });
      renderTurns();
    }
  }

  // ------------------------------------------------------------------ routing
  function route() {
    stopPolls();
    const [, section, id] = (location.hash || "#/drivers").split("/");
    if (section === "claims") { if (!id) state.claims = null; return showClaims(id && decodeURIComponent(id)); }
    if (section === "copilot") return showCopilot();
    return showDrivers(id && decodeURIComponent(id));
  }

  main.addEventListener("click", (e) => {
    const row = e.target.closest("tr.row");
    if (row) location.hash = row.dataset.href;
  });
  main.addEventListener("keydown", (e) => {
    const row = e.target.closest("tr.row");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); location.hash = row.dataset.href; }
  });
  window.addEventListener("hashchange", () => {
    const section = (location.hash || "#/drivers").split("/")[1];
    const keep = (section === "drivers" && document.getElementById("driver-list")) ||
                 (section === "claims" && document.getElementById("claim-list"));
    if (!keep) main.innerHTML = "";
    route();
  });
  route();
})();
