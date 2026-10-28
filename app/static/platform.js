"use strict";

const state = {
  view: "home",
  packages: [],
  selectedId: null,
  uploadFile: null,
  apiDocsLoaded: false,
  opcua: {
    connected: false,
    endpoint: "",
    selected: null,
    pollTimer: null,
    statusLoaded: false,
    catalog: { defaultServerId: "", servers: [] },
    subscribedNodeIds: [],
    expandedIds: new Set(),
    monitor: { mode: null, nodes: [], values: {}, title: "", truncated: false },
  },
};

function parseView() {
  const hash = (window.location.hash || "#home").replace(/^#/, "");
  if (hash === "api") return "api";
  if (hash === "utility" || hash.startsWith("utility/")) return "utility";
  return "home";
}

function showView(name) {
  state.view = name;
  $("view-home").hidden = name !== "home";
  $("view-utility").hidden = name !== "utility";
  $("view-api").hidden = name !== "api";
  $("home-actions").hidden = name !== "home";

  $("nav-home").classList.toggle("active", name === "home");
  $("nav-utility").classList.toggle("active", name === "utility");
  $("nav-utility-opcua").classList.toggle("active", name === "utility");
  $("nav-api").classList.toggle("active", name === "api");

  if (name === "utility" && !state.opcua.statusLoaded) {
    state.opcua.statusLoaded = true;
    loadOpcuaStatus();
  }
  if (name === "api" && !state.apiDocsLoaded) {
    state.apiDocsLoaded = true;
    $("api-docs-frame").src = "/docs";
  }
}

function applyHashView() {
  showView(parseView());
}

const $ = (id) => document.getElementById(id);

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

function formatBytes(size) {
  if (size == null) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString("ko-KR");
  } catch (_) {
    return iso;
  }
}

function updateActionButtons() {
  const hasSelection = Boolean(state.selectedId);
  $("download-aasx-btn").disabled = !hasSelection;
  $("download-artifacts-btn").disabled = !hasSelection;
  $("delete-btn").disabled = !hasSelection;
  $("opcua-read-package-btn").disabled = !hasSelection || !state.opcua.connected;
  const opcuaReady = state.opcua.connected;
  $("opcua-read-once-btn").disabled = !opcuaReady;
  $("opcua-subscribe-btn").disabled = !opcuaReady;
  $("opcua-monitor-stop-btn").disabled = state.opcua.monitor.mode !== "live";
}

async function loadPackages(reload = false) {
  try {
    state.packages = reload
      ? await fetchJson("/api/packages/reload", { method: "POST" })
      : await fetchJson("/api/packages");
  } catch (e) {
    $("package-tbody").replaceChildren(
      el("tr", {}, el("td", { colspan: "9", className: "error-text" }, `불러오기 실패: ${e.message}`)),
    );
    $("package-count").textContent = "오류";
    return;
  }
  if (state.selectedId && !state.packages.some((p) => p.id === state.selectedId)) {
    state.selectedId = null;
  }
  renderTable();
  updateActionButtons();
}

