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
  async function route() {
    const { path, params } = parseHash();
    const m = path.match(/^\/job\/(.+)$/);
    if (m) {
      await showJob(decodeURIComponent(m[1]));
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
    $("job-view").hidden = true;
    $("list-view").hidden = false;
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

  function drawRegions(markdown) {
    const overlay = $("overlay");
    const scale = (state.job && state.job.bbox_scale) || 1000;
    const nodes = [];
    for (const [name, [l, t, r, b]] of parseRegions(markdown)) {
      const box = el("div", { class: "region", title: name, dataset: { name } });
      box.style.left = (l / scale) * 100 + "%";
      box.style.top = (t / scale) * 100 + "%";
      box.style.width = (Math.max(0, r - l) / scale) * 100 + "%";
      box.style.height = (Math.max(0, b - t) / scale) * 100 + "%";
      nodes.push(box);
    }
    overlay.replaceChildren(...nodes);
  }

  function highlightRegion(name) {
    const scale = (state.job && state.job.bbox_scale) || 1000;
    for (const node of $("overlay").children) node.classList.toggle("active", node.dataset.name === name);
    for (const img of $("rendered").querySelectorAll("img")) {
      img.classList.toggle("active", (img.getAttribute("src") || "").endsWith("/" + name));
    }
    const coords = parseRegions("images/" + name).get(name);
    if (coords && viewer.natW) {
      const [l, t, r, b] = coords;
      viewer.centerOn(((l + r) / 2 / scale) * viewer.natW, ((t + b) / 2 / scale) * viewer.natH);
    }
  }

  // ------------------------------------------------------------------ job view
  async function showJob(key) {
    $("list-view").hidden = true;
    $("job-view").hidden = false;
    const job = await api("/api/jobs/" + encodeURIComponent(key));
    state.job = job;
    state.savedMarkdown = job.markdown;
    state.dirty = false;

    $("job-title").textContent = job.job_id;
    $("job-status").textContent = job.status;
    $("job-status").className = "badge " + job.status;
    $("job-file").textContent = [job.original_filename, job.width && `${job.width}×${job.height}`]
      .filter(Boolean).join(" · ");
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

  // ------------------------------------------------------------------ boot
  async function boot() {
    bindListControls();
    bindViewer();
    bindJobControls();
    api("/api/roots").then((r) => { $("roots").textContent = r.roots.join(" · "); }).catch(() => {});
    await route();
  }

  boot().catch(showFatal);
})();
