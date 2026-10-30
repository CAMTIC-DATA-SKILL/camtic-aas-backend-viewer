"use strict";

(function () {
  const KIND_LABEL = { step: "STEP", usd: "USD", file: "파일" };
  const ACCEPT = {
    step: ".stp,.step,application/step",
    usd: ".usd,.usda,.usdc,.usdz",
    file: "*",
  };
  const HINT = {
    step: ".stp / .step 파일을 놓거나 클릭",
    usd: ".usd / .usda / .usdc / .usdz 파일을 놓거나 클릭",
    file: "파일을 놓거나 클릭하여 첨부",
  };
  const STEP_EXTS = [".stp", ".step"];
  const USD_EXTS = [".usd", ".usda", ".usdc", ".usdz"];

  const state = {
    packageId: null,
    rows: [],
    nextId: 1,
    onSaved: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "className") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function formatBytes(size) {
    if (size == null) return "-";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function fileExt(name) {
    const match = /\.[^.]+$/.exec(name || "");
    return match ? match[0].toLowerCase() : "";
  }

  function kindError(kind, filename) {
    const ext = fileExt(filename);
    if (kind === "step" && !STEP_EXTS.includes(ext)) {
      return "STEP 규격이 아닙니다. .stp 또는 .step 파일만 첨부할 수 있습니다.";
    }
    if (kind === "usd" && !USD_EXTS.includes(ext)) {
      return "USD 규격이 아닙니다. .usd / .usda / .usdc / .usdz 파일만 첨부할 수 있습니다.";
    }
    return "";
  }

  function sanitizeIdShort(raw) {
    return String(raw || "")
      .trim()
      .replace(/[^A-Za-z0-9_-]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 64);
  }

  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        message = body.detail || message;
        if (Array.isArray(message)) message = message.map((d) => d.msg || JSON.stringify(d)).join("; ");
      } catch (_) { /* not json */ }
      throw new Error(message);
    }
    return res.json();
  }

  function ensureDialog() {
    if ($("doc-upload-dialog")) return;
    document.body.append(
      el(
        "dialog",
        { id: "doc-upload-dialog", className: "upload-dialog doc-upload-dialog" },
        el(
          "form",
          { method: "dialog", id: "doc-upload-form" },
          el(
            "header",
            {},
            el("h2", {}, "Documentation Submodel"),
            el("button", { type: "submit", value: "cancel", className: "icon-btn", "aria-label": "닫기" }, "×"),
          ),
          el(
            "p",
            { className: "muted" },
            "AASX의 Documentation 항목을 확인하고 STEP, USD 등 DT 모델링 파일을 첨부합니다.",
          ),
          el("div", { id: "doc-submodel-meta", className: "doc-meta" }),
          el(
            "section",
            { className: "doc-section" },
            el("h3", {}, "기존 첨부 파일"),
            el("div", { id: "doc-file-list", className: "doc-file-list" }),
          ),
          el(
            "section",
            { className: "doc-section" },
            el(
              "div",
              { className: "doc-section-head" },
              el("h3", {}, "DT 모델링 파일 첨부"),
              el("button", { id: "doc-add-btn", type: "button", className: "doc-plus-btn", title: "첨부 항목 추가" }, "+"),
            ),
            el("div", { id: "doc-entry-list", className: "doc-entry-list" }),
          ),
          el("p", { id: "doc-upload-status", className: "upload-status", hidden: true }),
          el(
            "footer",
            {},
            el("button", { type: "submit", value: "cancel" }, "닫기"),
            el("button", { id: "doc-upload-submit", type: "button", className: "btn-primary", disabled: true }, "첨부하기"),
          ),
        ),
      ),
    );

    $("doc-upload-submit").addEventListener("click", submitAttachments);
    $("doc-add-btn").addEventListener("click", () => addRow());
    $("doc-upload-dialog").addEventListener("close", resetSelection);
  }

  function addRow(preset = {}) {
    const row = {
      id: state.nextId++,
      name: preset.name || "",
      kind: preset.kind || "file",
      file: null,
    };
    state.rows.push(row);
    renderRows();
    $(`doc-name-${row.id}`)?.focus();
    return row;
  }

  function removeRow(id) {
    state.rows = state.rows.filter((row) => row.id !== id);
    renderRows();
    updateSubmitState();
  }

  function getRow(id) {
    return state.rows.find((row) => row.id === id);
  }

  function renderRows() {
    const box = $("doc-entry-list");
    if (!state.rows.length) {
      box.replaceChildren(el("p", { className: "muted" }, "+ 버튼을 눌러 File 이름과 종류를 지정한 뒤 파일을 첨부하세요."));
      return;
    }
    box.replaceChildren(...state.rows.map(renderRow));
  }

  function renderRow(row) {
    const inputId = `doc-file-${row.id}`;
    const dropzone = el(
      "label",
      { className: "dropzone doc-entry-drop", for: inputId },
      el("input", {
        id: inputId,
        type: "file",
        accept: ACCEPT[row.kind],
        hidden: true,
        onchange: (e) => setRowFile(row.id, e.target.files?.[0] || null, e.target),
      }),
      el(
        "span",
        { className: "doc-slot-label" },
        row.file ? `${row.file.name} (${formatBytes(row.file.size)})` : HINT[row.kind],
      ),
    );
    bindDropzone(dropzone, row.id);

    return el(
      "article",
      { className: "doc-entry", "data-row-id": String(row.id) },
      el(
        "div",
        { className: "doc-entry-top" },
        el(
          "label",
          { className: "doc-name-field" },
          "File 이름",
          el("input", {
            id: `doc-name-${row.id}`,
            type: "text",
            value: row.name,
            placeholder: "예: RobotCAD",
            spellcheck: "false",
            oninput: (e) => {
              row.name = e.target.value;
            },
          }),
        ),
        el(
          "button",
          { type: "button", className: "icon-btn", title: "항목 삭제", onclick: () => removeRow(row.id) },
          "×",
        ),
      ),
      el(
        "fieldset",
        { className: "doc-kind-box" },
        el("legend", {}, "파일 종류"),
        ...["step", "usd", "file"].map((kind) =>
          el(
            "label",
            {},
            el("input", {
              type: "radio",
              name: `doc-kind-${row.id}`,
              value: kind,
              checked: row.kind === kind,
              onchange: () => setRowKind(row.id, kind),
            }),
            ` ${KIND_LABEL[kind]}`,
          ),
        ),
      ),
      dropzone,
    );
  }

  function bindDropzone(dropzone, rowId) {
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
      setRowFile(rowId, e.dataTransfer?.files?.[0] || null);
    });
  }

  function setRowKind(id, kind) {
    const row = getRow(id);
    if (!row) return;
    if (row.file) {
      const error = kindError(kind, row.file.name);
      if (error) {
        window.alert(error);
        renderRows();
        return;
      }
    }
    row.kind = kind;
    renderRows();
    updateSubmitState();
  }

  function setRowFile(id, file, input) {
    const row = getRow(id);
    if (!row) return;
    if (!file) {
      row.file = null;
      renderRows();
      updateSubmitState();
      return;
    }
    const error = kindError(row.kind, file.name);
    if (error) {
      window.alert(error);
      if (input) input.value = "";
      return;
    }
    row.file = file;
    if (!sanitizeIdShort(row.name)) {
      row.name = sanitizeIdShort(file.name.replace(/\.[^.]+$/, "")) || row.name;
    }
    renderRows();
    updateSubmitState();
    showStatus("", false, true);
  }

  function readyRows() {
    return state.rows.filter((row) => row.file);
  }

  function updateSubmitState() {
    if ($("doc-upload-submit")) $("doc-upload-submit").disabled = !readyRows().length;
  }

  function resetSelection() {
    state.rows = [];
    state.nextId = 1;
    if ($("doc-entry-list")) renderRows();
    updateSubmitState();
  }

  function showStatus(message, isError = false, hide = false) {
    const node = $("doc-upload-status");
    if (!node) return;
    if (hide || !message) {
      node.hidden = true;
      node.textContent = "";
      return;
    }
    node.hidden = false;
    node.className = `upload-status ${isError ? "error-text" : "ok-text"}`;
    node.textContent = message;
  }

  function renderDocumentation(docs) {
    const submodels = docs.submodels || [];
    const files = submodels.flatMap((sm) => sm.files || []);
    $("doc-submodel-meta").replaceChildren(
      el("div", {}, el("strong", {}, docs.fileName || docs.packageId || "")),
      submodels.length
        ? el(
            "div",
            { className: "muted" },
            submodels
              .map((sm) => `${sm.idShort || "Documentation"}${sm.semanticId ? ` · ${sm.semanticId}` : ""}`)
              .join(" / "),
          )
        : el("div", { className: "muted" }, "Documentation Submodel이 없습니다. 파일을 첨부하면 새로 만듭니다."),
    );

    if (!files.length) {
      $("doc-file-list").replaceChildren(el("p", { className: "muted" }, "아직 첨부된 파일이 없습니다."));
      return;
    }
    $("doc-file-list").replaceChildren(
      el(
        "ul",
        {},
        files.map((item) =>
          el(
            "li",
            {},
            el("span", { className: `doc-kind-badge ${item.kind}` }, KIND_LABEL[item.kind] || "파일"),
            el("span", { className: "doc-file-name" }, item.idShort || item.fileName || "(이름 없음)"),
            el("span", { className: "muted" }, item.fileName || "-"),
            el("span", { className: "muted" }, formatBytes(item.size)),
            item.url ? el("a", { href: item.url, target: "_blank", rel: "noopener" }, "열기") : null,
          ),
        ),
      ),
    );
  }

  async function loadDocumentation(packageId, seed) {
    if (seed) {
      renderDocumentation(seed);
      return seed;
    }
    $("doc-file-list").replaceChildren(el("p", { className: "muted" }, "불러오는 중..."));
    const docs = await fetchJson(`/api/packages/${encodeURIComponent(packageId)}/documentation`);
    renderDocumentation(docs);
    return docs;
  }

  async function submitAttachments() {
    if (!state.packageId) return;
    const rows = readyRows();
    if (!rows.length) return;
    for (const row of rows) {
      const error = kindError(row.kind, row.file.name);
      if (error) {
        window.alert(`${row.name || row.file.name}: ${error}`);
        return;
      }
    }
    const form = new FormData();
    for (const row of rows) {
      form.append("files", row.file, row.file.name);
      form.append("kinds", row.kind);
      form.append("names", sanitizeIdShort(row.name));
    }
    $("doc-upload-submit").disabled = true;
    showStatus("Documentation Submodel에 첨부하는 중...", false);
    try {
      const result = await fetchJson(`/api/packages/${encodeURIComponent(state.packageId)}/documentation/files`, {
        method: "POST",
        body: form,
      });
      resetSelection();
      renderDocumentation(result);
      const count = result.added?.length || 0;
      showStatus(`첨부 완료: ${count}개 파일`, false);
      state.onSaved?.(result);
    } catch (e) {
      showStatus(e.message, true);
      updateSubmitState();
    }
  }

  async function open(packageId, { documentation, onSaved } = {}) {
    if (!packageId) return;
    ensureDialog();
    state.packageId = packageId;
    state.onSaved = onSaved || null;
    resetSelection();
    showStatus("", false, true);
    $("doc-upload-dialog").showModal();
    try {
      await loadDocumentation(packageId, documentation);
    } catch (e) {
      $("doc-file-list").replaceChildren(el("p", { className: "error-text" }, e.message));
    }
  }

  window.DocumentationUpload = { open };
})();