function renderTable() {
  $("package-count").textContent = `${state.packages.length}개 패키지`;
  if (!state.packages.length) {
    $("package-tbody").replaceChildren(
      el(
        "tr",
        {},
        el(
          "td",
          { colspan: "9", className: "muted" },
          "등록된 AASX 패키지가 없습니다. 'AAS 데이터 업로드'로 추가하세요.",
        ),
      ),
    );
    return;
  }

  const rows = state.packages.map((pkg) => {
    const selected = pkg.id === state.selectedId;
    const status = pkg.error
      ? el("span", { className: "status-badge error" }, "로드 실패")
      : el("span", { className: "status-badge ok" }, "정상");
    const artifacts = pkg.error
      ? "-"
      : `${pkg.artifactCount ?? 0}/3`;

    return el(
      "tr",
      {
        className: selected ? "selected" : "",
        onclick: (e) => {
          if (e.target.closest("input[type=radio], .aas-id")) return;
          openViewer(pkg.id);
        },
      },
      el(
        "td",
        {
          className: "col-check",
          onclick: (e) => e.stopPropagation(),
        },
        el("input", {
          type: "radio",
          name: "package-select",
          checked: selected,
          title: "작업 대상 선택",
          onchange: () => {
            state.selectedId = pkg.id;
            renderTable();
            updateActionButtons();
          },
        }),
      ),
      el(
        "td",
        {},
        el("div", { className: "pkg-name" }, pkg.fileName),
        el("div", { className: "muted" }, formatBytes(pkg.fileSize)),
      ),
      el("td", { className: "col-aas-id" }, renderShellIds(pkg.shells)),
      el("td", {}, String(pkg.counts?.shells ?? 0)),
      el("td", {}, String(pkg.counts?.submodels ?? 0)),
      el("td", {}, String(pkg.counts?.conceptDescriptions ?? 0)),
      el("td", {}, artifacts),
      el("td", {}, status, pkg.error ? el("div", { className: "error-text small" }, pkg.error) : null),
      el("td", { className: "muted" }, formatTime(pkg.loadedAt)),
    );
  });
  $("package-tbody").replaceChildren(...rows);
}

function renderShellIds(shells) {
  if (!shells?.length) return el("span", { className: "muted" }, "-");
  return el(
    "div",
    { className: "shell-ids" },
    shells.map((shell) =>
      el(
        "div",
        { className: "shell-id-row" },
        shell.idShort ? el("div", { className: "muted small" }, shell.idShort) : null,
        el(
          "code",
          {
            className: "aas-id",
            title: "클릭하여 복사",
            onclick: async (e) => {
              e.stopPropagation();
              try {
                await navigator.clipboard.writeText(shell.id);
                e.currentTarget.classList.add("copied");
                setTimeout(() => e.currentTarget.classList.remove("copied"), 900);
              } catch (_) {
                window.prompt("AAS id 복사:", shell.id);
              }
            },
          },
          shell.id,
        ),
      ),
    ),
  );
}

function openViewer(packageId) {
  window.location.href = `/viewer.html?package=${encodeURIComponent(packageId)}`;
}

function downloadSelected(kind) {
  if (!state.selectedId) return;
  const base = `/api/packages/${encodeURIComponent(state.selectedId)}`;
  const url = kind === "artifacts" ? `${base}/artifacts/download` : `${base}/download`;
  window.location.href = url;
}

async function deleteSelected() {
  if (!state.selectedId) return;
  const pkg = state.packages.find((p) => p.id === state.selectedId);
  const label = pkg?.fileName || state.selectedId;
  if (!window.confirm(`'${label}' 패키지와 OPC-UA 산출물을 삭제할까요?\n이 작업은 되돌릴 수 없습니다.`)) {
    return;
  }
  const packageId = state.selectedId;
  $("delete-btn").disabled = true;
  try {
    await fetchJson(`/api/packages/${encodeURIComponent(packageId)}`, { method: "DELETE" });
    state.selectedId = null;
    await loadPackages(false);
  } catch (e) {
    window.alert(`삭제 실패: ${e.message}`);
    updateActionButtons();
  }
}

// ---------------------------------------------------------------- upload

function setUploadFile(file) {
  if (!file) {
    state.uploadFile = null;
    $("dropzone-label").textContent = "여기에 AASX 파일을 놓거나 클릭하여 선택";
    $("upload-submit").disabled = true;
    return;
  }
  if (!file.name.toLowerCase().endsWith(".aasx")) {
    showUploadStatus("AASX(.aasx) 파일만 업로드할 수 있습니다.", true);
    state.uploadFile = null;
    $("upload-submit").disabled = true;
    return;
  }
  state.uploadFile = file;
  $("dropzone-label").textContent = `${file.name} (${formatBytes(file.size)})`;
  $("upload-submit").disabled = false;
  showUploadStatus("", false, true);
}

