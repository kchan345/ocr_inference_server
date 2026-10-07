"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const BBOX_RE = /images\/bbox_(\d+)_(\d+)_(\d+)_(\d+)\.(?:jpe?g|png)/gi;

  const state = {
    list: { page: 1, size: 20, status: "", q: "", pages: 1, total: 0, keys: [] },
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

  function listHash(overrides = {}) {
    const p = { page: state.list.page, size: state.list.size, status: state.list.status, q: state.list.q, ...overrides };
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(p)) if (v !== "" && v != null) qs.set(k, v);
    return "#/jobs?" + qs.toString();
  }

  function jobHash(key) { return "#/job/" + encodeURIComponent(key); }

  // ------------------------------------------------------------------ routing
  function showSection(id) {
    for (const s of ["list-view", "job-view", "new-view"]) $(s).hidden = s !== id;
    $("nav-jobs").classList.toggle("active", id !== "new-view");
    $("nav-new").classList.toggle("active", id === "new-view");
    if (id !== "new-view") stopSubmissionPolling();
  }

  async function route() {
    const { path, params } = parseHash();
    const m = path.match(/^\/job\/(.+)$/);
    if (m) {
      await showJob(decodeURIComponent(m[1]));
    } else if (path === "/new") {
      await showNew();
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

  function showFatal(err) {
    console.error(err);
    alert(err.message || String(err));
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
    if (![...$("page-size").options].some((o) => o.value === String(state.list.size))) {
      $("page-size").append(el("option", { value: state.list.size, textContent: state.list.size }));
    }
    $("page-size").value = String(state.list.size);

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

  function bindListControls() {
    $("first-page").onclick = () => { location.hash = listHash({ page: 1 }); };
    $("prev-page").onclick = () => { location.hash = listHash({ page: state.list.page - 1 }); };
    $("next-page").onclick = () => { location.hash = listHash({ page: state.list.page + 1 }); };
    $("last-page").onclick = () => { location.hash = listHash({ page: state.list.pages }); };
    $("status-filter").onchange = (e) => { location.hash = listHash({ status: e.target.value, page: 1 }); };
    $("page-size").onchange = (e) => { location.hash = listHash({ size: e.target.value, page: 1 }); };
    let timer = null;
    $("search").oninput = (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => { location.hash = listHash({ q: e.target.value.trim(), page: 1 }); }, 300);
    };
    $("refresh").onclick = () => showList().catch(showFatal);
  }

  // ------------------------------------------------------------------ image viewer
  const viewer = {
    scale: 1, tx: 0, ty: 0, min: 0.02, max: 32, natW: 0, natH: 0,
    get vp() { return $("viewport"); },
    apply() {
      $("stage").style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
      $("zoom-level").textContent = Math.round(this.scale * 100) + "%";
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

  function bindViewer() {
    const vp = $("viewport");
    vp.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = vp.getBoundingClientRect();
      const delta = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
      viewer.zoomAt(Math.exp(-delta * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });

    let drag = null;
    vp.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, tx: viewer.tx, ty: viewer.ty, id: e.pointerId };
      vp.setPointerCapture(e.pointerId);
      vp.classList.add("dragging");
      vp.focus();
    });
    vp.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      viewer.tx = drag.tx + (e.clientX - drag.x);
      viewer.ty = drag.ty + (e.clientY - drag.y);
      viewer.apply();
    });
    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      vp.classList.remove("dragging");
      if (vp.hasPointerCapture(e.pointerId)) vp.releasePointerCapture(e.pointerId);
    };
    vp.addEventListener("pointerup", endDrag);
    vp.addEventListener("pointercancel", endDrag);
    vp.addEventListener("dblclick", (e) => {
      const r = vp.getBoundingClientRect();
      viewer.zoomAt(e.shiftKey ? 0.5 : 2, e.clientX - r.left, e.clientY - r.top);
    });
    vp.addEventListener("keydown", (e) => {
      if (e.key === "+" || e.key === "=") viewer.zoomCenter(1.25);
      else if (e.key === "-" || e.key === "_") viewer.zoomCenter(0.8);
      else if (e.key === "0") viewer.fit();
      else if (e.key === "1") viewer.actual();
      else return;
      e.preventDefault();
    });

    $("zoom-in").onclick = () => viewer.zoomCenter(1.25);
    $("zoom-out").onclick = () => viewer.zoomCenter(0.8);
    $("zoom-fit").onclick = () => viewer.fit();
    $("zoom-actual").onclick = () => viewer.actual();
    $("show-regions").onchange = (e) => $("overlay").classList.toggle("hidden", !e.target.checked);
    window.addEventListener("resize", () => { if (!$("job-view").hidden) viewer.fit(); });

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
      job.crop && `OCR region ${job.crop.box.join(",")} · kept ${job.persisted_image} image`,
    ].filter(Boolean).join(" · ");
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

  // ------------------------------------------------------------------ new OCR submissions
  const SERVER_KEY = "ocrViewer.serverUrl";
  const sub = { config: null, items: [], selected: -1, nextId: 1, info: null, timer: null, drag: null };

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

  function addFiles(files) {
    for (const file of files) {
      const item = { id: sub.nextId++, file, url: URL.createObjectURL(file), box: null, natW: 0, natH: 0, error: "" };
      sub.items.push(item);
      const probe = new Image();
      probe.onload = () => {
        item.natW = probe.naturalWidth;
        item.natH = probe.naturalHeight;
        renderImportList();
        if (item === currentItem()) { drawCropBox(); updateCropInfo(); }
      };
      probe.onerror = () => { item.error = "Preview not supported by the browser (can still be sent whole)"; renderImportList(); };
      probe.src = item.url;
    }
    if (sub.selected < 0 && sub.items.length) sub.selected = 0;
    renderImportList();
    selectItem(sub.selected);
  }

  function boxLabel(item) {
    if (!item.box) return "whole image";
    const [x1, y1, x2, y2] = item.box;
    return `box ${x2 - x1}×${y2 - y1}`;
  }

  function renderImportList() {
    $("import-list").replaceChildren(...sub.items.map((item, idx) => {
      const remove = el("button", { type: "button", class: "remove", title: "Remove", textContent: "×" });
      remove.addEventListener("click", (e) => { e.stopPropagation(); removeItem(idx); });
      const li = el("li", { class: idx === sub.selected ? "selected" : "", dataset: { id: item.id } },
        el("span", { class: "name", title: item.file.name }, item.file.name),
        el("span", { class: "muted small" }, item.natW ? `${item.natW}×${item.natH} · ${boxLabel(item)}` : ""),
        remove);
      if (item.error) li.append(el("div", { class: "err" }, item.error));
      li.addEventListener("click", () => selectItem(idx));
      return li;
    }));
    $("submit-ocr").disabled = sub.items.length === 0;
  }

  function removeItem(idx) {
    URL.revokeObjectURL(sub.items[idx].url);
    sub.items.splice(idx, 1);
    if (sub.selected >= sub.items.length) sub.selected = sub.items.length - 1;
    renderImportList();
    selectItem(sub.selected);
  }

  function currentItem() { return sub.items[sub.selected] || null; }

  function selectItem(idx) {
    sub.selected = idx;
    const item = currentItem();
    $("crop-stage").hidden = !item;
    $("crop-name").textContent = item ? item.file.name : "No image selected";
    if (item && $("crop-image").getAttribute("src") !== item.url) $("crop-image").src = item.url;
    for (const li of $("import-list").children) li.classList.toggle("selected", Number(li.dataset.id) === (item && item.id));
    drawCropBox();
    updateCropInfo();
  }

  function drawCropBox() {
    const item = currentItem();
    const box = $("crop-box");
    $("crop-clear").disabled = !(item && item.box);
    if (!item || !item.box || !item.natW) { box.hidden = true; return; }
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
    if (!item) { $("crop-info").textContent = ""; return; }
    let w = item.natW, h = item.natH;
    if (item.box) {
      const [x1, y1, x2, y2] = item.box;
      w = x2 - x1; h = y2 - y1;
      $("crop-info").textContent = `Box ${x1},${y1} → ${x2},${y2} (${w}×${h} px)`;
    } else {
      $("crop-info").textContent = "No box: the whole image is sent";
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
    const r = $("crop-image").getBoundingClientRect();
    const x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
    const y = Math.min(Math.max(e.clientY - r.top, 0), r.height);
    return [Math.round((x / r.width) * item.natW), Math.round((y / r.height) * item.natH)];
  }

  function bindCropper() {
    const stage = $("crop-stage");
    stage.addEventListener("pointerdown", (e) => {
      const item = currentItem();
      if (e.button !== 0 || !item || !item.natW) return;
      e.preventDefault();
      sub.drag = { start: pointToImage(e), id: e.pointerId };
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", (e) => {
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
      const item = currentItem();
      if (item.box && (item.box[2] - item.box[0] < 4 || item.box[3] - item.box[1] < 4)) item.box = null;
      drawCropBox();
      updateCropInfo();
      renderImportList();
    };
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", end);
    $("crop-clear").onclick = () => {
      const item = currentItem();
      if (item) item.box = null;
      drawCropBox();
      updateCropInfo();
      renderImportList();
    };
    $("import-files").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
    const drop = $("drop-zone");
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      addFiles([...e.dataTransfer.files].filter((f) => f.type.startsWith("image/") || /\.tiff?$/i.test(f.name)));
    });
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
      const form = new FormData();
      form.append("image", item.file, item.file.name);
      form.append("server_url", serverUrl());
      form.append("persist", persist);
      if (prompt.trim()) form.append("prompt", prompt);
      if (item.box) form.append("box", item.box.join(","));
      try {
        const rec = await api("/api/submissions", { method: "POST", body: form });
        messages.push(el("div", { class: "message ok" }, `${item.file.name}: job ${rec.job_id} accepted`));
        URL.revokeObjectURL(item.url);
      } catch (err) {
        item.error = err.message;
        remaining.push(item);
        messages.push(el("div", { class: "message error" }, `${item.file.name}: ${err.message}`));
      }
    }
    sub.items = remaining;
    sub.selected = remaining.length ? 0 : -1;
    renderImportList();
    selectItem(sub.selected);
    $("submit-messages").replaceChildren(...messages);
    await refreshSubmissions();
  }

  function submissionStatus(rec) {
    if (rec.state === "error") return ["failed", "error"];
    if (rec.state === "imported") return [rec.job_status, rec.job_status];
    return [rec.job_status || "queued", rec.last_error ? "waiting for server" : rec.job_status || "queued"];
  }

  async function refreshSubmissions() {
    const { items } = await api("/api/submissions");
    $("submission-rows").replaceChildren(...items.map((rec) => {
      const [cls, label] = submissionStatus(rec);
      const status = el("td", {}, el("span", { class: `badge ${cls}` }, label));
      if (rec.error || rec.last_error) status.append(el("div", { class: "muted small" }, rec.error || rec.last_error));
      const open = rec.key
        ? el("a", { href: jobHash(rec.key), class: "open-job" }, "Open")
        : el("span", { class: "muted" }, rec.state === "error" ? "" : "pending…");
      return el("tr", { dataset: { jobId: rec.job_id, state: rec.state } },
        el("td", {}, fmtDate(rec.submitted_at)),
        el("td", {}, rec.original_filename),
        el("td", { title: rec.server_url }, el("code", {}, rec.job_id)),
        el("td", {}, rec.crop ? rec.crop.box.join(",") : "whole image"),
        el("td", {}, rec.crop ? rec.persist : "original"),
        status,
        el("td", {}, open));
    }));
    return items;
  }

  function stopSubmissionPolling() {
    clearTimeout(sub.timer);
    sub.timer = null;
  }

  async function pollSubmissions() {
    stopSubmissionPolling();
    const items = await refreshSubmissions().catch(() => []);
    if ($("new-view").hidden) return;
    const busy = items.some((r) => r.state === "submitted");
    sub.timer = setTimeout(pollSubmissions, busy ? 1000 : 5000);
  }

  async function showNew() {
    showSection("new-view");
    document.title = "New OCR – OCR Artifact Viewer";
    if (!$("server-url").value) {
      $("server-url").value = localStorage.getItem(SERVER_KEY) || (sub.config && sub.config.server_url) || "";
    }
    $("inbox-info").textContent = sub.config && sub.config.inbox ? `Results are saved to ${sub.config.inbox}` : "";
    if ($("server-url").value) checkServer();
    else $("server-status").textContent = "Enter the inference server URL";
    await pollSubmissions();
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    bindListControls();
    bindViewer();
    bindJobControls();
    bindCropper();
    api("/api/roots").then((r) => { $("roots").textContent = r.roots.join(" · "); }).catch(() => {});
    sub.config = await api("/api/config").catch(() => ({ submissions_enabled: false }));
    $("nav-new").hidden = !sub.config.submissions_enabled;
    await route();
  }

  boot().catch(showFatal);
})();
