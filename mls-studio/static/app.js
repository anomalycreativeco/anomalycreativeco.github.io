/* MLS Studio — page logic. Talks only to the local server at /api. */
(function () {
  const $ = (id) => document.getElementById(id);
  const state = {
    cfg: null, models: [], transforms: [], source: null, balance: null,
    fio: { account_id: "", workspace_id: "", project_id: "", folder_id: "", path: [], root: null, trail: [] },
    picked: null, jobs: [], polling: null,
  };

  const APP_VERSION = "1.6";
  // theme: the hub passes ?theme=light|dark when it embeds the page; standalone it follows the system unless toggled
  const THEMES = ["auto", "light", "dark"], THEME_LABEL = { auto: "◐", light: "☀", dark: "☾" }, THEME_TITLE = { auto: "Theme: follows the system", light: "Theme: light", dark: "Theme: dark" };
  const params = new URLSearchParams(location.search);
  function applyTheme(t) { if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t); $("btn-theme").textContent = THEME_LABEL[t]; $("btn-theme").title = THEME_TITLE[t]; }
  let theme = "auto"; try { theme = localStorage.getItem("mls.theme") || "auto"; } catch (e) {}
  if (params.get("theme") === "dark" || params.get("theme") === "light") theme = params.get("theme");
  if (params.get("embedded") === "1") document.body.classList.add("embedded");
  window.addEventListener("message", (e) => { if (e.data && (e.data.theme === "dark" || e.data.theme === "light")) applyTheme(e.data.theme); });
  applyTheme(theme);
  $("btn-theme").onclick = () => { theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]; try { localStorage.setItem("mls.theme", theme); } catch (e) {} applyTheme(theme); };
  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({ error: r.status === 404
      ? "The server is running an older version than this page. Stop it with Ctrl-C and start it again, then reload."
      : "Bad response from the local server (HTTP " + r.status + ")" }));
    if (!r.ok || data.error) throw new Error(data.error || ("HTTP " + r.status));
    return data;
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function banner(html, kind) { $("banner").innerHTML = html ? `<div class="${kind || "flag"}">${html}</div>` : ""; }
  function setDot(id, ok, text) { const el = $(id); el.innerHTML = `<i class="dot ${ok === null ? "" : ok ? "ok" : "bad"}"></i>${text}`; }

  // ---------------------------------------------------------------- config --
  async function loadConfig() {
    state.cfg = await api("GET", "/api/config");
    const c = state.cfg;
    $("mls-limit").value = c.mls_limit_kb || 3999;
    $("appver").textContent = "v" + (c.version || APP_VERSION);
    $("s-redirect").textContent = c.frameio_redirect_uri;
    if (c.dry_run) banner("<b>Dry run.</b> AutoHDR and Frame.io are simulated; nothing is uploaded and no credits are spent.", "flag");
    if (c.version !== APP_VERSION) { banner("<b>Restart needed.</b> This page is newer than the server behind it. In the terminal press Ctrl-C, run the start command again, then reload this page.", "flag"); return; }
    const ahReady = c.dry_run || (c.autohdr_client_id && c.secrets.autohdr_client_secret);
    const fioReady = c.dry_run || (c.frameio_client_id && c.secrets.frameio_client_secret && (c.frameio_mode === "s2s" || c.secrets.frameio_connected));
    setDot("status-autohdr", ahReady ? null : false, "AutoHDR");
    setDot("status-frameio", fioReady ? null : false, "Frame.io");
    const slackReady = c.secrets.slack_webhook_url || c.secrets.slack_bot_token;
    setDot("status-slack", slackReady ? (c.slack_notify === false ? null : true) : false, "Slack" + (slackReady && c.slack_notify === false ? " · off" : ""));
    if (ahReady) loadModels().catch((e) => { setDot("status-autohdr", false, "AutoHDR"); banner("AutoHDR: " + esc(e.message) + ' — check <a href="#" id="open-settings-link">Settings</a>.'); });
    else banner('AutoHDR and Frame.io keys are not set up yet. Open <a href="#" id="open-settings-link">Settings</a> to add them (one time).');
    if (fioReady) loadAccounts().catch((e) => { setDot("status-frameio", false, "Frame.io"); $("fio-hint").textContent = e.message; });
    if (c.last_source && !state.source) { $("src-manual").value = c.last_source; }
    document.body.addEventListener("click", (e) => { if (e.target.id === "open-settings-link") { e.preventDefault(); openSettings(); } });
  }

  async function loadModels() {
    const d = await api("GET", "/api/autohdr/models");
    state.models = d.models; state.transforms = d.transforms;
    const me = await api("GET", "/api/autohdr/me").catch(() => null);
    state.balance = me ? me.credit_balance : null;
    setDot("status-autohdr", true, "AutoHDR" + (state.balance != null ? " · " + state.balance + " cr" : ""));
    fillModels("indoor-model", "indoor", state.cfg.default_indoor_model_id);
    fillModels("outdoor-model", "outdoor", state.cfg.default_outdoor_model_id);
    estimate();
  }
  function fillModels(selId, slot, def) {
    const sel = $(selId);
    sel.innerHTML = '<option value="">Account default</option>';
    const groups = { House: [], Creator: [] };
    state.models.filter((m) => !m.variant || m.variant === slot).forEach((m) => (m.type === "custom" ? groups.Creator : groups.House).push(m));
    for (const g of ["House", "Creator"]) {
      if (!groups[g].length) continue;
      const og = document.createElement("optgroup"); og.label = g === "House" ? "House looks" : "Creator looks (bill per photo)";
      groups[g].forEach((m) => {
        const o = document.createElement("option");
        o.value = m.id; o.textContent = m.name + (m.description ? " — " + m.description : "") + (m.type === "custom" ? " · " + m.style_credit_cost + " cr" : "");
        og.appendChild(o);
      });
      sel.appendChild(og);
    }
    if (def) sel.value = String(def);
  }

  // ---------------------------------------------------------------- source --
  async function pick() {
    const r = await api("POST", "/api/pick-folder");
    if (r.cancelled) return;
    await scan(r.path);
  }
  async function scan(path) {
    try {
      const info = await api("POST", "/api/scan", { path });
      state.source = info;
      $("src-path").textContent = info.path; $("src-manual").value = info.path;
      if (!$("shoot-name").value) $("shoot-name").value = info.name;
      const chips = info.files.slice(0, 14).map((f) => `<span class="chip">${esc(f.name)}</span>`).join("") + (info.count > 14 ? `<span class="chip">+${info.count - 14} more</span>` : "");
      $("src-info").innerHTML = `<div class="stat"><b>${info.count}</b> photos · ${info.total_mb} MB${info.raw_count ? ` · <b>${info.raw_count}</b> RAW` : ""}${Object.keys(info.skipped || {}).length ? ` · <span style="color:var(--warn)">not photos, left out: ${esc(Object.entries(info.skipped).map(([k, v]) => `${v} ${k}`).join(", "))}</span>` : ""}${info.subfolders.length ? ` · subfolders ignored: ${esc(info.subfolders.join(", "))}` : ""}</div><div class="chipset" style="margin-top:8px">${chips}</div>`;
      if (info.raw_count && info.raw_count < info.count) $("src-info").innerHTML += `<p class="flag" style="margin-top:10px"><b>Mixed folder.</b> RAW and finished files together. AutoHDR takes both as camera files; the MLS resize only works on finished JPEG/PNG/HEIC/TIFF.</p>`;
      if (!info.count) $("src-info").innerHTML = `<div class="err">No photos found in that folder.</div>`;
      document.querySelector('input[name=kind][value=raw]').checked = true;
      estimate();
    } catch (e) { $("src-info").innerHTML = `<div class="err">${esc(e.message)}</div>`; state.source = null; estimate(); }
  }

  // --------------------------------------------------------------- frameio --
  const FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 6.5A2.5 2.5 0 0 1 5.5 4h4.2c.6 0 1.2.2 1.6.7l1.2 1.3h6A2.5 2.5 0 0 1 21 8.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5v-11Z"/></svg>';
  const FILE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="m4 16 4.5-4.5 3.5 3.5 2.5-2.5L20 17"/><circle cx="15.5" cy="9.5" r="1.5"/></svg>';
  try { state.fio.view = localStorage.getItem("mls.fioView") || "grid"; } catch (e) { state.fio.view = "grid"; }
  state.fio.cache = {};

  async function loadAccounts() {
    const d = await api("GET", "/api/frameio/accounts");
    const sel = $("fio-account");
    sel.innerHTML = d.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.display_name || a.name || a.id)}</option>`).join("");
    setDot("status-frameio", true, "Frame.io");
    const last = state.cfg.frameio_last_folder;
    if (last && d.accounts.some((a) => a.id === last.account_id)) sel.value = last.account_id;
    state.fio.account_id = sel.value;
    await loadWorkspaces(last);
  }
  async function loadWorkspaces(last) {
    const d = await api("GET", `/api/frameio/workspaces?account_id=${encodeURIComponent(state.fio.account_id)}`);
    const sel = $("fio-workspace");
    sel.innerHTML = d.workspaces.map((w) => `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join("");
    if (last && d.workspaces.some((w) => w.id === last.workspace_id)) sel.value = last.workspace_id;
    state.fio.workspace_id = sel.value;
    await loadProjects(last);
  }
  async function loadProjects(last) {
    const d = await api("GET", `/api/frameio/projects?account_id=${encodeURIComponent(state.fio.account_id)}&workspace_id=${encodeURIComponent(state.fio.workspace_id)}`);
    state.fio.projects = d.projects; state.fio.trail = []; state.fio.project_id = ""; state.fio.cache = {};
    renderPicker();
    if (last && last.project_id && d.projects.some((p) => p.id === last.project_id)) await openTrail(last.trail || [], last.project_id);
  }
  async function loadChildren(id) {
    if (!state.fio.cache[id]) {
      state.fio.cache[id] = { loading: true, folders: [], files: [] };
      renderPicker();
      try { state.fio.cache[id] = await api("GET", `/api/frameio/children?account_id=${encodeURIComponent(state.fio.account_id)}&folder_id=${encodeURIComponent(id)}`); }
      catch (e) { state.fio.cache[id] = { folders: [], files: [], error: e.message }; }
    }
    return state.fio.cache[id];
  }
  // trail = [{id, name}] from the project's root folder down to the folder we are in
  async function openTrail(trail, projectId) {
    state.fio.trail = trail.slice(); state.fio.project_id = projectId;
    renderPicker();
    for (const t of trail) await loadChildren(t.id);
    renderPicker();
    if (trail.length) usePicked();
  }
  function openProject(p) { return openTrail([{ id: p.root_folder_id, name: p.name }], p.id); }
  function openFolder(parentLevel, item) { return openTrail(state.fio.trail.slice(0, parentLevel + 1).concat([{ id: item.id, name: item.name }]), state.fio.project_id); }

  function renderPicker() {
    const f = state.fio;
    $("fio-projects").innerHTML = (f.projects || []).length
      ? f.projects.map((p) => `<div class="item ${f.project_id === p.id ? "sel" : ""}" data-project="${esc(p.id)}"><span>${esc(p.name)}</span></div>`).join("")
      : '<div class="item empty">No projects in this workspace</div>';
    document.querySelectorAll("#fio-view button").forEach((b) => b.classList.toggle("on", b.dataset.view === f.view));
    if (!f.trail.length) { $("fio-crumbs").innerHTML = '<span class="small">Choose a project</span>'; $("fio-body").innerHTML = '<div class="fempty">Pick a project on the left, or paste a next.frame.io link above.</div>'; return; }
    $("fio-crumbs").innerHTML = f.trail.map((t, i) => i < f.trail.length - 1 ? `<a data-i="${i}">${esc(t.name)}</a><span>›</span>` : `<b>${esc(t.name)}</b>`).join("");
    if (f.view === "columns") renderCols(); else renderGrid();
  }
  function renderGrid() {
    const cur = state.fio.trail[state.fio.trail.length - 1];
    const c = state.fio.cache[cur.id];
    const level = state.fio.trail.length - 1;
    if (!c || c.loading) { $("fio-body").innerHTML = '<div class="fempty">Loading…</div>'; return; }
    if (c.error) { $("fio-body").innerHTML = `<div class="fempty">${esc(c.error)}</div>`; return; }
    if (!c.folders.length && !c.files.length) { $("fio-body").innerHTML = '<div class="fempty">Empty folder. Deliver here and the shoot folder is created inside it.</div>'; return; }
    const folders = c.folders.map((d) => `<div class="tile" data-level="${level}" data-id="${esc(d.id)}" title="${esc(d.name)}"><div class="thumb">${FOLDER_SVG}</div><div class="name">${esc(d.name)}</div></div>`).join("");
    const files = (c.files || []).map((x) => `<div class="tile file" title="${esc(x.name)}"><div class="thumb">${x.thumb ? `<img src="${esc(x.thumb)}" alt="" loading="lazy">` : FILE_SVG}</div><div class="name">${esc(x.name)}</div></div>`).join("");
    $("fio-body").innerHTML = `<div class="tiles">${folders}${files}</div>`;
  }
  function renderCols() {
    const f = state.fio;
    const html = f.trail.map((t, i) => {
      const c = f.cache[t.id] || { loading: true, folders: [], files: [] };
      const selId = (f.trail[i + 1] || {}).id;
      let body;
      if (c.loading) body = '<div class="item empty">Loading…</div>';
      else if (c.error) body = `<div class="item empty">${esc(c.error)}</div>`;
      else if (!c.folders.length) body = '<div class="item empty">No subfolders. Deliver here.</div>';
      else body = c.folders.map((d) => `<div class="item ${selId === d.id ? "sel" : ""}" data-level="${i}" data-id="${esc(d.id)}"><span>${esc(d.name)}</span><span class="chev">›</span></div>`).join("");
      const foot = c.loading ? "" : `<div class="colfoot">${c.folders.length} folder${c.folders.length === 1 ? "" : "s"}${c.file_count ? ` · ${c.file_count} file${c.file_count === 1 ? "" : "s"}` : ""}</div>`;
      return `<div class="col"><div class="colhead" title="${esc(t.name)}">${esc(t.name)}</div><div class="items">${body}</div>${foot}</div>`;
    }).join("");
    $("fio-body").innerHTML = `<div class="cols">${html}</div>`;
    const el = $("fio-body").firstElementChild; el.scrollLeft = el.scrollWidth;
  }
  function usePicked(obj) {
    if (!obj && !state.fio.trail.length) return;
    const cur = state.fio.trail[state.fio.trail.length - 1];
    state.picked = obj || { account_id: state.fio.account_id, workspace_id: state.fio.workspace_id, project_id: state.fio.project_id, folder_id: cur.id, path: state.fio.trail.map((t) => t.name).join(" / "), trail: state.fio.trail.slice() };
    $("fio-picked").hidden = false;
    $("fio-picked").textContent = "Output folder: " + state.picked.path;
    estimate();
  }
  async function useLink() {
    const url = $("fio-link").value.trim();
    if (!url) return;
    $("fio-hint").textContent = "Looking up that link…";
    try {
      const r = await api("POST", "/api/frameio/resolve", { url });
      $("fio-hint").textContent = "";
      if (r.account_id !== state.fio.account_id) { $("fio-account").value = r.account_id; state.fio.account_id = r.account_id; await loadWorkspaces(r); }
      else if (r.workspace_id && r.workspace_id !== state.fio.workspace_id) { $("fio-workspace").value = r.workspace_id; state.fio.workspace_id = r.workspace_id; await loadProjects(r); }
      else await openTrail(r.trail, r.project_id);
      if (!state.picked || state.picked.folder_id !== r.folder_id) usePicked(r);
    } catch (e) { $("fio-hint").textContent = e.message; }
  }

  // ------------------------------------------------------------- estimate --
  function opts() {
    return {
      do_autohdr: $("do-autohdr").checked, do_resize: $("do-resize").checked, do_frameio: $("do-frameio").checked,
      kind: document.querySelector("input[name=kind]:checked").value,
      indoor_model_id: $("indoor-model").value ? +$("indoor-model").value : null,
      outdoor_model_id: $("outdoor-model").value ? +$("outdoor-model").value : null,
      enhancements: { grass: $("enh-grass").checked, declutter: $("enh-declutter").checked, fireplace: $("enh-fireplace").checked, tv_replacement: $("enh-tv").checked, tv_mode: $("enh-tv-mode").value },
      reedit_prompt: $("reedit").value.trim(), mls_limit_kb: +$("mls-limit").value || 3999,
      indoor_model_name: ($("indoor-model").selectedOptions[0] || {}).textContent.split(" — ")[0] || null,
      outdoor_model_name: ($("outdoor-model").selectedOptions[0] || {}).textContent.split(" — ")[0] || null,
    };
    if (!o.indoor_model_id) o.indoor_model_name = null;
    if (!o.outdoor_model_id) o.outdoor_model_name = null;
  }
  function estimate() {
    const o = opts();
    $("step-edit").classList.toggle("off", !o.do_autohdr);
    $("fio-area").style.display = o.do_frameio ? "" : "none";
    const parts = [];
    let ok = !!(state.source && state.source.count);
    if (!state.source) parts.push("Choose a folder to begin.");
    else {
      parts.push(`<b>${state.source.count}</b> files`);
      if (o.do_autohdr) {
        let per = 1; const costs = [];
        for (const id of [o.indoor_model_id, o.outdoor_model_id]) { const m = state.models.find((x) => x.id === id); if (m && m.type === "custom") costs.push(m.style_credit_cost); }
        if (o.reedit_prompt) per += 1;
        const hi = per + (costs.length ? Math.max(...costs) : 0);
        parts.push(o.kind === "raw" ? "AutoHDR HDR edit" : "AutoHDR enhance");
        parts.push(`≈ <b>${per === hi ? per : per + "–" + hi}</b> credit${hi > 1 ? "s" : ""} per finished photo${o.kind === "raw" ? " (brackets merge, so photos &lt; files)" : ""}` + (state.balance != null ? ` · balance ${state.balance}` : ""));
      }
      if (o.do_resize) parts.push(`MLS set under ${o.mls_limit_kb} KB`);
      if (o.do_frameio) { parts.push(state.picked ? `→ Frame.io: ${esc(state.picked.path)}` : "<span style='color:var(--warn)'>pick a Frame.io folder</span>"); if (!state.picked) ok = false; }
      if (!(o.do_autohdr || o.do_resize || o.do_frameio)) { parts.push("<span style='color:var(--warn)'>turn on at least one stage</span>"); ok = false; }
    }
    $("est").innerHTML = parts.join(" · ");
    $("btn-run").disabled = !ok;
  }

  async function run() {
    $("btn-run").disabled = true;
    try {
      const o = opts();
      const job = await api("POST", "/api/jobs", { source: state.source.path, name: $("shoot-name").value.trim(), options: o, frameio: state.picked ? Object.assign({}, state.picked, { create_shoot_folder: $("fio-shootfolder").checked }) : null });
      banner(`Started <b>${esc(job.name)}</b>. You can close this tab; the job keeps running while the server is up.`, "picked");
      await loadJobs();
    } catch (e) { banner(esc(e.message), "err"); }
    estimate();
  }

  // ------------------------------------------------------------------ jobs --
  async function loadJobs() {
    const d = await api("GET", "/api/jobs");
    state.jobs = d.jobs; renderJobs();
    const active = d.jobs.some((j) => j.status === "running" || j.status === "queued");
    const wanted = active ? 3000 : 10000;
    if (state.pollingEvery !== wanted) { if (state.polling) clearInterval(state.polling); state.polling = setInterval(loadJobs, wanted); state.pollingEvery = wanted; }
  }
  const STEP_LABEL = { notify: "Posting to Slack", create: "Creating shoot", upload: "Uploading to AutoHDR", commit: "Committing", process: "AutoHDR processing", reedit: "Re-editing", download: "Downloading high-res", resize: "Resizing for MLS", frameio: "Uploading to Frame.io", done: "Done" };
  function renderJobs() {
    const el = $("jobs");
    if (!state.jobs.length) { el.innerHTML = '<p class="small">No jobs yet.</p>'; return; }
    el.innerHTML = state.jobs.map((j) => {
      const p = j.progress || {}; const pct = p.total ? Math.round((p.current / p.total) * 100) : 0;
      const running = j.status === "running";
      const r = j.results || {};
      const links = [];
      if (r.highres_dir) links.push(`<a href="#" data-open="${esc(r.highres_dir)}">Open High Res</a>`);
      if (r.mls_dir) links.push(`<a href="#" data-open="${esc(r.mls_dir)}">Open MLS</a>`);
      if (r.frameio_shoot_url) links.push(`<a href="${esc(r.frameio_shoot_url)}" target="_blank" rel="noopener">Frame.io folder ↗</a>`);
      for (const k in r.frameio_links || {}) if (r.frameio_links[k] && !r.frameio_shoot_url) links.push(`<a href="${esc(r.frameio_links[k])}" target="_blank" rel="noopener">Frame.io › ${esc(k)} ↗</a>`);
      if (r.photoshoot_id) links.push(`<span class="small">AutoHDR shoot #${esc(r.photoshoot_id)}</span>`);
      if (r.slack_posted) links.push(`<span class="small">Slack: posted</span>`);
      if (r.slack_error) links.push(`<span class="small" style="color:var(--warn)">Slack failed: ${esc(r.slack_error)}</span>`);
      const failedResize = (r.resize_failed || []).length ? `<div class="flag"><b>${r.resize_failed.length} could not get under the limit:</b> ${esc(r.resize_failed.join(", "))}</div>` : "";
      const btns = [];
      if (running) btns.push(`<button class="btn sm" data-act="cancel" data-id="${j.id}">Cancel</button>`);
      if (["failed", "interrupted", "cancelled"].includes(j.status)) btns.push(`<button class="btn sm primary" data-act="resume" data-id="${j.id}">Resume</button>`);
      if (!running) btns.push(`<button class="btn sm danger" data-act="delete" data-id="${j.id}">Remove</button>`);
      return `<div class="job">
        <div class="head"><div><span class="title">${esc(j.name)}</span> <span class="pill ${esc(j.status)}">${esc(j.status)}</span></div><div class="row">${btns.join("")}</div></div>
        <div class="sub">${j.files.length} files · ${esc(j.source)} · ${new Date(j.created * 1000).toLocaleString()}</div>
        ${running || j.status === "queued" ? `<div class="bar ${p.total ? "" : "indet"}"><i style="width:${pct}%"></i></div><div class="label">${esc(STEP_LABEL[j.step] || j.step || "Starting")}${p.label ? " · " + esc(p.label) : ""}${p.total ? ` · ${p.current}/${p.total}` : ""}</div>` : ""}
        ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
        ${failedResize}
        ${links.length ? `<div class="links">${links.join("")}</div>` : ""}
        <details ${running ? "open" : ""}><summary>Log</summary><pre class="log">${esc((j.log || []).slice(-40).join("\n"))}</pre></details>
      </div>`;
    }).join("");
  }
  $("jobs").addEventListener("click", async (e) => {
    const a = e.target.closest("[data-act]"); const o = e.target.closest("[data-open]");
    if (o) { e.preventDefault(); api("POST", "/api/open", { path: o.dataset.open }); }
    if (a) { try { await api("POST", `/api/jobs/${a.dataset.id}/${a.dataset.act}`); await loadJobs(); } catch (err) { banner(esc(err.message), "err"); } }
  });

  // -------------------------------------------------------------- settings --
  function openSettings() {
    const c = state.cfg;
    $("s-ah-id").value = c.autohdr_client_id || ""; $("s-ah-secret").value = ""; $("s-ah-secret").placeholder = c.secrets.autohdr_client_secret ? "•••••••• (saved in Keychain)" : "";
    document.querySelector(`input[name=fio-mode][value=${c.frameio_mode || "user"}]`).checked = true;
    $("s-fio-id").value = c.frameio_client_id || ""; $("s-fio-secret").value = ""; $("s-fio-secret").placeholder = c.secrets.frameio_client_secret ? "•••••••• (saved in Keychain)" : "";
    $("s-limit").value = c.mls_limit_kb; $("s-hr-name").value = c.highres_folder_name; $("s-mls-name").value = c.mls_folder_name;
    $("s-slack-webhook").value = ""; $("s-slack-webhook").placeholder = c.secrets.slack_webhook_url ? "•••••••• (saved in Keychain)" : "https://hooks.slack.com/services/…";
    $("s-slack-token").value = ""; $("s-slack-token").placeholder = c.secrets.slack_bot_token ? "•••••••• (saved in Keychain)" : "xoxb-… with chat:write";
    $("s-slack-channel").value = c.slack_channel || "#general"; $("s-slack-notify").checked = c.slack_notify !== false; $("s-slack-result").textContent = "";
    $("s-fio-result").textContent = c.secrets.frameio_connected ? "Connected as a Frame.io user." : "";
    $("settings-msg").innerHTML = ""; $("s-ah-result").textContent = "";
    $("settings").showModal();
  }
  async function saveSettings(silent) {
    const body = {
      autohdr_client_id: $("s-ah-id").value.trim(), autohdr_client_secret: $("s-ah-secret").value.trim(),
      frameio_mode: document.querySelector("input[name=fio-mode]:checked").value, frameio_client_id: $("s-fio-id").value.trim(), frameio_client_secret: $("s-fio-secret").value.trim(),
      mls_limit_kb: +$("s-limit").value || 3999, highres_folder_name: $("s-hr-name").value.trim() || "High Res", mls_folder_name: $("s-mls-name").value.trim() || "MLS",
      slack_webhook_url: $("s-slack-webhook").value.trim(), slack_bot_token: $("s-slack-token").value.trim(), slack_channel: $("s-slack-channel").value.trim() || "#general", slack_notify: $("s-slack-notify").checked,
    };
    state.cfg = await api("POST", "/api/config", body);
    $("s-ah-secret").value = ""; $("s-fio-secret").value = ""; $("s-slack-webhook").value = ""; $("s-slack-token").value = "";
    if (!silent) { $("settings").close(); await loadConfig(); }
  }
  $("btn-settings").onclick = openSettings;
  $("s-close").onclick = () => $("settings").close();
  $("s-save").onclick = () => saveSettings().catch((e) => ($("settings-msg").innerHTML = `<div class="err">${esc(e.message)}</div>`));
  $("s-ah-test").onclick = async () => { $("s-ah-result").textContent = "Testing…"; try { await saveSettings(true); const me = await api("GET", "/api/autohdr/me"); $("s-ah-result").innerHTML = `<span class="ok">✓ ${esc(me.email)} · ${me.credit_balance} credits · scopes: ${esc((me.scopes || []).join(" "))}</span>`; } catch (e) { $("s-ah-result").innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`; } };
  $("s-fio-test").onclick = async () => { $("s-fio-result").textContent = "Testing…"; try { await saveSettings(true); const d = await api("GET", "/api/frameio/me"); $("s-fio-result").innerHTML = `<span class="ok">✓ ${esc(d.me.name || d.me.email || "signed in")} · ${d.accounts.length} account${d.accounts.length === 1 ? "" : "s"}</span>`; } catch (e) { $("s-fio-result").innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`; } };
  $("s-fio-connect").onclick = async () => { try { await saveSettings(true); if (document.querySelector("input[name=fio-mode]:checked").value !== "user") { $("s-fio-result").textContent = "Connect is for the OAuth Web App mode. Server-to-Server needs no sign-in — press Test."; return; } window.location.href = "/api/frameio/connect"; } catch (e) { $("s-fio-result").textContent = e.message; } };
  $("s-slack-test").onclick = async () => { $("s-slack-result").textContent = "Sending…"; try { await saveSettings(true); const d = await api("POST", "/api/slack/test"); $("s-slack-result").innerHTML = `<span class="ok">✓ posted via ${esc(d.via)} — check #general</span>`; } catch (e) { $("s-slack-result").innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`; } };
  $("s-slack-clear").onclick = async () => { await api("POST", "/api/config", { slack_clear: true }); state.cfg = await api("GET", "/api/config"); openSettings(); $("s-slack-result").textContent = "Removed."; };
  $("s-fio-disconnect").onclick = async () => { await api("POST", "/api/config", { frameio_disconnect: true }); $("s-fio-result").textContent = "Disconnected."; state.cfg = await api("GET", "/api/config"); };

  // ---------------------------------------------------------------- wiring --
  $("btn-pick").onclick = () => pick().catch((e) => banner(esc(e.message), "err"));
  $("btn-scan").onclick = () => scan($("src-manual").value.trim());
  $("src-manual").addEventListener("keydown", (e) => { if (e.key === "Enter") scan($("src-manual").value.trim()); });
  $("btn-run").onclick = run;
  ["do-autohdr", "do-resize", "do-frameio", "indoor-model", "outdoor-model", "reedit", "mls-limit", "enh-grass", "enh-declutter", "enh-fireplace", "enh-tv"].forEach((id) => $(id).addEventListener("change", estimate));
  document.querySelectorAll("input[name=kind]").forEach((r) => r.addEventListener("change", estimate));
  $("reedit").addEventListener("input", estimate);
  $("fio-account").onchange = async () => { state.fio.account_id = $("fio-account").value; state.picked = null; $("fio-picked").hidden = true; await loadWorkspaces(); estimate(); };
  $("fio-workspace").onchange = async () => { state.fio.workspace_id = $("fio-workspace").value; state.picked = null; $("fio-picked").hidden = true; await loadProjects(); estimate(); };
  $("fio-projects").onclick = (e) => { const d = e.target.closest("[data-project]"); if (!d) return; const p = state.fio.projects.find((x) => x.id === d.dataset.project); if (p) openProject(p); };
  $("fio-crumbs").onclick = (e) => { const a = e.target.closest("a[data-i]"); if (!a) return; openTrail(state.fio.trail.slice(0, +a.dataset.i + 1), state.fio.project_id); };
  $("fio-body").onclick = (e) => { const d = e.target.closest("[data-id][data-level]"); if (!d) return; const c = state.fio.cache[state.fio.trail[+d.dataset.level].id]; const it = c && c.folders.find((x) => x.id === d.dataset.id); if (it) openFolder(+d.dataset.level, it); };
  $("fio-view").onclick = (e) => { const b = e.target.closest("button[data-view]"); if (!b) return; state.fio.view = b.dataset.view; try { localStorage.setItem("mls.fioView", state.fio.view); } catch (err) {} renderPicker(); };
  $("fio-link-go").onclick = useLink;
  $("fio-link").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); useLink(); } });

  loadConfig().then(() => { if (location.search.includes("frameio=connected")) { history.replaceState({}, "", "/"); banner("Frame.io connected.", "picked"); } });
  loadJobs();
})();