function showUploadStatus(message, isError = false, hide = false) {
  const node = $("upload-status");
  if (hide || !message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.className = `upload-status ${isError ? "error-text" : "ok-text"}`;
  node.textContent = message;
}

function openUploadDialog() {
  setUploadFile(null);
  showUploadStatus("", false, true);
  $("file-input").value = "";
  $("upload-dialog").showModal();
}

async function submitUpload() {
  if (!state.uploadFile) return;
  const form = new FormData();
  form.append("file", state.uploadFile, state.uploadFile.name);
  $("upload-submit").disabled = true;
  showUploadStatus("업로드 및 OPC-UA 산출물 생성 중...", false);
  try {
    const result = await fetchJson("/api/packages", { method: "POST", body: form });
    const pkg = result.package;
    showUploadStatus(
      `완료: ${pkg.fileName}` +
        (result.artifacts
          ? ` · 노드 ${result.artifacts.nodeCount}개 산출물 생성`
          : pkg.error
            ? ` · 로드 오류: ${pkg.error}`
            : ""),
      Boolean(pkg.error),
    );
    state.selectedId = pkg.id;
    await loadPackages(false);
    setTimeout(() => $("upload-dialog").close(), 900);
  } catch (e) {
    showUploadStatus(e.message, true);
    $("upload-submit").disabled = false;
  }
}

// ---------------------------------------------------------------- events

$("reload-btn").addEventListener("click", () => loadPackages(true));
$("upload-btn").addEventListener("click", openUploadDialog);
$("download-aasx-btn").addEventListener("click", () => downloadSelected("aasx"));
$("download-artifacts-btn").addEventListener("click", () => downloadSelected("artifacts"));
$("delete-btn").addEventListener("click", () => deleteSelected());
$("upload-submit").addEventListener("click", submitUpload);

$("file-input").addEventListener("change", (e) => {
  setUploadFile(e.target.files?.[0] || null);
});

const dropzone = $("dropzone");
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  setUploadFile(e.dataTransfer?.files?.[0] || null);
});

// ---------------------------------------------------------------- OPC-UA client

