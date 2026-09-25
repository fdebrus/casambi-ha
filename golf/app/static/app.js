/* Golf Day front-end: vanilla JS, no build step. */
(() => {
  "use strict";

  // ---------- persistence ----------
  const store = {
    get(k, d) { try { const v = localStorage.getItem("golf." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("golf." + k, JSON.stringify(v)); } catch {} },
    del(k) { try { localStorage.removeItem("golf." + k); } catch {} },
  };
  const clientId = store.get("clientId") || (() => { const id = Math.random().toString(36).slice(2) + Date.now().toString(36); store.set("clientId", id); return id; })();

  const S = {
    lang: store.get("lang", (navigator.language || "fr").startsWith("en") ? "en" : "fr"),
    theme: store.get("theme", "auto"),
    tab: (location.hash || "#leaderboard").slice(1),
    mode: store.get("mode", "net"),
    pin: store.get("pin", ""),
    flightId: store.get("flightId", null),
    authorName: store.get("authorName", ""),
    lastSeenPost: store.get("lastSeenPost", 0),
    notif: store.get("notif", false),
    state: null,
    feed: [],
    expanded: new Set(),
    programOpen: false,
    composerOpen: false,
    replyOpen: null,
    online: true,
  };

  // ---------- utils ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const t = (key, vars = {}) => {
    let s = (I18N[S.lang] && I18N[S.lang][key]) || I18N.fr[key] || key;
    Object.entries(vars).forEach(([k, v]) => { s = s.split("{" + k + "}").join(v); });
    return s;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleTimeString(S.lang === "fr" ? "fr-BE" : "en-GB", { hour: "2-digit", minute: "2-digit" }); };
  const fmtDate = (ymd) => { if (!ymd) return ""; const d = new Date(ymd + "T12:00:00"); return d.toLocaleDateString(S.lang === "fr" ? "fr-BE" : "en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" }); };
  const fmtHcp = (h) => h === null || h === undefined ? "" : String(h).replace(".", S.lang === "fr" ? "," : ".").replace(/,0$|\.0$/, "");
  const sign = (n) => (n > 0 ? "+" + n : String(n));
  const svg = (id, cls = "") => `<svg class="${cls}"><use href="#${id}"/></svg>`;
  const flightById = (id) => (S.state?.flights || []).find((f) => f.id === id);
  const holeLink = (n) => `<a href="#" data-hole="${n}">${esc(t("hole_n", { n }))}</a>`;
  const flightLink = (f) => f ? `<a href="#" data-flight="${f.id}">${esc(f.name)}</a>` : "";

  let toastTimer;
  function toast(msg) {
    let el = $(".toast");
    if (!el) { el = document.createElement("div"); el.className = "toast"; document.body.appendChild(el); }
    el.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), 2600);
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.json) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
    if (opts.form) init.body = opts.form;
    const r = await fetch(path, init);
    if (!r.ok) { let detail = r.statusText; try { detail = (await r.json()).detail || detail; } catch {} throw new Error(detail); }
    return r.json();
  }

  // ---------- modal ----------
  function openModal(title, bodyHtml, onMount) {
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-bg"><div class="modal" role="dialog"><h2>${title}<button class="icon-btn close" aria-label="${t("close")}">✕</button></h2>${bodyHtml}</div></div>`;
    const close = () => { root.innerHTML = ""; };
    $(".modal-bg", root).addEventListener("click", (e) => { if (e.target.classList.contains("modal-bg")) close(); });
    $(".close", root).addEventListener("click", close);
    if (onMount) onMount(root, close);
    return close;
  }

  // ---------- header ----------
  function renderHeader() {
    const ev = S.state?.event || {};
    document.documentElement.lang = S.lang;
    $("#btn-lang").textContent = S.lang.toUpperCase();
    $$("[data-i18n]").forEach((el) => { el.innerHTML = t(el.dataset.i18n); });
    const brand = $("#brand");
    brand.innerHTML = ev.org_logo ? `<img src="${esc(ev.org_logo)}" alt="${esc(ev.org_name || "")}">` : `${svg("i-flag")}<span>${esc(ev.org_name || "Golf Day")}</span>`;
    const pill = $("#live-pill");
    pill.className = "pill " + (ev.live && S.online ? "live" : "paused");
    pill.textContent = ev.live && S.online ? t("live") : t("paused");
    $("#ev-title").textContent = ev.title || "Golf";
    $("#ev-sub").textContent = [fmtDate(ev.date), ev.course_name].filter(Boolean).join(" · ");
    $("#ev-updated").innerHTML = `${esc(t("updated_at", { t: fmtTime(S.state?.server_time) }))} <b>${esc(ev.status === "closed" ? t("scoring_closed") : t("scoring_open"))}</b>`;
    $("#btn-bell").classList.toggle("on", !!S.notif);
    $("#btn-theme").innerHTML = svg(effectiveTheme() === "dark" ? "i-moon" : "i-sun");
  }

  // partners carousel
  let partnerIdx = 0;
  function renderPartner(animate) {
    const list = (S.state?.event?.partners || []).filter((p) => p.name || p.logo);
    const box = $("#partner-logo");
    $("#partners").classList.toggle("hidden", list.length === 0);
    if (!list.length) return;
    const p = list[partnerIdx % list.length];
    const html = p.logo ? `<img src="${esc(p.logo)}" alt="${esc(p.name)}">` : `<span>${esc(p.name)}</span>`;
    if (animate) { box.classList.add("fade"); setTimeout(() => { box.innerHTML = html; box.classList.remove("fade"); }, 400); }
    else box.innerHTML = html;
  }
  setInterval(() => { partnerIdx++; renderPartner(true); }, 4000);

  function effectiveTheme() {
    if (S.theme === "auto") return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    return S.theme;
  }
  function applyTheme() { document.documentElement.dataset.theme = effectiveTheme(); }

  // ---------- leaderboard ----------
  function statusText(sc) {
    if (sc.status === "finished") return t("finished");
    if (sc.status === "playing") return t("playing", { h: sc.current_hole });
    return t("waiting");
  }
  function pointsOf(f) { return S.mode === "net" ? f.score.net_total : f.score.gross_total; }
  function rankOf(f) { return S.mode === "net" ? f.score.rank_net : f.score.rank_gross; }
  function tieOf(f) { return S.mode === "net" ? f.score.tie_net : f.score.tie_gross; }
  const playersLine = (f) => f.players.map((p) => esc(p.short)).join(" · ");

  function renderLeaderboard() {
    const st = S.state; const ev = st.event;
    const flights = [...st.flights].sort((a, b) => rankOf(a) - rankOf(b) || a.name.localeCompare(b.name));
    const done = flights.filter((f) => f.score.status === "finished").length;
    const started = flights.some((f) => f.score.status !== "not_started");
    let banner;
    if (flights.length && st.all_finished) banner = `<b>${esc(t("final_results"))}</b> ${esc(t("all_finished"))}`;
    else if (started) banner = esc(t("in_progress", { done, total: flights.length }));
    else banner = esc(t("not_started"));
    const prog = ev.program || [];
    const first = prog[0];
    const programCard = first ? `
      <div class="card program-card" id="program-card">
        <div class="program">
          <div class="ico">${svg("i-flag")}</div>
          <div class="grow"><b>${esc(t("program"))}</b> ${esc(t("from"))} ${esc(first.time)}<br>${esc(first[S.lang] || first.fr)}<div class="muted small">${esc(t("steps", { n: prog.length }))}</div></div>
          <span class="more">${svg("i-info")}</span>
        </div>
        <ol class="${S.programOpen ? "" : "hidden"}">${prog.map((p) => `<li><b>${esc(p.time)}</b><span>${esc(p[S.lang] || p.fr)}</span></li>`).join("")}</ol>
      </div>` : "";
    const top = flights.slice(0, 3);
    const podiumOrder = [top[1], top[0], top[2]];
    const podium = top.length ? `<div class="podium">${podiumOrder.map((f, i) => {
      if (!f) return "<div></div>";
      const pos = rankOf(f);
      return `<div class="step p${pos <= 3 ? pos : 3}" data-flight="${f.id}">
        <div class="laurel">${svg("i-laurel")}<div class="medal">${pos}</div></div>
        <div class="name">${esc(f.name)}</div>
        <div class="pts">${pointsOf(f)}<small>${esc(t("pts"))}</small></div>
        <div class="status">${esc(statusText(f.score))}</div>
      </div>`;
    }).join("")}</div>` : "";
    const rows = flights.slice(3).map((f) => renderRankRow(f)).join("");
    const expandedTop = top.filter((f) => S.expanded.has(f.id)).map((f) => `<div class="card" id="flight-${f.id}">${renderRankRow(f, true)}</div>`).join("");
    const anyTie = flights.some(tieOf);
    $("#view-leaderboard").innerHTML = `
      ${programCard}
      <div class="toolbar">
        <div class="seg"><button data-mode="net" class="${S.mode === "net" ? "on" : ""}">${esc(t("net"))}</button><button data-mode="gross" class="${S.mode === "gross" ? "on" : ""}">${esc(t("gross"))}</button></div>
        <div class="links"><a href="#" id="link-how">${svg("i-info")} ${esc(t("how"))}</a><a href="#" id="link-yourday">${svg("i-trophy")} ${esc(t("your_day"))}</a></div>
      </div>
      <div class="banner">${banner}</div>
      ${podium}${expandedTop}${rows}
      ${anyTie ? `<div class="muted small">${esc(t("tie_note"))}</div>` : ""}`;
    $("#program-card")?.addEventListener("click", () => { S.programOpen = !S.programOpen; renderLeaderboard(); });
    $$("[data-mode]").forEach((b) => b.addEventListener("click", () => { S.mode = b.dataset.mode; store.set("mode", S.mode); renderLeaderboard(); }));
    $("#link-how").addEventListener("click", (e) => { e.preventDefault(); openModal(esc(t("how")), `<p>${esc(ev["how_it_works_" + S.lang] || ev.how_it_works_fr)}</p>`); });
    $("#link-yourday").addEventListener("click", (e) => { e.preventDefault(); openYourDay(); });
    $$(".podium .step, .rank-row").forEach((el) => el.addEventListener("click", () => toggleFlight(+el.dataset.flight)));
  }

  function toggleFlight(id) {
    if (S.expanded.has(id)) S.expanded.delete(id); else S.expanded.add(id);
    renderLeaderboard();
    const el = document.getElementById("flight-" + id);
    if (el && S.expanded.has(id)) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderRankRow(f, withoutCard) {
    const open = S.expanded.has(f.id);
    const inner = `<div class="rank-row" data-flight="${f.id}">
        <div class="n">${rankOf(f)}${tieOf(f) ? "*" : ""}</div>
        <div class="grow"><div class="name">${esc(f.name)}</div><div class="players">${playersLine(f)}</div></div>
        <div class="right"><div class="pts">${pointsOf(f)}<small>${esc(t("pts"))}</small></div><div class="status">${esc(statusText(f.score))}</div></div>
      </div>${open ? renderFlightDetail(f) : ""}`;
    return withoutCard ? inner : `<div class="card" id="flight-${f.id}">${inner}</div>`;
  }

  function markFor(h) {
    if (h.strokes === null) return "";
    const d = h.strokes - h.par;
    const cls = h.strokes === 1 || d <= -2 ? "eagle" : d === -1 ? "birdie" : d === 1 ? "bogey" : d >= 2 ? "dbl" : "";
    return cls ? `<span class="mark ${cls}">${h.strokes}</span>` : h.strokes;
  }
  const ptsCell = (v) => (v === null || v === undefined ? "" : `<span class="mark p">${v}</span>`);

  function scoreTable(f, holes) {
    const labels = Object.fromEntries(f.players.map((p) => [p.id, p.label]));
    const row = (cls, th, cells) => `<tr class="${cls}"><th>${th}</th>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`;
    const sum = (k) => holes.reduce((a, h) => a + (h[k] || 0), 0);
    return `<div class="scorecard-wrap"><table class="sc">
      ${row("", esc(t("hole")), [...holes.map((h) => h.hole), esc(t("total"))])}
      ${row("", esc(t("par")), [...holes.map((h) => h.par), holes.reduce((a, h) => a + h.par, 0)])}
      ${row("strokes", esc(t("strokes_row")), [...holes.map((h) => "•".repeat(h.received)), ""])}
      ${row("coups", esc(t("coups")), [...holes.map(markFor), holes.some((h) => h.strokes !== null) ? sum("strokes") : ""])}
      ${row("drive", esc(t("drive")), [...holes.map((h) => esc(labels[h.drive_player_id] || "")), ""])}
      ${row("ptsrow", esc(t("net_points")), [...holes.map((h) => ptsCell(h.net_points)), sum("net_points")])}
      ${row("ptsrow", esc(t("gross_points")), [...holes.map((h) => ptsCell(h.gross_points)), sum("gross_points")])}
    </table></div>`;
  }

  function renderFlightDetail(f) {
    const sc = f.score;
    const players = f.players.map((p) => `<li>${esc(p.first_name)} ${esc(p.last_name)} ${p.handicap !== null ? `<span class="muted">(${fmtHcp(p.handicap)})</span>` : ""}</li>`).join("");
    const summary = [
      t("start_hole", { h: f.start_hole }), t("team_hcp", { h: f.team_hcp }),
      t("net_pts", { p: sc.net_total, r: sign(sc.net_pace) }), t("gross_pts", { p: sc.gross_total, r: sign(sc.gross_pace) }),
      sc.last_hole ? t("last_score", { h: sc.last_hole, t: fmtTime(sc.last_update) }) : t("no_scores"),
    ].map(esc).join(" · ");
    const min = S.state.event.min_drives_per_player || 0;
    const drives = f.players.map((p) => `${esc(p.short)} <b>${p.drives}</b>`).join(" · ");
    return `<div class="flight-detail">
      <ul>${players}</ul>
      <div class="summary">${summary}</div>
      ${scoreTable(f, sc.holes.slice(0, 9))}${scoreTable(f, sc.holes.slice(9))}
      <div class="muted">${esc(t("drives_kept", { n: min }))} : ${drives}</div>
    </div>`;
  }

  function openYourDay() {
    const f = flightById(S.flightId);
    if (!f) { openModal(esc(t("yd_title")), `<p>${esc(t("yd_no_flight"))}</p><button class="btn" id="yd-go">${esc(t("tab_card"))}</button>`, (root, close) => { $("#yd-go", root).addEventListener("click", () => { close(); setTab("card"); }); }); return; }
    const n = S.state.flights.length;
    const prizes = S.state.prizes.filter((p) => p.holder_flight_id === f.id);
    const html = `
      <p><b>${esc(f.name)}</b> · ${playersLine(f)}<br>${esc(statusText(f.score))} · ${esc(t("holes_played", { n: f.score.holes_played }))}</p>
      <p>${esc(t("yd_rank", { r: f.score.rank_net, n }))} · <b>${f.score.net_total} ${esc(t("pts"))}</b> (${sign(f.score.net_pace)})<br>
      ${esc(t("yd_rank_gross", { r: f.score.rank_gross, n }))} · <b>${f.score.gross_total} ${esc(t("pts"))}</b> (${sign(f.score.gross_pace)})</p>
      <p><b>${esc(t("yd_drives"))}</b><br>${f.players.map((p) => `${esc(p.short)} : ${p.drives}`).join(" · ")}</p>
      <p><b>${esc(t("yd_prizes"))}</b><br>${prizes.length ? prizes.map((p) => `${esc(p["title_" + S.lang])}${p.hole ? " · " + esc(t("hole_n", { n: p.hole })) : ""} : <span class="holder">${esc(p.holder_name)}</span>`).join("<br>") : esc(t("yd_none"))}</p>`;
    openModal(esc(t("yd_title")), html);
  }

  // ---------- prizes & course ----------
  function openPrizes() {
    const st = S.state;
    const prizes = st.prizes.map((p) => `<div class="prize"><div class="ico">${svg("i-trophy")}</div><div class="grow">
      <b>${esc(p["title_" + S.lang])}${p.hole ? ` · ${esc(t("hole_n", { n: p.hole }))}` : ""}</b>
      <div class="muted small">${esc(p["description_" + S.lang])}</div>
      ${p.claimable ? `<div>${esc(t("holder"))} : ${p.holder_name ? `<span class="holder">${esc(p.holder_name)}</span> ${p.holder_flight_id ? `(${esc(flightById(p.holder_flight_id)?.name || "")})` : ""}` : `<span class="muted">${esc(t("not_claimed"))}</span>`}</div>` : ""}
    </div></div>`).join("");
    const holes = st.holes;
    const tbl = (hs) => `<table class="course"><tr><th>${esc(t("hole"))}</th>${hs.map((h) => `<th>${h.number}</th>`).join("")}<th>${esc(t("total"))}</th></tr>
      <tr><th>${esc(t("par"))}</th>${hs.map((h) => `<td>${h.par}</td>`).join("")}<td><b>${hs.reduce((a, h) => a + h.par, 0)}</b></td></tr>
      <tr><th>${esc(t("si"))}</th>${hs.map((h) => `<td>${h.stroke_index}</td>`).join("")}<td></td></tr>
      ${hs.some((h) => h.length_m) ? `<tr><th>${esc(t("length"))}</th>${hs.map((h) => `<td>${h.length_m || ""}</td>`).join("")}<td>${hs.reduce((a, h) => a + (h.length_m || 0), 0)}</td></tr>` : ""}</table>`;
    openModal(esc(t("prizes_course")), `<h3>${esc(t("prizes"))}</h3><div style="display:grid;gap:14px">${prizes}</div>
      <h3>${esc(t("course"))}</h3><div class="muted small">${esc(st.event.course_name || "")}</div>
      <div class="scorecard-wrap">${tbl(holes.slice(0, 9))}</div><div class="scorecard-wrap">${tbl(holes.slice(9))}</div>`);
  }

  // ---------- scorecard (Carte) ----------
  const pending = new Map();
  function renderCard() {
    const view = $("#view-card");
    const f = flightById(S.flightId);
    if (!S.pin || !f) {
      view.innerHTML = `<div class="card">
        <div class="hero">${svg("i-scorecard")}<h2>${esc(t("enter_scores"))}</h2></div>
        <p>${esc(t("pin_help"))}</p>
        <form id="pin-form"><div class="field"><label for="pin">${esc(t("pin_label"))}</label><input id="pin" inputmode="numeric" autocomplete="one-time-code" required></div>
        <div class="error hidden" id="pin-error">${esc(t("bad_pin"))}</div>
        <button class="btn" type="submit">${esc(t("open_card"))}</button></form></div>`;
      $("#pin-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const pin = $("#pin").value.trim();
        try {
          const r = await api("/api/flight/login", { method: "POST", json: { pin } });
          S.pin = pin; S.flightId = r.flight_id; store.set("pin", pin); store.set("flightId", r.flight_id);
          renderCard(); toast(r.name);
        } catch { $("#pin-error").classList.remove("hidden"); }
      });
      return;
    }
    const closed = S.state.event.status === "closed";
    const sc = f.score;
    const order = playOrder(f.start_hole);
    const holes = order.map((n) => sc.holes[n - 1]);
    const min = S.state.event.min_drives_per_player || 0;
    const prizesByHole = {};
    S.state.prizes.filter((p) => p.claimable && p.hole).forEach((p) => (prizesByHole[p.hole] = prizesByHole[p.hole] || []).push(p));
    view.innerHTML = `
      <div class="card totals">
        <div class="row"><div class="grow"><h2>${esc(f.name)}</h2><div class="muted small">${f.players.map((p) => `${esc(p.short)} (${fmtHcp(p.handicap)})`).join(" · ")}</div>
        <div class="small">${esc(t("team_hcp", { h: f.team_hcp }))} · ${esc(t("start_hole", { h: f.start_hole }))}</div></div>
        <div class="right"><div class="pts">${sc.net_total}<small>${esc(t("net_short"))}</small></div><div class="muted small">${sc.gross_total} ${esc(t("gross_short"))} · ${esc(statusText(sc))}</div></div></div>
        <div class="muted small" style="margin-top:6px">${esc(t("drives_kept", { n: min }))} : ${f.players.map((p) => `<span class="${p.drives < min && sc.holes_played >= 18 - (min - p.drives) ? "error" : ""}">${esc(p.label)} ${p.drives}</span>`).join(" · ")}</div>
        ${closed ? `<div class="banner" style="margin-top:8px">${esc(t("closed_note"))}</div>` : ""}
        <div class="row" style="margin-top:8px"><button class="btn ghost small" id="btn-logout">${esc(t("change_flight"))}</button></div>
      </div>
      ${holes.map((h) => renderHoleCard(f, h, h.hole === sc.current_hole, closed, prizesByHole[h.hole] || [])).join("")}`;
    $("#btn-logout").addEventListener("click", () => { S.pin = ""; S.flightId = null; store.del("pin"); store.del("flightId"); renderCard(); });
    if (!closed) {
      $$("[data-step]").forEach((b) => b.addEventListener("click", () => stepStrokes(f, +b.dataset.hole, +b.dataset.step)));
      $$("[data-clear]").forEach((b) => b.addEventListener("click", () => saveScore(f, +b.dataset.clear, null, null)));
      $$("[data-drive]").forEach((b) => b.addEventListener("click", () => {
        const h = sc.holes[+b.dataset.hole - 1];
        const pid = +b.dataset.drive;
        saveScore(f, h.hole, h.strokes, h.drive_player_id === pid ? null : pid);
      }));
      $$("[data-claim]").forEach((b) => b.addEventListener("click", () => openClaim(f, +b.dataset.claim)));
    }
    const cur = $(".hole-card.current");
    if (cur && !S._cardScrolled) { S._cardScrolled = true; setTimeout(() => cur.scrollIntoView({ behavior: "smooth", block: "center" }), 100); }
  }
  function playOrder(start) { return Array.from({ length: 18 }, (_, i) => ((start - 1 + i) % 18) + 1); }

  function renderHoleCard(f, h, current, closed, prizes) {
    const val = h.strokes === null ? `<span class="val empty">–</span>` : `<span class="val">${h.strokes}</span>`;
    const drives = f.players.map((p) => `<button class="chip ${h.drive_player_id === p.id ? "on" : ""}" data-drive="${p.id}" data-hole="${h.hole}" ${closed ? "disabled" : ""}>${esc(p.label)} · ${esc(p.first_name)}</button>`).join("");
    const pts = h.strokes === null ? "" : `<div class="hole-pts"><span>${h.net_points} ${esc(t("pts_short"))} ${esc(t("net_short"))}</span><span>${h.gross_points} ${esc(t("pts_short"))} ${esc(t("gross_short"))}</span>${h.label && ["birdie", "eagle", "albatross", "hole_in_one"].includes(h.label) ? `<span class="badge">${esc(t("k_" + h.label))}</span>` : ""}</div>`;
    const prizeHtml = prizes.map((p) => `<div class="prize-box"><b>${esc(p["title_" + S.lang])}</b> · ${p.holder_name ? esc(t("holder_now", { name: p.holder_name })) : esc(t("nobody"))}
      ${closed ? "" : `<div style="margin-top:6px"><button class="btn small secondary" data-claim="${p.id}">${esc(t("claim_prize"))}</button></div>`}</div>`).join("");
    return `<div class="card hole-card ${current ? "current" : ""}" id="hole-${h.hole}">
      <div class="hole-head"><span class="num">${esc(t("hole"))} ${h.hole}</span><span class="muted">${esc(t("par"))} ${h.par} · ${esc(t("si"))} ${h.stroke_index}</span><span class="dots" title="${esc(t("received", { n: h.received }))}">${"•".repeat(h.received)}</span>${current ? `<span class="badge" style="margin-left:auto">${esc(t("current_hole"))}</span>` : ""}</div>
      <div class="row"><div class="stepper"><button data-step="-1" data-hole="${h.hole}" ${closed ? "disabled" : ""}>−</button>${val}<button data-step="1" data-hole="${h.hole}" ${closed ? "disabled" : ""}>+</button></div>
        ${h.strokes !== null && !closed ? `<button class="btn ghost small" data-clear="${h.hole}">${esc(t("clear"))}</button>` : ""}</div>
      ${pts}
      <div><div class="muted small" style="margin-bottom:6px">${esc(t("drive_of"))}</div><div class="chips">${drives}</div></div>
      ${prizeHtml}
    </div>`;
  }

  function stepStrokes(f, hole, delta) {
    const h = f.score.holes[hole - 1];
    const cur = h.strokes === null ? h.par : h.strokes;
    const next = Math.max(1, Math.min(20, h.strokes === null && delta < 0 ? h.par : cur + (h.strokes === null ? 0 : delta)));
    saveScore(f, hole, next, h.drive_player_id);
  }

  async function saveScore(f, hole, strokes, drive) {
    // optimistic local update, then persist
    const h = f.score.holes[hole - 1];
    h.strokes = strokes; h.drive_player_id = drive;
    if (strokes === null && drive === null) h.net_points = h.gross_points = null;
    renderCard();
    try {
      await api("/api/flight/score", { method: "PUT", json: { pin: S.pin, hole, strokes, drive_player_id: drive } });
      await refresh(true);
    } catch (e) {
      toast(e.message === "scoring_closed" ? t("closed_note") : t("save_error"));
      await refresh(true);
    }
  }

  function openClaim(f, prizeId) {
    const p = S.state.prizes.find((x) => x.id === prizeId);
    openModal(esc(p["title_" + S.lang]), `<p>${esc(t("claim_who"))}</p><div class="chips" id="claim-chips">${f.players.map((pl) => `<button class="chip" data-pid="${pl.id}">${esc(pl.first_name)} ${esc(pl.last_name)}</button>`).join("")}</div>`, (root, close) => {
      $$("[data-pid]", root).forEach((b) => b.addEventListener("click", async () => {
        try { await api(`/api/prizes/${prizeId}/claim`, { method: "POST", json: { pin: S.pin, player_id: +b.dataset.pid } }); close(); toast(t("prize_claimed")); refresh(true); }
        catch { toast(t("save_error")); }
      }));
    });
  }

  // ---------- rookies ----------
  function renderRookies() {
    const st = S.state; const ev = st.event;
    const stations = ev.rookie_stations || [];
    const rows = st.rookies.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.first_name)} ${esc(r.last_name)}</td>${stations.map((s) => `<td class="num">${r.stations[s] ?? "–"}</td>`).join("")}<td class="num"><b>${r.total}</b></td></tr>`).join("");
    $("#view-rookies").innerHTML = `
      <div class="card"><div class="row"><div class="ico" style="color:var(--primary)">${svg("i-pin")}</div><h2>${esc(t("rookies_title"))}</h2></div><p style="white-space:pre-line">${esc(ev["rookies_intro_" + S.lang] || ev.rookies_intro_fr)}</p></div>
      <div class="card"><h2>${esc(t("rookie_ranking"))}</h2>${st.rookies.length ? `<div class="scorecard-wrap"><table class="rk"><tr><th>#</th><th>${esc(t("player"))}</th>${stations.map((s) => `<th class="num">${esc(s)}</th>`).join("")}<th class="num">${esc(t("total"))}</th></tr>${rows}</table></div>` : `<p class="muted">${esc(t("no_rookies"))}</p>`}</div>`;
  }

  // ---------- feed ----------
  const REACTS = [["bravo", "i-clap"], ["fire", "i-fire"], ["laugh", "i-laugh"], ["shot", "i-shot"]];
  function postText(p) {
    const m = p.meta || {};
    const f = flightById(p.flight_id);
    const flight = flightLink(f) || esc(m.flight_name || "");
    const hole = p.hole ? holeLink(p.hole) : "";
    switch (p.kind) {
      case "birdie": case "eagle": case "albatross": case "hole_in_one": return t("t_" + p.kind, { hole, flight });
      case "prize": return t(p.hole ? "t_prize" : "t_prize_nohole", { title: esc(m["title_" + S.lang] || m.title_fr), hole, holder: esc(m.holder), flight });
      case "final": return t("t_final", { flight, points: m.points });
      case "info": return p.text === "welcome" ? esc(t("t_welcome")) : esc(p.text);
      default: return esc(p.text);
    }
  }
  function renderPost(p) {
    const sys = p.author_type === "system";
    const f = flightById(p.flight_id);
    const sub = [f ? f.name : "", fmtTime(p.created_at)].filter(Boolean).join(" · ");
    const initials = p.author_name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    const icon = ["birdie", "eagle", "albatross", "hole_in_one"].includes(p.kind) ? "i-bird" : ["prize", "final"].includes(p.kind) ? "i-trophy" : "i-info";
    let body;
    if (sys && p.kind !== "info") {
      const badge = `<span class="badge ${p.kind === "final" ? "final" : ""}">${esc(t("k_" + p.kind))}</span>`;
      body = icon === "i-bird" ? `<div class="with-icon">${svg(icon)}<div>${badge}<div>${postText(p)}</div></div></div>` : `${badge}<div>${postText(p)}</div>`;
      if (f) body += `<div class="players">${esc(f.name)} : ${playersLine(f)}</div>`;
    } else {
      body = `${p.text ? `<div>${postText(p)}</div>` : ""}${p.image ? `<img class="photo" src="/uploads/${esc(p.image)}" alt="" loading="lazy">` : ""}`;
    }
    const reacts = REACTS.map(([k, ic]) => `<button class="react ${p.my_reactions.includes(k) ? "on" : ""}" data-react="${k}" data-post="${p.id}"><span class="cnt">${svg(ic)} ${p.reactions[k] || 0}</span><span>${esc(t("r_" + k))}</span></button>`).join("");
    const replies = p.replies.map((r) => `<div class="reply"><b>${esc(r.author_name)} <span class="muted small">${fmtTime(r.created_at)}</span></b>${esc(r.text)}${r.image ? `<img class="photo" src="/uploads/${esc(r.image)}" alt="">` : ""}</div>`).join("");
    const replyForm = S.replyOpen === p.id ? `<form class="reply-form" data-reply="${p.id}">
        <input class="chip" name="author" placeholder="${esc(t("your_name"))}" value="${esc(S.authorName)}" required>
        <textarea name="text" placeholder="${esc(t("message"))}" required></textarea>
        <div class="row"><button class="btn small" type="submit">${esc(t("send"))}</button><button class="btn ghost small" type="button" data-cancel-reply>${esc(t("cancel"))}</button></div></form>` : "";
    return `<div class="card post ${sys ? "system" : ""}" id="post-${p.id}">
      <div class="post-head">${sys ? `<div class="avatar sys">${svg(icon)}</div>` : `<div class="avatar">${esc(initials)}</div>`}<div><b>${esc(sys ? t("sys_name") : p.author_name)}</b><span class="muted">${esc(sub)}</span></div>${p.mine ? `<button class="icon-btn" style="margin-left:auto" data-del="${p.id}" title="${esc(t("delete"))}">🗑</button>` : ""}</div>
      <div class="post-body">${body}</div>
      <div class="reactions">${reacts}</div>
      <div class="post-foot"><button class="reply-link" data-reply-open="${p.id}">${svg("i-chat")} ${esc(t("reply"))}</button>
      ${replies ? `<div class="replies">${replies}</div>` : ""}${replyForm}</div>
    </div>`;
  }

  function renderFeed() {
    const ev = S.state.event;
    const composer = S.composerOpen ? `<div class="card"><form id="post-form">
        <div class="field"><label>${esc(t("your_name"))}</label><input name="author" value="${esc(S.authorName)}" required maxlength="60"></div>
        <div class="field"><label>${esc(t("message"))}</label><textarea name="text" maxlength="2000"></textarea></div>
        <div class="field"><label>${esc(t("photo"))}</label><input type="file" name="image" accept="image/*"></div>
        <div class="row"><button class="btn" type="submit">${esc(t("publish"))}</button><button class="btn ghost" type="button" id="post-cancel">${esc(t("cancel"))}</button></div></form></div>`
      : `<div class="card composer" id="composer"><div class="ico">${svg("i-share")}</div><div><b>${esc(t("share_title"))}</b><span class="muted">${esc(t("share_sub"))}</span></div></div>`;
    $("#view-feed").innerHTML = `${ev.photographer_notice ? `<div class="notice">${svg("i-camera")}<span>${esc(t("photographer"))}</span></div>` : ""}${composer}${S.feed.map(renderPost).join("")}`;
    $("#composer")?.addEventListener("click", () => { S.composerOpen = true; renderFeed(); $("#post-form textarea")?.focus(); });
    $("#post-cancel")?.addEventListener("click", () => { S.composerOpen = false; renderFeed(); });
    $("#post-form")?.addEventListener("submit", (e) => submitPost(e, null));
    $$("[data-reply]").forEach((fm) => fm.addEventListener("submit", (e) => submitPost(e, +fm.dataset.reply)));
    $$("[data-cancel-reply]").forEach((b) => b.addEventListener("click", () => { S.replyOpen = null; renderFeed(); }));
    $$("[data-reply-open]").forEach((b) => b.addEventListener("click", () => { S.replyOpen = +b.dataset.replyOpen; renderFeed(); $(`[data-reply="${S.replyOpen}"] textarea`)?.focus(); }));
    $$("[data-react]").forEach((b) => b.addEventListener("click", () => react(+b.dataset.post, b.dataset.react)));
    $$("[data-del]").forEach((b) => b.addEventListener("click", async () => { if (!confirm(t("delete") + " ?")) return; try { await api(`/api/feed/${b.dataset.del}?client_id=${encodeURIComponent(clientId)}`, { method: "DELETE" }); await loadFeed(); } catch { toast(t("save_error")); } }));
  }

  async function submitPost(e, parentId) {
    e.preventDefault();
    const form = e.target;
    const fd = new FormData();
    const author = form.author.value.trim();
    if (!author) { toast(t("name_required")); return; }
    S.authorName = author; store.set("authorName", author);
    fd.append("author_name", author); fd.append("text", form.text.value.trim()); fd.append("client_id", clientId); fd.append("pin", S.pin || "");
    if (parentId) fd.append("parent_id", parentId);
    if (form.image && form.image.files[0]) fd.append("image", form.image.files[0]);
    const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
    try { await api("/api/feed", { method: "POST", form: fd }); S.composerOpen = false; S.replyOpen = null; toast(t("posted")); await loadFeed(); markFeedSeen(); }
    catch (err) { btn.disabled = false; toast(err.message === "name_required" ? t("name_required") : t("save_error")); }
  }

  async function react(postId, kind) {
    const p = S.feed.find((x) => x.id === postId); if (!p) return;
    const on = p.my_reactions.includes(kind);
    p.my_reactions = on ? p.my_reactions.filter((k) => k !== kind) : [...p.my_reactions, kind];
    p.reactions[kind] = (p.reactions[kind] || 0) + (on ? -1 : 1);
    renderFeed();
    try { await api(`/api/feed/${postId}/react`, { method: "POST", json: { kind, client_id: clientId } }); } catch { await loadFeed(); }
  }

  async function loadFeed() {
    const r = await api(`/api/feed?client_id=${encodeURIComponent(clientId)}`);
    S.feed = r.posts;
    if (S.tab === "feed") { renderFeed(); markFeedSeen(); }
    updateBadge();
  }
  function markFeedSeen() { S.lastSeenPost = S.state?.last_post_id || S.lastSeenPost; store.set("lastSeenPost", S.lastSeenPost); updateBadge(); }
  function updateBadge() {
    const n = S.feed.filter((p) => p.id > S.lastSeenPost).length;
    const b = $("#feed-badge"); b.textContent = n; b.classList.toggle("hidden", n === 0);
  }

  // ---------- requests (marshal / water) ----------
  function openRequest(kind) {
    const f = flightById(S.flightId);
    const cur = f?.score?.current_hole || f?.score?.last_hole || 1;
    const opts = Array.from({ length: 18 }, (_, i) => `<option value="${i + 1}" ${i + 1 === cur ? "selected" : ""}>${esc(t("hole"))} ${i + 1}</option>`).join("");
    openModal(`${svg(kind === "marshal" ? "i-marshal" : "i-bottle")} ${esc(t(kind + "_title"))}`, `<p class="muted">${esc(t(kind + "_sub"))}</p>
      <form id="req-form"><div class="field"><label>${esc(t("which_hole"))}</label><select name="hole">${opts}</select></div>
      <div class="field"><label>${esc(t("note"))}</label><input name="note" maxlength="300"></div>
      ${f ? `<div class="muted small">${esc(f.name)}</div>` : ""}
      <button class="btn" type="submit">${esc(t("send"))}</button></form>`, (root, close) => {
      $("#req-form", root).addEventListener("submit", async (e) => {
        e.preventDefault();
        try { await api("/api/requests", { method: "POST", json: { kind, pin: S.pin || null, hole: +e.target.hole.value, note: e.target.note.value } }); close(); toast(t("request_sent")); }
        catch { toast(t("request_error")); }
      });
    });
  }

  // ---------- notifications ----------
  async function toggleNotif() {
    if (S.notif) { S.notif = false; store.set("notif", false); renderHeader(); toast(t("notif_off")); return; }
    if (!("Notification" in window)) { toast(t("notif_denied")); return; }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast(t("notif_denied")); return; }
    S.notif = true; store.set("notif", true); renderHeader(); toast(t("notif_on"));
  }
  function notifyNew(n) {
    if (!S.notif || document.visibilityState === "visible" && S.tab === "feed") return;
    try { new Notification(S.state.event.title || "Golf Day", { body: t("new_posts", { n }), icon: "/static/icon.svg", tag: "golf-feed" }); } catch {}
  }

  // ---------- tabs & refresh ----------
  function setTab(tab) {
    S.tab = tab; location.hash = "#" + tab;
    $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    ["leaderboard", "card", "rookies", "feed"].forEach((v) => $("#view-" + v).classList.toggle("hidden", v !== tab));
    renderAll();
    if (tab === "feed") markFeedSeen();
    window.scrollTo(0, 0);
  }
  function renderAll() {
    if (!S.state) return;
    renderHeader();
    if (S.tab === "leaderboard") renderLeaderboard();
    if (S.tab === "card") renderCard();
    if (S.tab === "rookies") renderRookies();
    if (S.tab === "feed") renderFeed();
  }

  let lastPostId = null;
  async function refresh(silent) {
    try {
      const st = await api("/api/state");
      const prevOnline = S.online; S.online = true;
      S.state = st;
      if (S.flightId && !flightById(S.flightId)) { S.flightId = null; S.pin = ""; store.del("pin"); store.del("flightId"); }
      if (lastPostId !== null && st.last_post_id > lastPostId) { await loadFeed(); notifyNew(st.last_post_id - lastPostId); }
      else if (lastPostId === null) await loadFeed();
      lastPostId = st.last_post_id;
      renderAll();
      if (!prevOnline) toast(t("live"));
    } catch (e) {
      if (S.online) { S.online = false; if (!silent) toast(t("load_error")); renderHeader(); }
    }
  }

  // ---------- events ----------
  $$(".nav button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("#btn-lang").addEventListener("click", () => { S.lang = S.lang === "fr" ? "en" : "fr"; store.set("lang", S.lang); renderAll(); renderPartner(false); });
  $("#btn-theme").addEventListener("click", () => { S.theme = effectiveTheme() === "dark" ? "light" : "dark"; store.set("theme", S.theme); applyTheme(); renderHeader(); });
  $("#btn-bell").addEventListener("click", toggleNotif);
  $("#btn-prizes").addEventListener("click", openPrizes);
  $("#fab-marshal").addEventListener("click", () => openRequest("marshal"));
  $("#fab-water").addEventListener("click", () => openRequest("water"));
  document.body.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-flight], a[data-hole]");
    if (!a) return;
    e.preventDefault();
    if (a.dataset.flight) { const id = +a.dataset.flight; S.expanded.add(id); setTab("leaderboard"); setTimeout(() => document.getElementById("flight-" + id)?.scrollIntoView({ behavior: "smooth", block: "start" }), 50); }
    else openPrizes();
  });
  window.addEventListener("hashchange", () => { const tab = location.hash.slice(1); if (["leaderboard", "card", "rookies", "feed"].includes(tab) && tab !== S.tab) setTab(tab); });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(true); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (S.theme === "auto") { applyTheme(); renderHeader(); } });

  // ---------- boot ----------
  applyTheme();
  if (!["leaderboard", "card", "rookies", "feed"].includes(S.tab)) S.tab = "leaderboard";
  $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  ["leaderboard", "card", "rookies", "feed"].forEach((v) => $("#view-" + v).classList.toggle("hidden", v !== S.tab));
  refresh(false).then(() => renderPartner(false));
  setInterval(() => refresh(true), 15000);
})();
