"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const BBOX_RE = /images\/bbox_(\d+)_(\d+)_(\d+)_(\d+)\.(?:jpe?g|png)/gi;

  const state = {
    list: { page: 1, size: 20, status: "", q: "", pages: 1, total: 0, keys: [] },
    subs: { page: 1, size: 20, status: "", q: "", pages: 1 },
    job: null,
    mode: "render",
    savedMarkdown: "",
    dirty: false,
    currentHash: "",
    suppressHashChange: false,
  };

  // ------------------------------------------------------------------ helpers
  async function api(path, options = {}) {
    const res = await fetch(path, options);
    const type = res.headers.get("content-type") || "";
    const body = type.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = body && typeof body === "object" ? (body.detail || (body.error && body.error.message)) : body;
      throw new Error(detail || res.statusText);
    }
    return body;
  }

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else node[k] = v;
    }
    for (const child of children) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function fmtDate(value) {
    if (!value) return "";
    const d = new Date(value);
    return isNaN(d) ? value : d.toLocaleString();
  }

  function parseHash() {
    const raw = location.hash.replace(/^#/, "") || "/";
    const [path, query = ""] = raw.split("?");
    return { path, params: new URLSearchParams(query) };
  }

  function buildHash(base, values) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(values)) if (v !== "" && v != null) qs.set(k, v);
    return base + "?" + qs.toString();
  }

  function listHash(overrides = {}) {
    const { page, size, status, q } = state.list;
    return buildHash("#/jobs", { page, size, status, q, ...overrides });
  }

  function subsHash(overrides = {}) {
    const { page, size, status, q } = state.subs;
    return buildHash("#/submissions", { page, size, status, q, ...overrides });
  }

  function jobHash(key) { return "#/job/" + encodeURIComponent(key); }

  function adjustLabel(a) {
    if (!a) return "";
    const parts = [];
    if (a.rotation) parts.push(`rotated ${Number(a.rotation).toFixed(1)}°`);
    if (a.threshold != null) parts.push(`B&W ≥${a.threshold}`);
    else if (a.grayscale) parts.push("grayscale");
    return parts.join(", ");
  }

  function isTyping(target) {
    return !!target && (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(target.tagName) || target.isContentEditable);
  }

  function showFatal(err) {
    console.error(err);
    alert(err.message || String(err));
  }

  // ------------------------------------------------------------------ routing
  const SECTIONS = ["list-view", "job-view", "new-view", "submissions-view"];

  function showSection(id) {
    for (const s of SECTIONS) $(s).hidden = s !== id;
    $("nav-jobs").classList.toggle("active", id === "list-view" || id === "job-view");
    $("nav-new").classList.toggle("active", id === "new-view");
    $("nav-submissions").classList.toggle("active", id === "submissions-view");
    if (id !== "submissions-view") stopSubmissionPolling();
  }

  async function route() {
    const { path, params } = parseHash();
    const m = path.match(/^\/job\/(.+)$/);
    if (m) {
      await showJob(decodeURIComponent(m[1]));
    } else if (path === "/new") {
      await showNew(params);
    } else if (path === "/submissions") {
      state.subs.page = Math.max(1, parseInt(params.get("page"), 10) || 1);
      state.subs.size = parseInt(params.get("size"), 10) || state.subs.size;
      state.subs.status = params.get("status") || "";
      state.subs.q = params.get("q") || "";
      await showSubmissions();
    } else {
      state.list.page = Math.max(1, parseInt(params.get("page"), 10) || 1);
      state.list.size = parseInt(params.get("size"), 10) || state.list.size;
      state.list.status = params.get("status") || "";
      state.list.q = params.get("q") || "";
      await showList();
    }
    state.currentHash = location.hash;
  }

  window.addEventListener("hashchange", () => {
    if (state.suppressHashChange) { state.suppressHashChange = false; return; }
    if (state.dirty && !confirm("You have unsaved edits. Discard them?")) {
      state.suppressHashChange = true;
      location.hash = state.currentHash;
      return;
    }
    state.dirty = false;
    route().catch(showFatal);
  });

  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  function ensureOption(select, value) {
    if (![...select.options].some((o) => o.value === String(value))) {
      select.append(el("option", { value, textContent: value }));
    }
    select.value = String(value);
  }

  // ------------------------------------------------------------------ list view
  async function fetchPage(page) {
    const qs = new URLSearchParams({ page, page_size: state.list.size });
    if (state.list.status) qs.set("status", state.list.status);
    if (state.list.q) qs.set("q", state.list.q);
    return api("/api/jobs?" + qs.toString());
  }

  async function showList() {
    showSection("list-view");
    $("status-filter").value = state.list.status;
    $("search").value = state.list.q;
    ensureOption($("page-size"), state.list.size);

    const data = await fetchPage(state.list.page);
    state.list.page = data.page;
    state.list.pages = data.pages;
    state.list.total = data.total;
    state.list.keys = data.items.map((it) => it.key);

    const rows = $("job-rows");
    rows.replaceChildren(...data.items.map(renderRow));
    $("empty-list").hidden = data.total > 0;
    $("page-info").textContent = `Page ${data.page} of ${data.pages}`;
    $("total-info").textContent = `${data.total} job${data.total === 1 ? "" : "s"}`;
    $("first-page").disabled = $("prev-page").disabled = data.page <= 1;
    $("next-page").disabled = $("last-page").disabled = data.page >= data.pages;
    document.title = "OCR Artifact Viewer";
  }

  function renderRow(it) {
    const badges = [el("span", { class: `badge ${it.status}` }, it.status)];
    if (it.has_edits) badges.push(el("span", { class: "badge edited" }, "edited"));
    const size = it.width && it.height ? `${it.width}×${it.height}` : "";
    const row = el("tr", { dataset: { key: it.key } },
      el("td", {}, el("a", { href: jobHash(it.key) }, el("code", {}, it.job_id))),
      el("td", {}, ...badges),
      el("td", {}, fmtDate(it.created_at)),
      el("td", {}, it.original_filename || ""),
      el("td", {}, size),
      el("td", {}, it.region_count ?? ""),
      el("td", { class: "snippet", title: it.error || it.snippet || "" }, it.error || it.snippet || ""),
    );
    row.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      location.hash = jobHash(it.key);
    });
    return row;
  }

  function bindPager(prefix, ids, hashFn, pagesOf) {
    const [first, prev, next, last] = ids;
    $(first).onclick = () => { location.hash = hashFn({ page: 1 }); };
    $(prev).onclick = () => { location.hash = hashFn({ page: prefix.page - 1 }); };
    $(next).onclick = () => { location.hash = hashFn({ page: prefix.page + 1 }); };
    $(last).onclick = () => { location.hash = hashFn({ page: pagesOf() }); };
  }

  function bindSearch(input, hashFn) {
    let timer = null;
    input.oninput = (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => { location.hash = hashFn({ q: e.target.value.trim(), page: 1 }); }, 300);
    };
  }

  function bindListControls() {
    bindPager(state.list, ["first-page", "prev-page", "next-page", "last-page"], listHash, () => state.list.pages);
    $("status-filter").onchange = (e) => { location.hash = listHash({ status: e.target.value, page: 1 }); };
    $("page-size").onchange = (e) => { location.hash = listHash({ size: e.target.value, page: 1 }); };
    bindSearch($("search"), listHash);
    $("refresh").onclick = () => showList().catch(showFatal);
  }

  // ------------------------------------------------------------------ zoomable / pannable image
  function createZoom(viewportId, stageId, levelId) {
    return {
      scale: 1, tx: 0, ty: 0, min: 0.02, max: 32, natW: 0, natH: 0,
      get vp() { return $(viewportId); },
      apply() {
        const stage = $(stageId);
        stage.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
        stage.style.setProperty("--inv", String(1 / this.scale));
        $(levelId).textContent = Math.round(this.scale * 100) + "%";
        const ds = this.vp.dataset;
        ds.scale = this.scale.toFixed(4);
        ds.tx = this.tx.toFixed(1);
        ds.ty = this.ty.toFixed(1);
      },
      zoomAt(factor, cx, cy) {
        const next = Math.min(this.max, Math.max(this.min, this.scale * factor));
        const k = next / this.scale;
        this.tx = cx - (cx - this.tx) * k;
        this.ty = cy - (cy - this.ty) * k;
        this.scale = next;
        this.apply();
      },
      zoomCenter(factor) {
        const r = this.vp.getBoundingClientRect();
        this.zoomAt(factor, r.width / 2, r.height / 2);
      },
      fit() {
        if (!this.natW) return;
        const r = this.vp.getBoundingClientRect();
        this.scale = Math.min(r.width / this.natW, r.height / this.natH) * 0.98 || 1;
        this.tx = (r.width - this.natW * this.scale) / 2;
        this.ty = (r.height - this.natH * this.scale) / 2;
        this.apply();
      },
      actual() {
        const r = this.vp.getBoundingClientRect();
        this.scale = 1;
        this.tx = (r.width - this.natW) / 2;
        this.ty = Math.min(0, (r.height - this.natH) / 2);
        this.apply();
      },
      centerOn(px, py) {
        const r = this.vp.getBoundingClientRect();
        this.tx = r.width / 2 - px * this.scale;
        this.ty = r.height / 2 - py * this.scale;
        this.apply();
      },
    };
  }

  // Wheel zoom at the cursor, drag-to-pan when canPan(event) is true, keyboard +/-/0/1.
  function bindZoom(z, { canPan, dblclick = true }) {
    const vp = z.vp;
    vp.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = vp.getBoundingClientRect();
      const delta = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
      z.zoomAt(Math.exp(-delta * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    vp.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); }); // no autoscroll

    let drag = null;
    vp.addEventListener("pointerdown", (e) => {
      if (!canPan(e)) return;
      if (e.button !== 0) e.preventDefault();
      drag = { x: e.clientX, y: e.clientY, tx: z.tx, ty: z.ty, id: e.pointerId };
      vp.setPointerCapture(e.pointerId);
      vp.classList.add("dragging");
      vp.focus();
    });
    vp.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      z.tx = drag.tx + (e.clientX - drag.x);
      z.ty = drag.ty + (e.clientY - drag.y);
      z.apply();
    });
    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      vp.classList.remove("dragging");
      if (vp.hasPointerCapture(e.pointerId)) vp.releasePointerCapture(e.pointerId);
    };
    vp.addEventListener("pointerup", endDrag);
    vp.addEventListener("pointercancel", endDrag);
    if (dblclick) {
      vp.addEventListener("dblclick", (e) => {
        const r = vp.getBoundingClientRect();
        z.zoomAt(e.shiftKey ? 0.5 : 2, e.clientX - r.left, e.clientY - r.top);
      });
    }
    vp.addEventListener("keydown", (e) => {
      if (e.key === "+" || e.key === "=") z.zoomCenter(1.25);
      else if (e.key === "-" || e.key === "_") z.zoomCenter(0.8);
      else if (e.key === "0") z.fit();
      else if (e.key === "1") z.actual();
      else return;
      e.preventDefault();
    });
  }

  const viewer = createZoom("viewport", "stage", "zoom-level");

  function bindViewer() {
    bindZoom(viewer, { canPan: (e) => e.button === 0 });
    $("zoom-in").onclick = () => viewer.zoomCenter(1.25);
    $("zoom-out").onclick = () => viewer.zoomCenter(0.8);
    $("zoom-fit").onclick = () => viewer.fit();
    $("zoom-actual").onclick = () => viewer.actual();
    $("show-regions").onchange = (e) => $("overlay").classList.toggle("hidden", !e.target.checked);
    window.addEventListener("resize", () => {
      if (!$("job-view").hidden) viewer.fit();
      if (!$("new-view").hidden && cropZoom.natW) cropZoom.fit();
    });

    $("page-image").addEventListener("load", () => {
      const img = $("page-image");
      viewer.natW = img.naturalWidth;
      viewer.natH = img.naturalHeight;
      $("stage").style.width = viewer.natW + "px";
      $("stage").style.height = viewer.natH + "px";
      viewer.fit();
    });
  }

  function parseRegions(markdown) {
    const seen = new Map();
    for (const m of markdown.matchAll(BBOX_RE)) {
      const name = m[0].slice("images/".length);
      if (!seen.has(name)) seen.set(name, m.slice(1, 5).map(Number));
    }
    return seen;
  }

  // Region coordinates are normalized to the image that was OCR'd. When the original (uncropped) image is kept,
  // that image is the crop `region_frame` inside the displayed picture.
  function regionGeometry() {
    const job = state.job || {};
    const W = job.width || viewer.natW || 1;
    const H = job.height || viewer.natH || 1;
    const [fx, fy, fx2, fy2] = job.region_frame || [0, 0, W, H];
    return { W, H, fx, fy, fw: fx2 - fx, fh: fy2 - fy, scale: job.bbox_scale || 1000 };
  }

  function drawRegions(markdown) {
    const overlay = $("overlay");
    const g = regionGeometry();
    const nodes = [];
    if (state.job && state.job.region_frame) {
      const frame = el("div", { class: "crop-frame", title: "Region sent for OCR" });
      frame.style.left = (g.fx / g.W) * 100 + "%";
      frame.style.top = (g.fy / g.H) * 100 + "%";
      frame.style.width = (g.fw / g.W) * 100 + "%";
      frame.style.height = (g.fh / g.H) * 100 + "%";
      nodes.push(frame);
    }
    for (const [name, [l, t, r, b]] of parseRegions(markdown)) {
      const box = el("div", { class: "region", title: name, dataset: { name } });
      box.style.left = ((g.fx + (l / g.scale) * g.fw) / g.W) * 100 + "%";
      box.style.top = ((g.fy + (t / g.scale) * g.fh) / g.H) * 100 + "%";
      box.style.width = ((Math.max(0, r - l) / g.scale) * g.fw / g.W) * 100 + "%";
      box.style.height = ((Math.max(0, b - t) / g.scale) * g.fh / g.H) * 100 + "%";
      nodes.push(box);
    }
    overlay.replaceChildren(...nodes);
  }

  function highlightRegion(name) {
    const g = regionGeometry();
    for (const node of $("overlay").querySelectorAll(".region")) {
      node.classList.toggle("active", node.dataset.name === name);
    }
    for (const img of $("rendered").querySelectorAll("img")) {
      img.classList.toggle("active", (img.getAttribute("src") || "").endsWith("/" + name));
    }
    const coords = parseRegions("images/" + name).get(name);
    if (coords && viewer.natW) {
      const [l, t, r, b] = coords;
      const px = g.fx + ((l + r) / 2 / g.scale) * g.fw;
      const py = g.fy + ((t + b) / 2 / g.scale) * g.fh;
      viewer.centerOn((px / g.W) * viewer.natW, (py / g.H) * viewer.natH);
    }
  }

  // ------------------------------------------------------------------ job view
  async function showJob(key) {
    showSection("job-view");
    const job = await api("/api/jobs/" + encodeURIComponent(key));
    state.job = job;
    state.savedMarkdown = job.markdown;
    state.dirty = false;

    $("job-title").textContent = job.job_id;
    $("job-status").textContent = job.status;
    $("job-status").className = "badge " + job.status;
    $("job-file").textContent = [
      job.original_filename,
      job.width && `${job.width}×${job.height}`,
      adjustLabel(job.adjustments),
      job.crop && `OCR region ${job.crop.box.join(",")} · kept ${job.persisted_image} image`,
      job.resubmit_of && `resubmission of ${job.resubmit_of.slice(0, 8)}`,
    ].filter(Boolean).join(" · ");
    $("job-resubmit").hidden = !(sub.config && sub.config.submissions_enabled);
    document.title = `${job.job_id} – OCR Artifact Viewer`;

    const messages = [];
    if (job.meta && job.meta.error) {
      messages.push(el("div", { class: "message error" }, `${job.meta.error.code}: ${job.meta.error.message}`));
    }
    for (const w of (job.meta && job.meta.result && job.meta.result.warnings) || []) {
      messages.push(el("div", { class: "message warn" }, w));
    }
    $("job-messages").replaceChildren(...messages);

    const img = $("page-image");
    if (job.image_url) {
      img.src = job.image_url;
    } else {
      img.removeAttribute("src");
    }
    $("editor").value = job.markdown;
    updateSaveState(job.has_edits ? "Showing saved edits" : "");
    $("revert-md").disabled = !job.has_edits;
    updateNeighbors();
    drawRegions(job.markdown);
    await setMode(state.mode, true);
  }

  async function renderMarkdown(markdown) {
    const { html } = await api("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown, key: state.job.key }),
    });
    const target = $("rendered");
    target.innerHTML = html; // sanitized server-side
    for (const node of target.querySelectorAll(".math")) {
      if (!window.katex) break;
      try {
        window.katex.render(node.textContent, node, {
          displayMode: node.classList.contains("block"), throwOnError: false,
        });
        node.classList.add("katex-rendered");
      } catch (err) { console.warn(err); }
    }
    for (const image of target.querySelectorAll("img")) {
      const src = image.getAttribute("src") || "";
      const m = src.match(/\/(bbox_\d+_\d+_\d+_\d+\.(?:jpe?g|png))$/i);
      if (m) {
        image.title = "Show region on page";
        image.addEventListener("click", () => highlightRegion(m[1]));
      }
    }
    drawRegions(markdown);
  }

  async function setMode(mode, force = false) {
    if (mode === state.mode && !force) return;
    state.mode = mode;
    $("mode-render").classList.toggle("active", mode === "render");
    $("mode-edit").classList.toggle("active", mode === "edit");
    $("rendered").hidden = mode !== "render";
    $("editor").hidden = mode !== "edit";
    if (mode === "render") {
      await renderMarkdown($("editor").value);
    } else {
      $("editor").focus();
    }
  }

  function updateSaveState(text) {
    $("save-state").textContent = text;
    $("save-md").disabled = !state.dirty;
  }

  async function save() {
    const markdown = $("editor").value;
    await api(`/api/jobs/${encodeURIComponent(state.job.key)}/markdown`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown }),
    });
    state.savedMarkdown = markdown;
    state.dirty = false;
    state.job.has_edits = true;
    $("revert-md").disabled = false;
    updateSaveState("Saved");
    if (state.mode === "render") await renderMarkdown(markdown);
  }

  async function revert() {
    if (!confirm("Discard all edits and restore the original OCR output?")) return;
    const res = await api(`/api/jobs/${encodeURIComponent(state.job.key)}/markdown`, { method: "DELETE" });
    $("editor").value = res.markdown;
    state.savedMarkdown = res.markdown;
    state.dirty = false;
    state.job.has_edits = false;
    $("revert-md").disabled = true;
    updateSaveState("Reverted to original");
    if (state.mode === "render") await renderMarkdown(res.markdown);
  }

  async function neighborKey(direction) {
    const keys = state.list.keys;
    const idx = keys.indexOf(state.job.key);
    if (idx >= 0 && keys[idx + direction] !== undefined) return keys[idx + direction];
    const page = state.list.page + direction;
    if (idx < 0 || page < 1 || page > state.list.pages) return null;
    const data = await fetchPage(page);
    if (!data.items.length) return null;
    state.list.page = data.page;
    state.list.keys = data.items.map((it) => it.key);
    return direction > 0 ? data.items[0].key : data.items[data.items.length - 1].key;
  }

  function updateNeighbors() {
    const keys = state.list.keys;
    const idx = keys.indexOf(state.job.key);
    $("prev-job").disabled = idx < 0 || (idx === 0 && state.list.page <= 1);
    $("next-job").disabled = idx < 0 || (idx === keys.length - 1 && state.list.page >= state.list.pages);
  }

  function bindJobControls() {
    $("mode-render").onclick = () => setMode("render").catch(showFatal);
    $("mode-edit").onclick = () => setMode("edit").catch(showFatal);
    $("save-md").onclick = () => save().catch(showFatal);
    $("revert-md").onclick = () => revert().catch(showFatal);
    $("back-link").onclick = (e) => { e.preventDefault(); location.hash = listHash(); };
    $("job-resubmit").onclick = () => { location.hash = "#/new?job=" + encodeURIComponent(state.job.key); };
    $("editor").addEventListener("input", () => {
      state.dirty = $("editor").value !== state.savedMarkdown;
      updateSaveState(state.dirty ? "Unsaved changes" : "");
    });
    $("editor").addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save().catch(showFatal); }
    });
    for (const [id, dir] of [["prev-job", -1], ["next-job", 1]]) {
      $(id).onclick = async () => {
        const key = await neighborKey(dir);
        if (key) location.hash = jobHash(key);
      };
    }
  }

  // ------------------------------------------------------------------ new OCR: import, adjust, box, submit
  const SERVER_KEY = "ocrViewer.serverUrl";
  const defaultAdjust = () => ({ rotation: 0, grayscale: false, threshold: null });
  const sub = {
    config: null, items: [], selected: -1, nextId: 1, info: null, timer: null, drag: null,
    tool: "box", space: false, previewTimer: null, lastThreshold: 128, shownId: null,
  };
  const cropZoom = createZoom("crop-viewport", "crop-stage", "crop-zoom-level");

  function serverUrl() { return $("server-url").value.trim(); }

  async function checkServer() {
    const status = $("server-status");
    status.textContent = "Checking…";
    status.className = "muted";
    try {
      const { info, server_url } = await api("/api/server/info?url=" + encodeURIComponent(serverUrl()));
      sub.info = info;
      const q = info.queue || {};
      status.textContent = `Connected to ${server_url} · ${info.handler.display_name || info.handler.name}` +
        ` · max ${info.limits.max_pixels.toLocaleString()} px · buffer ${q.buffered}/${q.max_buffer}` +
        ` · active ${q.active}/${q.max_concurrency}`;
      status.className = "ok";
    } catch (err) {
      sub.info = null;
      status.textContent = "Not reachable: " + err.message;
      status.className = "error-text";
    }
    updateCropInfo();
  }

  function currentItem() { return sub.items[sub.selected] || null; }

  // Rotation in degrees clockwise, 0.1° steps, normalized to (-180, 180] like the backend.
  function normRotation(value) {
    let r = Number(value);
    if (!Number.isFinite(r)) r = 0;
    r = Math.round(r * 10) / 10;
    r = ((r % 360) + 360) % 360;
    if (r > 180) r -= 360;
    r = Math.round(r * 10) / 10;
    return r === 0 ? 0 : r;
  }

  function previewUrl(item) {
    if (!item || !item.source) return "";
    const a = item.adjust;
    const qs = new URLSearchParams();
    if (a.rotation) qs.set("rotation", a.rotation.toFixed(1));
    if (a.grayscale) qs.set("grayscale", "true");
    if (a.threshold != null) qs.set("threshold", String(a.threshold));
    const query = qs.toString();
    return `/api/sources/${item.source.source_id}/preview` + (query ? "?" + query : "");
  }

  function newItem(name, extra = {}) {
    return {
      id: sub.nextId++, name, source: null, adjust: defaultAdjust(), box: null, natW: 0, natH: 0,
      renderedRotation: 0, error: "", status: "uploading", resubmitOf: null, removed: false, ...extra,
    };
  }

  async function uploadSource(item, file) {
    const form = new FormData();
    form.append("image", file, file.name);
    try {
      item.source = await api("/api/sources", { method: "POST", body: form });
      item.status = "ready";
      if (item.removed) { deleteSource(item); return; }
    } catch (err) {
      item.status = "error";
      item.error = err.message;
    }
    renderImportList();
    if (item === currentItem()) selectItem(sub.selected);
  }

  function deleteSource(item) {
    if (item.source) { // the server keeps sources that a submission still references
      fetch(`/api/sources/${item.source.source_id}`, { method: "DELETE" }).catch(() => {});
    }
  }

  function addFiles(files) {
    const added = files.map((file) => {
      const item = newItem(file.name);
      sub.items.push(item);
      uploadSource(item, file);
      return item;
    });
    if (added.length) sub.selected = sub.items.indexOf(added[0]);
    renderImportList();
    selectItem(sub.selected);
  }

  function addDraft(draft) {
    const item = newItem(draft.source.filename, {
      source: draft.source,
      status: "ready",
      resubmitOf: draft.resubmit_of || null,
      adjust: { ...defaultAdjust(), ...(draft.adjustments || {}) },
      box: draft.box ? draft.box.slice() : null,
    });
    item.adjust.rotation = normRotation(item.adjust.rotation);
    item.renderedRotation = item.adjust.rotation;
    sub.items.push(item);
    sub.selected = sub.items.length - 1;
    const persist = document.querySelector(`input[name=persist][value="${draft.persist}"]`);
    if (persist) persist.checked = true;
    $("ocr-prompt").value = draft.prompt || "";
    if (draft.server_url && draft.server_url !== serverUrl()) {
      $("server-url").value = draft.server_url;
      checkServer();
    }
    renderImportList();
    selectItem(sub.selected);
  }

  function boxLabel(item) {
    if (!item.box) return "whole image";
    const [x1, y1, x2, y2] = item.box;
    return `box ${x2 - x1}×${y2 - y1}`;
  }

  function itemDetail(item) {
    if (item.status === "uploading") return "uploading…";
    if (item.status === "error") return "";
    const parts = [];
    if (item.natW) parts.push(`${item.natW}×${item.natH}`);
    parts.push(boxLabel(item));
    const adj = adjustLabel(item.adjust);
    if (adj) parts.push(adj);
    if (item.resubmitOf) parts.push(`resubmission of ${item.resubmitOf.slice(0, 8)}`);
    return parts.join(" · ");
  }

  function renderImportList() {
    $("import-list").replaceChildren(...sub.items.map((item, idx) => {
      const remove = el("button", { type: "button", class: "remove", title: "Remove", textContent: "×" });
      remove.addEventListener("click", (e) => { e.stopPropagation(); removeItem(idx); });
      const li = el("li", { class: idx === sub.selected ? "selected" : "", dataset: { id: item.id } },
        el("span", { class: "name", title: item.name }, item.name),
        remove,
        el("span", { class: "muted small detail" }, itemDetail(item)));
      if (item.error) li.append(el("div", { class: "err" }, item.error));
      li.addEventListener("click", () => selectItem(idx));
      return li;
    }));
    $("submit-ocr").disabled = !sub.items.some((it) => it.source);
  }

  function removeItem(idx) {
    const [item] = sub.items.splice(idx, 1);
    item.removed = true;
    deleteSource(item);
    if (sub.selected >= sub.items.length) sub.selected = sub.items.length - 1;
    renderImportList();
    selectItem(sub.selected);
  }

  function showStage(item, refit) {
    const img = $("crop-image");
    item.natW = img.naturalWidth;
    item.natH = img.naturalHeight;
    item.renderedRotation = item.adjust.rotation;
    cropZoom.natW = item.natW;
    cropZoom.natH = item.natH;
    $("crop-stage").style.width = item.natW + "px";
    $("crop-stage").style.height = item.natH + "px";
    $("crop-stage").hidden = false;
    sub.shownId = item.id;
    updateCropInfo(); // may change the toolbar height, so before fitting
    if (refit) cropZoom.fit();
  }

  function selectItem(idx) {
    sub.selected = idx;
    const item = currentItem();
    $("crop-name").textContent = item ? item.name : "No image selected";
    $("crop-resubmit").hidden = !(item && item.resubmitOf);
    if (item && item.resubmitOf) $("crop-resubmit").textContent = "Resubmission of " + item.resubmitOf.slice(0, 8);
    $("crop-empty").hidden = !!item;
    for (const li of $("import-list").children) li.classList.toggle("selected", Number(li.dataset.id) === (item && item.id));
    const img = $("crop-image");
    const url = previewUrl(item);
    const loading = $("crop-loading");
    if (!url) {
      $("crop-stage").hidden = true;
      sub.shownId = null;
      loading.textContent = "Uploading…";
      loading.hidden = !(item && item.status === "uploading");
    } else if (img.getAttribute("src") !== url) {
      if (sub.shownId !== item.id) $("crop-stage").hidden = true;
      loading.textContent = "Rendering…";
      loading.hidden = false;
      img.src = url;
    } else if (img.complete && img.naturalWidth) {
      loading.hidden = true;
      img.style.transform = "";
      showStage(item, sub.shownId !== item.id);
    }
    syncAdjustControls();
    drawCropBox();
    updateCropInfo();
  }

  function syncAdjustControls() {
    const item = currentItem();
    const ready = !!(item && item.source);
    $("adjust-panel").disabled = !ready;
    const a = ready ? item.adjust : defaultAdjust();
    const bw = a.threshold != null;
    if (bw) sub.lastThreshold = a.threshold;
    $("adj-bw").checked = bw;
    $("adj-grayscale").checked = a.grayscale || bw;
    $("adj-grayscale").disabled = bw;
    $("adj-threshold").disabled = !bw;
    $("adj-threshold").value = String(sub.lastThreshold);
    $("adj-threshold-value").textContent = String(sub.lastThreshold);
    $("adj-rotation").value = a.rotation.toFixed(1);
    $("adj-rotation-slider").value = String(a.rotation);
  }

  function setAdjust(changes, delay = 250) {
    const item = currentItem();
    if (!item || !item.source) return;
    const before = item.adjust.rotation;
    item.adjust = { ...item.adjust, ...changes };
    item.adjust.rotation = normRotation(item.adjust.rotation);
    if (item.adjust.threshold != null) {
      item.adjust.threshold = Math.max(0, Math.min(255, Math.round(item.adjust.threshold)));
    }
    if (item.adjust.threshold == null && changes.threshold === null && !("grayscale" in changes)) {
      item.adjust.grayscale = false;
    }
    if (item.adjust.rotation !== before) {
      item.box = null; // the box refers to the previous geometry
      if (sub.shownId === item.id) {
        $("crop-image").style.transform = `rotate(${item.adjust.rotation - item.renderedRotation}deg)`;
      }
    }
    syncAdjustControls();
    drawCropBox();
    updateCropInfo();
    renderImportList();
    clearTimeout(sub.previewTimer);
    sub.previewTimer = setTimeout(() => { if (item === currentItem()) selectItem(sub.selected); }, delay);
  }

  function drawCropBox() {
    const item = currentItem();
    const box = $("crop-box");
    $("crop-clear").disabled = !(item && item.box);
    if (!item || !item.box || !item.natW || sub.shownId !== item.id) { box.hidden = true; return; }
    const [x1, y1, x2, y2] = item.box;
    box.hidden = false;
    box.style.left = (x1 / item.natW) * 100 + "%";
    box.style.top = (y1 / item.natH) * 100 + "%";
    box.style.width = ((x2 - x1) / item.natW) * 100 + "%";
    box.style.height = ((y2 - y1) / item.natH) * 100 + "%";
  }

  function updateCropInfo() {
    const item = currentItem();
    const warn = $("crop-warning");
    warn.hidden = true;
    if (!item || !item.natW) { $("crop-info").textContent = ""; return; }
    let w = item.natW, h = item.natH;
    if (item.box) {
      const [x1, y1, x2, y2] = item.box;
      w = x2 - x1; h = y2 - y1;
      $("crop-info").textContent = `Box ${x1},${y1} → ${x2},${y2} (${w}×${h} px)`;
    } else {
      $("crop-info").textContent = `No box: the whole image is sent (${w}×${h} px)`;
    }
    const max = sub.info && sub.info.limits && sub.info.limits.max_pixels;
    if (max && w * h > max) {
      warn.hidden = false;
      warn.textContent = `${w}×${h} = ${(w * h).toLocaleString()} px exceeds the server limit of ` +
        `${max.toLocaleString()} px; the server will reject it. Draw a smaller box or downscale the image.`;
    }
  }

  function pointToImage(e) {
    const item = currentItem();
    const r = $("crop-stage").getBoundingClientRect();
    const x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
    const y = Math.min(Math.max(e.clientY - r.top, 0), r.height);
    return [Math.round((x / r.width) * item.natW), Math.round((y / r.height) * item.natH)];
  }

  function setTool(tool) {
    sub.tool = tool;
    $("tool-box").classList.toggle("active", tool === "box");
    $("tool-pan").classList.toggle("active", tool === "pan");
    $("crop-viewport").classList.toggle("pan-mode", tool === "pan" || sub.space);
  }

  function bindNewView() {
    const vp = $("crop-viewport");
    const canPan = (e) => e.button === 1 || (e.button === 0 && (sub.tool === "pan" || sub.space));
    bindZoom(cropZoom, { canPan, dblclick: false });

    vp.addEventListener("pointerdown", (e) => {
      const item = currentItem();
      if (e.button !== 0 || canPan(e) || !item || !item.natW || sub.shownId !== item.id) return;
      e.preventDefault();
      vp.focus();
      sub.drag = { start: pointToImage(e), id: e.pointerId };
      vp.setPointerCapture(e.pointerId);
    });
    vp.addEventListener("pointermove", (e) => {
      if (!sub.drag || e.pointerId !== sub.drag.id) return;
      const [ax, ay] = sub.drag.start;
      const [bx, by] = pointToImage(e);
      currentItem().box = [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)];
      drawCropBox();
      updateCropInfo();
    });
    const end = (e) => {
      if (!sub.drag || e.pointerId !== sub.drag.id) return;
      sub.drag = null;
      if (vp.hasPointerCapture(e.pointerId)) vp.releasePointerCapture(e.pointerId);
      const item = currentItem();
      if (item.box && (item.box[2] - item.box[0] < 4 || item.box[3] - item.box[1] < 4)) item.box = null;
      drawCropBox();
      updateCropInfo();
      renderImportList();
    };
    vp.addEventListener("pointerup", end);
    vp.addEventListener("pointercancel", end);

    window.addEventListener("keydown", (e) => {
      if (e.code !== "Space" || $("new-view").hidden || isTyping(e.target)) return;
      e.preventDefault();
      if (!sub.space) { sub.space = true; vp.classList.add("pan-mode"); }
    });
    window.addEventListener("keyup", (e) => {
      if (e.code === "Space" && sub.space) { sub.space = false; vp.classList.toggle("pan-mode", sub.tool === "pan"); }
    });
    $("tool-box").onclick = () => setTool("box");
    $("tool-pan").onclick = () => setTool("pan");
    $("crop-zoom-in").onclick = () => cropZoom.zoomCenter(1.25);
    $("crop-zoom-out").onclick = () => cropZoom.zoomCenter(0.8);
    $("crop-zoom-fit").onclick = () => cropZoom.fit();
    $("crop-zoom-actual").onclick = () => cropZoom.actual();

    const img = $("crop-image");
    img.addEventListener("load", () => {
      const item = currentItem();
      $("crop-loading").hidden = true;
      img.style.transform = "";
      if (!item || img.getAttribute("src") !== previewUrl(item)) return;
      const refit = sub.shownId !== item.id || item.natW !== img.naturalWidth || item.natH !== img.naturalHeight;
      showStage(item, refit);
      drawCropBox();
      updateCropInfo();
      renderImportList();
    });
    img.addEventListener("error", () => {
      const item = currentItem();
      if (!item || img.getAttribute("src") !== previewUrl(item)) return;
      $("crop-loading").hidden = true;
      item.error = "Could not render a preview of this image.";
      renderImportList();
    });

    $("crop-clear").onclick = () => {
      const item = currentItem();
      if (item) item.box = null;
      drawCropBox();
      updateCropInfo();
      renderImportList();
    };

    // adjustments
    $("adj-grayscale").onchange = (e) => setAdjust({ grayscale: e.target.checked }, 0);
    $("adj-bw").onchange = (e) => setAdjust({ threshold: e.target.checked ? sub.lastThreshold : null }, 0);
    $("adj-threshold").oninput = (e) => {
      sub.lastThreshold = Number(e.target.value);
      $("adj-threshold-value").textContent = e.target.value;
      if ($("adj-bw").checked) setAdjust({ threshold: sub.lastThreshold });
    };
    $("adj-rotation").onchange = (e) => setAdjust({ rotation: e.target.value }, 0);
    $("adj-rotation-slider").oninput = (e) => setAdjust({ rotation: e.target.value });
    const rotateBy = (delta, delay) => {
      const item = currentItem();
      if (item) setAdjust({ rotation: item.adjust.rotation + delta }, delay);
    };
    $("rot-minus").onclick = () => rotateBy(-0.1, 250);
    $("rot-plus").onclick = () => rotateBy(0.1, 250);
    $("rot-left90").onclick = () => rotateBy(-90, 0);
    $("rot-right90").onclick = () => rotateBy(90, 0);
    $("adj-reset").onclick = () => setAdjust(defaultAdjust(), 0);

    // import
    $("import-files").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
    const drop = $("drop-zone");
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      addFiles([...e.dataTransfer.files].filter((f) => f.type.startsWith("image/") || /\.tiff?$/i.test(f.name)));
    });

    // server
    $("server-url").addEventListener("change", () => {
      localStorage.setItem(SERVER_KEY, serverUrl());
      checkServer();
    });
    $("server-check").onclick = () => checkServer();
    $("server-reset").onclick = () => {
      localStorage.removeItem(SERVER_KEY);
      $("server-url").value = (sub.config && sub.config.server_url) || "";
      checkServer();
    };
    $("submit-ocr").onclick = () => submitAll().catch(showFatal);
  }

  async function submitAll() {
    const persist = document.querySelector("input[name=persist]:checked").value;
    const prompt = $("ocr-prompt").value;
    const messages = [];
    $("submit-ocr").disabled = true;
    const remaining = [];
    for (const item of [...sub.items]) {
      if (!item.source) {
        remaining.push(item);
        if (item.status === "uploading") messages.push(el("div", { class: "message warn" }, `${item.name}: still uploading`));
        continue;
      }
      const form = new FormData();
      form.append("source_id", item.source.source_id);
      form.append("server_url", serverUrl());
      form.append("persist", persist);
      if (prompt.trim()) form.append("prompt", prompt);
      if (item.box) form.append("box", item.box.join(","));
      if (item.adjust.rotation) form.append("rotation", item.adjust.rotation.toFixed(1));
      if (item.adjust.grayscale) form.append("grayscale", "true");
      if (item.adjust.threshold != null) form.append("threshold", String(item.adjust.threshold));
      if (item.resubmitOf) form.append("resubmit_of", item.resubmitOf);
      try {
        const rec = await api("/api/submissions", { method: "POST", body: form });
        messages.push(el("div", { class: "message ok" }, `${item.name}: job ${rec.job_id} accepted · `,
          el("a", { href: "#/submissions" }, "View submissions")));
      } catch (err) {
        item.error = err.message;
        remaining.push(item);
        messages.push(el("div", { class: "message error" }, `${item.name}: ${err.message}`));
      }
    }
    sub.items = remaining;
    sub.selected = remaining.length ? 0 : -1;
    renderImportList();
    selectItem(sub.selected);
    $("submit-messages").replaceChildren(...messages);
  }

  async function showNew(params) {
    showSection("new-view");
    document.title = "New OCR – OCR Artifact Viewer";
    if (!$("server-url").value) {
      $("server-url").value = localStorage.getItem(SERVER_KEY) || (sub.config && sub.config.server_url) || "";
    }
    if ($("server-url").value) checkServer();
    else $("server-status").textContent = "Enter the inference server URL";
    const resubmit = params.get("resubmit");
    const jobKey = params.get("job");
    if (resubmit || jobKey) {
      history.replaceState(null, "", "#/new");
      const url = resubmit
        ? `/api/submissions/${encodeURIComponent(resubmit)}/draft`
        : `/api/jobs/${encodeURIComponent(jobKey)}/draft`;
      try {
        addDraft(await api(url, { method: "POST" }));
      } catch (err) {
        $("submit-messages").replaceChildren(el("div", { class: "message error" }, "Cannot load job: " + err.message));
      }
    }
    if (cropZoom.natW) requestAnimationFrame(() => cropZoom.fit());
  }

  // ------------------------------------------------------------------ submissions page
  function submissionBadge(rec) {
    if (rec.state === "error") return ["failed", "error"];
    if (rec.state === "imported") return [rec.job_status, rec.job_status];
    const status = rec.job_status || "queued";
    return [status, rec.last_error ? "waiting for server" : status];
  }

  async function resubmitNow(rec) {
    try {
      const res = await api(`/api/submissions/${rec.job_id}/resubmit`, { method: "POST" });
      $("sub-messages").replaceChildren(el("div", { class: "message ok" },
        `Resubmitted ${rec.job_id.slice(0, 8)} as job ${res.job_id}`));
    } catch (err) {
      $("sub-messages").replaceChildren(el("div", { class: "message error" },
        `Resubmitting ${rec.job_id.slice(0, 8)} failed: ${err.message}`));
    }
    if (state.subs.page !== 1) location.hash = subsHash({ page: 1 });
    else await pollSubmissions();
  }

  function submissionRow(rec) {
    const [cls, label] = submissionBadge(rec);
    const status = el("td", {}, el("span", { class: `badge ${cls}` }, label));
    if (rec.error || rec.last_error) status.append(el("div", { class: "muted small" }, rec.error || rec.last_error));
    const actions = el("td", { class: "actions" });
    if (rec.key) actions.append(el("a", { href: jobHash(rec.key), class: "open-job" }, "Open"));
    if (rec.status !== "pending") {
      const redraw = el("button", { type: "button", class: "redraw", title: "Open in New OCR to change box/adjustments" },
        "Redraw");
      redraw.onclick = () => { location.hash = "#/new?resubmit=" + rec.job_id; };
      const again = el("button", { type: "button", class: "resubmit", title: "Submit again with the same settings" },
        "Resubmit");
      again.onclick = () => { again.disabled = true; resubmitNow(rec).catch(showFatal); };
      actions.append(redraw, again);
    } else {
      actions.append(el("span", { class: "muted" }, "pending…"));
    }
    const job = el("td", { title: rec.server_url }, el("code", {}, rec.job_id));
    if (rec.resubmit_of) job.append(el("div", { class: "muted small" }, `resubmission of ${rec.resubmit_of.slice(0, 8)}`));
    return el("tr", { dataset: { jobId: rec.job_id, state: rec.state, status: rec.status } },
      el("td", {}, fmtDate(rec.submitted_at)),
      el("td", {}, rec.original_filename),
      job,
      el("td", {}, rec.crop ? rec.crop.box.join(",") : "whole image"),
      el("td", {}, adjustLabel(rec.adjustments) || "—"),
      el("td", {}, rec.crop ? rec.persist : "original"),
      status,
      actions);
  }

  async function refreshSubmissions() {
    const qs = new URLSearchParams({ page: state.subs.page, page_size: state.subs.size });
    if (state.subs.status) qs.set("status", state.subs.status);
    if (state.subs.q) qs.set("q", state.subs.q);
    const data = await api("/api/submissions?" + qs.toString());
    state.subs.page = data.page;
    state.subs.pages = data.pages;
    $("submission-rows").replaceChildren(...data.items.map(submissionRow));
    $("sub-empty").hidden = data.total > 0;
    $("sub-page-info").textContent = `Page ${data.page} of ${data.pages}`;
    $("sub-total").textContent = `${data.total} submission${data.total === 1 ? "" : "s"}`;
    $("sub-first").disabled = $("sub-prev").disabled = data.page <= 1;
    $("sub-next").disabled = $("sub-last").disabled = data.page >= data.pages;
    return data.items;
  }

  function stopSubmissionPolling() {
    clearTimeout(sub.timer);
    sub.timer = null;
  }

  async function pollSubmissions() {
    stopSubmissionPolling();
    const items = await refreshSubmissions().catch(() => []);
    if ($("submissions-view").hidden) return;
    const busy = items.some((r) => r.state === "submitted");
    sub.timer = setTimeout(pollSubmissions, busy ? 1000 : 5000);
  }

  async function showSubmissions() {
    showSection("submissions-view");
    document.title = "Submissions – OCR Artifact Viewer";
    $("sub-status").value = state.subs.status;
    if (document.activeElement !== $("sub-search")) $("sub-search").value = state.subs.q;
    ensureOption($("sub-page-size"), state.subs.size);
    $("inbox-info").textContent = sub.config && sub.config.inbox ? `Results are saved to ${sub.config.inbox}` : "";
    await pollSubmissions();
  }

  function bindSubmissionControls() {
    bindPager(state.subs, ["sub-first", "sub-prev", "sub-next", "sub-last"], subsHash, () => state.subs.pages);
    $("sub-status").onchange = (e) => { location.hash = subsHash({ status: e.target.value, page: 1 }); };
    $("sub-page-size").onchange = (e) => { location.hash = subsHash({ size: e.target.value, page: 1 }); };
    bindSearch($("sub-search"), subsHash);
    $("sub-refresh").onclick = () => pollSubmissions().catch(showFatal);
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    bindListControls();
    bindViewer();
    bindJobControls();
    bindNewView();
    bindSubmissionControls();
    api("/api/roots").then((r) => { $("roots").textContent = r.roots.join(" · "); }).catch(() => {});
    sub.config = await api("/api/config").catch(() => ({ submissions_enabled: false }));
    $("nav-new").hidden = $("nav-submissions").hidden = !sub.config.submissions_enabled;
    await route();
  }

  boot().catch(showFatal);
})();