function showOpcuaMessage(message, isError = false, hide = false) {
  const node = $("opcua-message");
  if (hide || !message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.className = `upload-status ${isError ? "error-text" : "ok-text"}`;
  node.textContent = message;
}

function formatOpcuaValue(value) {
  if (value == null) return "null";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}

function catalogServer(serverId) {
  return (state.opcua.catalog.servers || []).find((item) => item.id === serverId) || null;
}

function renderOpcuaCatalog(catalog) {
  state.opcua.catalog = catalog || { defaultServerId: "", servers: [] };
  const select = $("opcua-server");
  const current = select.value;
  const servers = state.opcua.catalog.servers || [];
  select.replaceChildren(
    el("option", { value: "" }, "직접 입력"),
    ...servers.map((server) =>
      el("option", { value: server.id }, `${server.name || server.id} (${server.endpoint})`),
    ),
  );
  const next = current && catalogServer(current)
    ? current
    : state.opcua.catalog.defaultServerId && catalogServer(state.opcua.catalog.defaultServerId)
      ? state.opcua.catalog.defaultServerId
      : "";
  select.value = next;
  if (next) applyCatalogServer(next, { fillEmptyOnly: Boolean($("opcua-endpoint").value) });
}

function applyCatalogServer(serverId, { fillEmptyOnly = false } = {}) {
  const server = catalogServer(serverId);
  if (!server) return;
  if (!fillEmptyOnly || !$("opcua-endpoint").value) {
    $("opcua-endpoint").value = server.endpoint || "";
  }
}

function renderOpcuaStatus(status) {
  state.opcua.connected = Boolean(status.connected);
  state.opcua.endpoint = status.endpoint || "";
  state.opcua.subscribedNodeIds = status.subscribedNodeIds || [];
  const badge = $("opcua-badge");
  badge.textContent = status.connected ? "연결됨" : "미연결";
  badge.className = `status-badge ${status.connected ? "ok" : "warn"}`;
  const subscribed = status.subscribedNodeIds?.length || 0;
  const latest = status.latestCount || 0;
  const parts = status.connected
    ? [
        status.endpoint,
        status.namespaces?.length ? `ns ${status.namespaces.length}개` : "",
        subscribed ? `구독 ${subscribed}개` : "구독 없음",
        subscribed ? `최신 ${latest}` : "",
        status.catalogServerId ? `서버 ${status.catalogServerId}` : "",
      ]
    : [`기본 엔드포인트: ${status.endpoint || "-"}`];
  $("opcua-status-text").textContent = parts.filter(Boolean).join(" · ");
  if (!$("opcua-endpoint").value) {
    $("opcua-endpoint").value = status.endpoint || "";
  }
  if (status.catalogServerId && catalogServer(status.catalogServerId)) {
    $("opcua-server").value = status.catalogServerId;
  }
  $("opcua-connect-btn").disabled = false;
  $("opcua-disconnect-btn").disabled = !status.connected;
  updateActionButtons();
}

function opcuaTreeLabel(item) {
  return item.displayName || item.browseName || item.nodeId;
}

function setOpcuaExpanded(nodeId, open) {
  if (open) state.opcua.expandedIds.add(nodeId);
  else state.opcua.expandedIds.delete(nodeId);
}

function renderOpcuaChildren(container, items) {
  if (!items.length) {
    container.replaceChildren(el("p", { className: "muted" }, "하위 노드가 없습니다."));
    return;
  }
  const list = el(
    "ul",
    {},
    items.map((item) => {
      const expandable = item.nodeClass !== "Variable" && item.nodeClass !== "Method";
      const shouldOpen = expandable && state.opcua.expandedIds.has(item.nodeId);
      const li = el(
        "li",
        { className: expandable ? "collapsed" : "", "data-node-id": item.nodeId },
        el(
          "div",
          {
            className: "row",
            onclick: (e) => {
              e.stopPropagation();
              selectOpcuaNode(item, li.querySelector(".row"));
              if (expandable && !li.querySelector(":scope > ul, :scope > .muted")) {
                toggleOpcuaNode(li, item, { forceOpen: true });
              }
            },
          },
          el("span", {
            className: expandable ? "caret" : "caret leaf",
            onclick: (e) => {
              e.stopPropagation();
              if (expandable) toggleOpcuaNode(li, item);
            },
          }),
          el("span", { className: `badge ${item.nodeClass}` }, item.nodeClass || "Node"),
          el("span", { className: "label" }, opcuaTreeLabel(item)),
        ),
      );
      if (shouldOpen) queueMicrotask(() => toggleOpcuaNode(li, item, { forceOpen: true }));
      return li;
    }),
  );
  container.replaceChildren(list);
}

async function toggleOpcuaNode(li, item, { forceOpen = false } = {}) {
  if (li.dataset.loading === "1") return;
  const existing = li.querySelector(":scope > ul, :scope > .muted");
  if (existing) {
    if (forceOpen) {
      li.classList.remove("collapsed");
      setOpcuaExpanded(item.nodeId, true);
      return;
    }
    li.classList.toggle("collapsed");
    setOpcuaExpanded(item.nodeId, !li.classList.contains("collapsed"));
    return;
  }
  li.classList.remove("collapsed");
  setOpcuaExpanded(item.nodeId, true);
  li.dataset.loading = "1";
  const holder = el("p", { className: "muted" }, "불러오는 중...");
  li.append(holder);
  try {
    const result = await fetchJson(`/api/opcua/browse?nodeId=${encodeURIComponent(item.nodeId)}`);
    holder.remove();
    if (!result.children.length) {
      li.append(el("p", { className: "muted" }, "하위 노드가 없습니다."));
      return;
    }
    const childBox = el("div");
    renderOpcuaChildren(childBox, result.children);
    li.append(...childBox.childNodes);
  } catch (e) {
    holder.className = "error-text";
    holder.textContent = e.message;
  } finally {
    delete li.dataset.loading;
  }
}

async function loadOpcuaRoot() {
  const tree = $("opcua-tree");
  tree.replaceChildren(el("p", { className: "muted" }, "불러오는 중..."));
  try {
    const result = await fetchJson("/api/opcua/browse");
    renderOpcuaChildren(tree, result.children);
  } catch (e) {
    tree.replaceChildren(el("p", { className: "error-text" }, e.message));
  }
}

function selectOpcuaNode(item, row) {
  state.opcua.selected = item;
  $("opcua-tree").querySelectorAll(".row.selected").forEach((node) => node.classList.remove("selected"));
  if (row) row.classList.add("selected");
  renderOpcuaDetail(item);
}

function renderOpcuaDetail(item, live) {
  const writeValue = live && live.value != null ? formatOpcuaValue(live.value) : "";
  $("opcua-detail").replaceChildren(
    el("h3", {}, opcuaTreeLabel(item)),
    el(
      "table",
      {},
      el("tr", {}, el("th", {}, "NodeId"), el("td", {}, item.nodeId)),
      el("tr", {}, el("th", {}, "BrowseName"), el("td", {}, item.browseName || "-")),
      el("tr", {}, el("th", {}, "NodeClass"), el("td", {}, item.nodeClass || "-")),
      live
        ? el("tr", {}, el("th", {}, "Status"), el("td", {}, live.status || "-"))
        : null,
      live
        ? el("tr", {}, el("th", {}, "SourceTime"), el("td", {}, live.sourceTimestamp || "-"))
        : null,
    ),
    el("div", { className: "opcua-value" }, live ? formatOpcuaValue(live.value) : "한차례 읽기 또는 구독하면 아래 모니터에 값이 표시됩니다."),
    el(
      "div",
      { className: "opcua-write-row" },
      el("button", { type: "button", className: "btn-secondary", onclick: () => readOpcuaTargets(item) }, "한차례 읽기"),
      el("button", { type: "button", className: "btn-secondary", onclick: () => subscribeOpcuaTargets(item) }, "구독"),
    ),
    el(
      "div",
      { className: "opcua-write-row" },
      el("input", { id: "opcua-write-value", type: "text", value: writeValue, placeholder: "쓸 값" }),
      el("button", { type: "button", className: "btn-primary", onclick: () => writeOpcuaNode(item) }, "쓰기"),
    ),
  );
}

async function readOpcuaNode(item) {
  await readOpcuaTargets(item);
}

function parseWriteValue(raw) {
  const text = raw.trim();
  if (!text) return "";
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  if (/^-?\d+$/.test(text)) return Number(text);
  if (/^-?\d+\.\d+$/.test(text)) return Number(text);
  try {
    return JSON.parse(text);
  } catch (_) {
    return text;
  }
}

async function writeOpcuaNode(item) {
  const raw = $("opcua-write-value")?.value ?? "";
  try {
    await fetchJson("/api/opcua/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nodeId: item.nodeId, value: parseWriteValue(raw) }),
    });
    showOpcuaMessage(`쓰기 완료: ${item.nodeId}`, false);
    await readOpcuaTargets(item);
  } catch (e) {
    showOpcuaMessage(e.message, true);
  }
}

