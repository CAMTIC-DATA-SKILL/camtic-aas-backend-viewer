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
  const MAIN_SLOTS = [
    {
      kind: "step",
      idShort: "DigitalTwinCadModel",
      semanticId: "urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0",
    },
    {
      kind: "usd",
      idShort: "DigitalTwinUsdModel",
      semanticId: "urn:physical-ai:ids/cd/DigitalTwinUsdModel/1/0",
    },
  ];
  const state = {
    packageId: null,
    rows: [],
    nextId: 1,
    onSaved: null,
    slotFiles: { step: null, usd: null },
    docs: null,
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
            "메인 CAD/USD 모델은 SemanticID로 구분합니다. 슬롯을 클릭하거나 파일을 끌어다 놓으세요. 파일이 없으면 '파일 없음'입니다. 그 외 에셋만 + 로 추가합니다.",
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
              el("h3", {}, "추가 에셋"),
              el("button", { id: "doc-add-btn", type: "button", className: "doc-plus-btn", title: "추가 에셋 첨부" }, "+"),
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
      box.replaceChildren(el("p", { className: "muted" }, "+ 버튼으로 메인 모델 이외의 추가 에셋을 첨부하세요."));
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
            placeholder: "예: ToolCAD (메인 슬롯 이름 제외)",
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
      const auto = sanitizeIdShort(file.name.replace(/\.[^.]+$/, ""));
      row.name = reservedNameError(auto) ? "" : auto;
    }
    renderRows();
    updateSubmitState();
    showStatus("", false, true);
  }

  function reservedNameError(name) {
    const cleaned = sanitizeIdShort(name);
    const slot = MAIN_SLOTS.find(
      (item) =>
        item.idShort.toLowerCase() === cleaned.toLowerCase() ||
        item.kind === cleaned.toLowerCase() ||
        item.kind.toUpperCase() === cleaned,
    );
    if (slot) {
      return `메인 모델은 위 ${slot.idShort} 슬롯에서만 지정합니다.`;
    }
    return "";
  }

  function readyRows() {
    return state.rows.filter((row) => row.file);
  }

  function readySlots() {
    return MAIN_SLOTS.filter((slot) => state.slotFiles[slot.kind]);
  }

  function updateSubmitState() {
    if ($("doc-upload-submit")) $("doc-upload-submit").disabled = !(readyRows().length || readySlots().length);
  }

  function resetSelection() {
    state.rows = [];
    state.nextId = 1;
    state.slotFiles = { step: null, usd: null };
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
    state.docs = docs;
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
        : el("div", { className: "muted" }, "Documentation Submodel이 없습니다. 열면 메인 모델 슬롯을 만듭니다."),
    );

    const extras = files.filter((item) => !isMainSlotFile(item));
    $("doc-file-list").replaceChildren(
      el(
        "ul",
        {},
        MAIN_SLOTS.map((slot) => renderSlotRow(slot, findSlotFile(files, slot))),
        extras.map((item) => renderExistingRow(item)),
      ),
    );
    updateSubmitState();
  }

  function isMainSlotFile(item) {
    return MAIN_SLOTS.some(
      (slot) =>
        item.semanticId === slot.semanticId ||
        item.idShort === slot.idShort ||
        (item.idShort || "").toLowerCase() === slot.kind,
    );
  }

  function findSlotFile(files, slot) {
    return (
      files.find((item) => item.semanticId === slot.semanticId) ||
      files.find((item) => item.idShort === slot.idShort) ||
      null
    );
  }

  function renderSlotRow(slot, item) {
    const pending = state.slotFiles[slot.kind];
    const missing = !pending && (!item || item.missing || !item.packagePath);
    const inputId = `doc-slot-${slot.kind}`;
    const fileLabel = pending
      ? `${pending.name} (${formatBytes(pending.size)}) · 첨부 대기`
      : missing
        ? "파일 없음"
        : item.fileName || "-";
    const input = el("input", {
      id: inputId,
      type: "file",
      accept: ACCEPT[slot.kind],
      hidden: true,
      onchange: (e) => setSlotFile(slot, e.target.files?.[0] || null, e.target),
    });
    const row = el(
      "li",
      {
        className: `doc-slot${missing && !pending ? " is-empty" : ""}`,
        title: "클릭하거나 파일을 끌어다 놓으세요",
      },
      el("span", { className: `doc-kind-badge ${slot.kind}` }, KIND_LABEL[slot.kind]),
      el(
        "span",
        { className: "doc-slot-identity" },
        el("span", { className: "doc-file-name" }, slot.idShort),
        el("span", { className: "doc-slot-semantic" }, slot.semanticId),
      ),
      el("span", { className: missing && !pending ? "doc-missing" : "muted" }, fileLabel),
      el("span", { className: "muted" }, pending || missing ? "" : formatBytes(item?.size)),
      !pending && item?.url
        ? el("a", { href: item.url, target: "_blank", rel: "noopener" }, "열기")
        : el("span", { className: "muted" }, ""),
      el("label", { className: "doc-slot-pick", for: inputId }, input, missing ? "파일 선택" : "바꾸기"),
    );
    bindSlotDrop(row, slot, input);
    return row;
  }

  function bindSlotDrop(row, slot, input) {
    row.addEventListener("dragover", (e) => {
      e.preventDefault();
      row.classList.add("dragover");
    });
    row.addEventListener("dragleave", () => row.classList.remove("dragover"));
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      row.classList.remove("dragover");
      setSlotFile(slot, e.dataTransfer?.files?.[0] || null);
    });
    row.addEventListener("click", (e) => {
      if (e.target.closest("a, input, label")) return;
      input.click();
    });
  }

  function renderExistingRow(item) {
    return el(
      "li",
      {},
      el("span", { className: `doc-kind-badge ${item.kind}` }, KIND_LABEL[item.kind] || "파일"),
      el("span", { className: "doc-file-name" }, item.idShort || item.fileName || "(이름 없음)"),
      el("span", { className: "muted" }, item.fileName || "-"),
      el("span", { className: "muted" }, formatBytes(item.size)),
      item.url ? el("a", { href: item.url, target: "_blank", rel: "noopener" }, "열기") : el("span", {}, ""),
      el("span", {}, ""),
    );
  }

  function setSlotFile(slot, file, input) {
    if (!file) {
      state.slotFiles[slot.kind] = null;
      if (state.docs) renderDocumentation(state.docs);
      else updateSubmitState();
      return;
    }
    const error = kindError(slot.kind, file.name);
    if (error) {
      window.alert(`${slot.idShort}: ${error}`);
      if (input) input.value = "";
      return;
    }
    state.slotFiles[slot.kind] = file;
    if (state.docs) renderDocumentation(state.docs);
    else updateSubmitState();
    showStatus("", false, true);
  }

  async function loadDocumentation(packageId, seed) {
    if (seed) {
      renderDocumentation(seed);
      if ((seed.patched || []).length) state.onSaved?.(seed);
      return seed;
    }
    $("doc-file-list").replaceChildren(el("p", { className: "muted" }, "불러오는 중..."));
    const docs = await fetchJson(`/api/packages/${encodeURIComponent(packageId)}/documentation`);
    renderDocumentation(docs);
    if ((docs.patched || []).length) state.onSaved?.(docs);
    return docs;
  }

  async function submitAttachments() {
    if (!state.packageId) return;
    const rows = readyRows();
    const slots = readySlots();
    if (!rows.length && !slots.length) return;
    for (const row of rows) {
      const name = sanitizeIdShort(row.name);
      if (!name) {
        window.alert(`${row.file.name}: 추가 에셋은 File 이름을 입력하세요. 메인 모델 슬롯 이름은 사용할 수 없습니다.`);
        return;
      }
      const reserved = reservedNameError(name);
      if (reserved) {
        window.alert(`${row.name || row.file.name}: ${reserved}`);
        return;
      }
      const error = kindError(row.kind, row.file.name);
      if (error) {
        window.alert(`${row.name || row.file.name}: ${error}`);
        return;
      }
    }
    const form = new FormData();
    for (const slot of slots) {
      const file = state.slotFiles[slot.kind];
      form.append("files", file, file.name);
      form.append("kinds", slot.kind);
      form.append("names", slot.idShort);
    }
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