function stopOpcuaPoll() {
  if (state.opcua.pollTimer) {
    clearInterval(state.opcua.pollTimer);
    state.opcua.pollTimer = null;
  }
}

function formatOpcuaTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleTimeString("ko-KR");
  } catch (_) {
    return iso;
  }
}

function monitorNodeLabel(node) {
  const name = node.displayName || node.browseName || "";
  const generic = !name || name === node.nodeId || /^value[_-]?\d+$/i.test(name);
  if (!generic) return name;
  const id = node.nodeId || "";
  const tail = id.replace(/^ns=\d+;[is]=/i, "").split(/[\/.]/).filter(Boolean).slice(-3).join(".");
  return tail || name || id;
}

function subscribedAsMonitorNodes() {
  return (state.opcua.subscribedNodeIds || []).map((nodeId) => ({
    nodeId,
    browseName: nodeId,
    displayName: nodeId,
    nodeClass: "Variable",
  }));
}

async function resolveOpcuaVariables(item) {
  if (!item && state.opcua.subscribedNodeIds.length) {
    return { variables: subscribedAsMonitorNodes(), truncated: false, fromSubscription: true };
  }
  const qs = item?.nodeId ? `?nodeId=${encodeURIComponent(item.nodeId)}` : "";
  const result = await fetchJson(`/api/opcua/variables${qs}`);
  return { ...result, fromSubscription: false };
}

function valuesByNodeId(values) {
  const map = {};
  for (const item of values || []) {
    if (item?.nodeId) map[item.nodeId] = item;
  }
  return map;
}

function setMonitorMeta(text) {
  $("opcua-monitor-meta").textContent = text;
}

function renderOpcuaMonitor() {
  const monitor = state.opcua.monitor;
  const box = $("opcua-monitor");
  if (!monitor.nodes.length) {
    box.replaceChildren(el("p", { className: "muted" }, "아직 표시할 값이 없습니다."));
    setMonitorMeta("Variable까지 펼치지 않아도 됩니다. Object를 고르고 한차례 읽기 또는 구독을 누르세요.");
    updateActionButtons();
    return;
  }
  const modeLabel = monitor.mode === "live" ? "실시간 구독" : "한차례 읽기";
  const extra = monitor.truncated ? " · 일부만 표시(200개 제한)" : "";
  setMonitorMeta(`${modeLabel} · ${monitor.nodes.length}개${extra}${monitor.title ? ` · ${monitor.title}` : ""}`);
  box.replaceChildren(
    el(
      "table",
      {},
      el(
        "tr",
        {},
        el("th", {}, "이름"),
        el("th", {}, "값"),
        el("th", {}, "상태"),
        el("th", {}, "시각"),
        el("th", {}, "NodeId"),
      ),
      monitor.nodes.map((node) => {
        const live = monitor.values[node.nodeId] || {};
        return el(
          "tr",
          { "data-node-id": node.nodeId },
          el("td", {}, monitorNodeLabel(node)),
          el("td", { className: "value-cell" }, live.nodeId ? formatOpcuaValue(live.value) : "-"),
          el("td", {}, live.status || "-"),
          el("td", { className: "muted" }, formatOpcuaTime(live.sourceTimestamp)),
          el("td", { className: "muted" }, node.nodeId),
        );
      }),
    ),
  );
  updateActionButtons();
}

function updateOpcuaMonitorValues(values, { flash = false } = {}) {
  const next = { ...state.opcua.monitor.values, ...valuesByNodeId(values) };
  const prev = state.opcua.monitor.values;
  state.opcua.monitor.values = next;
  const table = $("opcua-monitor").querySelector("table");
  if (!table) {
    renderOpcuaMonitor();
    return;
  }
  for (const node of state.opcua.monitor.nodes) {
    const live = next[node.nodeId];
    if (!live) continue;
    const row = [...table.querySelectorAll("tr[data-node-id]")].find((el) => el.dataset.nodeId === node.nodeId);
    if (!row) continue;
    const cells = row.children;
    const nextValue = formatOpcuaValue(live.value);
    const changed = flash && prev[node.nodeId] && formatOpcuaValue(prev[node.nodeId].value) !== nextValue;
    cells[1].textContent = nextValue;
    cells[2].textContent = live.status || "-";
    cells[3].textContent = formatOpcuaTime(live.sourceTimestamp);
    row.classList.toggle("changed", Boolean(changed));
  }
}

function startOpcuaMonitorPoll() {
  stopOpcuaPoll();
  state.opcua.monitor.mode = "live";
  updateActionButtons();
  state.opcua.pollTimer = setInterval(async () => {
    if (state.opcua.monitor.mode !== "live") return;
    try {
      const result = await fetchJson("/api/opcua/values");
      updateOpcuaMonitorValues(result.values, { flash: true });
      const selected = state.opcua.selected;
      if (selected && result.values) {
        const live = result.values.find((item) => item.nodeId === selected.nodeId);
        if (live) renderOpcuaDetail(selected, live);
      }
    } catch (_) { /* ignore poll errors */ }
  }, 500);
}

function stopOpcuaMonitor() {
  stopOpcuaPoll();
  if (state.opcua.monitor.mode === "live") {
    state.opcua.monitor.mode = "read";
    renderOpcuaMonitor();
    showOpcuaMessage("실시간 모니터를 중지했습니다. 마지막 값은 유지됩니다.", false);
  }
  updateActionButtons();
}

function resetOpcuaMonitor() {
  stopOpcuaPoll();
  state.opcua.monitor = { mode: null, nodes: [], values: {}, title: "", truncated: false };
  renderOpcuaMonitor();
}

async function readOpcuaTargets(item = state.opcua.selected) {
  showOpcuaMessage("변수를 모아 한 번 읽는 중...", false);
  try {
    const resolved = await resolveOpcuaVariables(item);
    const nodes = resolved.variables || [];
    if (!nodes.length) {
      showOpcuaMessage("읽을 Variable이 없습니다. Object나 Variable을 선택하세요.", true);
      return;
    }
    const result = await fetchJson("/api/opcua/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nodeIds: nodes.map((node) => node.nodeId) }),
    });
    stopOpcuaPoll();
    state.opcua.monitor = {
      mode: "read",
      nodes,
      values: valuesByNodeId(result.values),
      title: item ? opcuaTreeLabel(item) : resolved.fromSubscription ? "현재 구독" : "Objects",
      truncated: Boolean(resolved.truncated),
    };
    renderOpcuaMonitor();
    if (item && result.values?.[0] && nodes.length === 1) {
      renderOpcuaDetail(item, result.values[0]);
    }
    showOpcuaMessage(`한차례 읽기 완료: ${nodes.length}개`, false);
  } catch (e) {
    showOpcuaMessage(e.message, true);
  }
}

async function subscribeOpcuaTargets(item = state.opcua.selected) {
  showOpcuaMessage("하위 Variable을 모아 구독하는 중...", false);
  try {
    const resolved = await resolveOpcuaVariables(item);
    const nodes = resolved.variables || [];
    if (!nodes.length) {
      showOpcuaMessage("구독할 Variable이 없습니다. Object나 Variable을 선택하세요.", true);
      return;
    }
    const nodeIds = nodes.map((node) => node.nodeId);
    const already = new Set(state.opcua.subscribedNodeIds);
    const missing = nodeIds.filter((id) => !already.has(id));
    if (missing.length) {
      const status = await fetchJson("/api/opcua/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodeIds: missing, replace: false }),
      });
      renderOpcuaStatus({ ...status, connected: true, endpoint: state.opcua.endpoint });
    }
    const snapshot = await fetchJson("/api/opcua/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nodeIds }),
    }).catch(() => ({ values: [] }));
    state.opcua.monitor = {
      mode: "live",
      nodes,
      values: valuesByNodeId(snapshot.values),
      title: item ? opcuaTreeLabel(item) : resolved.fromSubscription ? "현재 구독" : "Objects",
      truncated: Boolean(resolved.truncated),
    };
    renderOpcuaMonitor();
    startOpcuaMonitorPoll();
    if (item && snapshot.values?.[0] && nodes.length === 1) {
      renderOpcuaDetail(item, snapshot.values[0]);
    }
    showOpcuaMessage(`구독 모니터 시작: ${nodes.length}개 · Variable을 펼치지 않아도 값이 갱신됩니다.`, false);
  } catch (e) {
    showOpcuaMessage(e.message, true);
  }
}

async function subscribeOpcuaNode(item) {
  await subscribeOpcuaTargets(item);
}

async function loadOpcuaCatalog() {
  try {
    renderOpcuaCatalog(await fetchJson("/api/opcua/catalog"));
  } catch (e) {
    showOpcuaMessage(`카탈로그 조회 실패: ${e.message}`, true);
  }
}

async function loadOpcuaStatus() {
  try {
    await loadOpcuaCatalog();
    const status = await fetchJson("/api/opcua/status");
    renderOpcuaStatus(status);
    if (status.connected) await loadOpcuaRoot();
  } catch (e) {
    $("opcua-status-text").textContent = `상태 조회 실패: ${e.message}`;
    $("opcua-badge").textContent = "오류";
    $("opcua-badge").className = "status-badge error";
  }
}

async function connectOpcua() {
  $("opcua-connect-btn").disabled = true;
  showOpcuaMessage("접속 중...", false);
  const serverId = $("opcua-server").value.trim();
  const server = catalogServer(serverId);
  const body = {
    serverId: serverId || undefined,
    aasId: server?.aasId || undefined,
    packageId: state.selectedId || undefined,
    endpoint: $("opcua-endpoint").value.trim() || undefined,
    username: $("opcua-username").value.trim() || undefined,
    password: $("opcua-password").value || undefined,
  };
  try {
    const status = await fetchJson("/api/opcua/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderOpcuaStatus(status);
    const subscribed = status.subscribedNodeIds?.length || 0;
    if (status.subscribeError) {
      showOpcuaMessage(`접속됨: ${status.endpoint} · 구독 실패: ${status.subscribeError}`, true);
    } else {
      showOpcuaMessage(
        subscribed
          ? `접속됨: ${status.endpoint} · 매핑 ${subscribed}개 구독`
          : `접속됨: ${status.endpoint}`,
        false,
      );
    }
    if (!$("opcua-tree").querySelector("ul")) await loadOpcuaRoot();
  } catch (e) {
    showOpcuaMessage(e.message, true);
    $("opcua-connect-btn").disabled = false;
  }
}

async function disconnectOpcua() {
  stopOpcuaPoll();
  try {
    const status = await fetchJson("/api/opcua/disconnect", { method: "POST" });
    renderOpcuaStatus(status);
    state.opcua.selected = null;
    state.opcua.expandedIds = new Set();
    $("opcua-tree").replaceChildren(el("p", { className: "muted" }, "서버에 접속하면 Objects 하위 노드가 표시됩니다."));
    $("opcua-detail").replaceChildren(el("p", { className: "muted" }, "트리에서 노드를 선택하세요."));
    resetOpcuaMonitor();
    showOpcuaMessage("연결을 종료했습니다.", false);
  } catch (e) {
    showOpcuaMessage(e.message, true);
  }
}

async function readSelectedPackageNodes() {
  if (!state.selectedId) return;
  showOpcuaMessage("AAS 매핑 변수를 읽는 중...", false);
  try {
    const result = await fetchJson(`/api/opcua/packages/${encodeURIComponent(state.selectedId)}/read`, {
      method: "POST",
    });
    const rows = (result.values || []).map((item) =>
      el(
        "tr",
        {},
        el("td", {}, `${item.submodelIdShort || "-"}.${item.path || item.aasPath || "-"}`),
        el("td", {}, formatOpcuaValue(item.value)),
        el("td", {}, item.status || "-"),
        el("td", { className: "muted" }, item.nodeId || item.sourceLabel || "-"),
      ),
    );
    $("opcua-detail").replaceChildren(
      el("h3", {}, `${state.selectedId} AAS 매핑 값`),
      el(
        "table",
        {},
        el(
          "tr",
          {},
          el("th", {}, "AAS 경로"),
          el("th", {}, "값"),
          el("th", {}, "상태"),
          el("th", {}, "NodeId"),
        ),
        rows.length ? rows : el("tr", {}, el("td", { colspan: "4", className: "muted" }, "매핑된 값이 없습니다.")),
      ),
    );
    showOpcuaMessage(`AAS 매핑 ${result.values?.length || 0}개 읽기 완료`, false);
  } catch (e) {
    showOpcuaMessage(e.message, true);
  }
}

$("opcua-connect-btn").addEventListener("click", connectOpcua);
$("opcua-disconnect-btn").addEventListener("click", disconnectOpcua);
$("opcua-read-once-btn").addEventListener("click", () => readOpcuaTargets());
$("opcua-subscribe-btn").addEventListener("click", () => subscribeOpcuaTargets());
$("opcua-monitor-stop-btn").addEventListener("click", stopOpcuaMonitor);
$("opcua-read-package-btn").addEventListener("click", readSelectedPackageNodes);
$("opcua-server").addEventListener("change", () => {
  const serverId = $("opcua-server").value;
  if (serverId) applyCatalogServer(serverId);
});

window.addEventListener("hashchange", applyHashView);
if (!window.location.hash) {
  window.location.replace("#home");
}
applyHashView();
loadPackages();
