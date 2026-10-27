"use strict";

const PREFERRED_LANGS = ["ko", "en"];
const BADGES = {
  AssetAdministrationShell: "AAS",
  Submodel: "SM",
  SubmodelElementCollection: "SMC",
  SubmodelElementList: "SML",
  Property: "Prop",
  MultiLanguageProperty: "MLP",
  Range: "Range",
  File: "File",
  Blob: "Blob",
  ReferenceElement: "Ref",
  RelationshipElement: "Rel",
  AnnotatedRelationshipElement: "ARel",
  Operation: "Op",
  Capability: "Cap",
  Entity: "Ent",
  BasicEventElement: "Evt",
};

const state = {
  packages: [],
  packageId: null,
  tree: null,
  selectedRow: null,
  selectedNode: null,
  filter: "",
  liveTimer: null,
};

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

function pickLang(langStrings) {
  if (!langStrings) return null;
  for (const lang of PREFERRED_LANGS) if (langStrings[lang]) return langStrings[lang];
  const values = Object.values(langStrings);
  return values.length ? values[0] : null;
}

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      message = (await res.json()).detail || message;
    } catch (_) { /* not json */ }
    throw new Error(message);
  }
  return res.json();
}

const apiBase = (packageId) => `/api/packages/${encodeURIComponent(packageId)}`;

// ---------------------------------------------------------------- packages

function requestedPackageId() {
  return new URLSearchParams(window.location.search).get("package");
}

async function loadPackages(reload = false) {
  try {
    state.packages = reload
      ? await fetchJson("/api/packages/reload", { method: "POST" })
      : await fetchJson("/api/packages");
  } catch (e) {
    $("package-list").replaceChildren(el("li", { className: "error" }, `불러오기 실패: ${e.message}`));
    return;
  }
  renderPackageList();
  const requested = requestedPackageId();
  const current =
    state.packages.find((p) => p.id === state.packageId) ||
    state.packages.find((p) => p.id === requested) ||
    state.packages[0];
  if (current) {
    await selectPackage(current.id);
  } else {
    state.packageId = null;
    $("package-header").replaceChildren(
      el("p", { className: "muted" }, "등록된 AASX 패키지가 없습니다. 플랫폼에서 업로드하세요."),
      el("p", {}, el("a", { href: "/" }, "← 플랫폼으로 이동")),
    );
    $("tree").replaceChildren();
  }
}

function renderPackageList() {
  const items = state.packages.map((p) => {
    const meta = p.error
      ? `로드 실패: ${p.error}`
      : `AAS ${p.counts.shells} · SM ${p.counts.submodels} · CD ${p.counts.conceptDescriptions}`;
    return el(
      "li",
      {
        className: [p.id === state.packageId ? "active" : "", p.error ? "error" : ""].join(" ").trim(),
        title: p.fileName,
        onclick: () => selectPackage(p.id),
      },
      el("div", { className: "name" }, p.fileName),
      el("div", { className: "meta" }, meta),
    );
  });
  $("package-list").replaceChildren(...items);
}

async function selectPackage(packageId) {
  stopLiveRefresh();
  state.packageId = packageId;
  state.selectedRow = null;
  state.selectedNode = null;
  const url = new URL(window.location.href);
  url.searchParams.set("package", packageId);
  window.history.replaceState({}, "", url);
  renderPackageList();
  $("detail").replaceChildren(el("p", { className: "muted" }, "트리에서 항목을 선택하세요."));

  const summary = state.packages.find((p) => p.id === packageId);
  if (summary?.error) {
    state.tree = null;
    renderPackageHeader(summary);
    $("tree").replaceChildren(el("p", { className: "error-text" }, summary.error));
    return;
  }

  $("tree").replaceChildren(el("p", { className: "muted" }, "불러오는 중..."));
  try {
    state.tree = await fetchJson(`${apiBase(packageId)}/tree`);
  } catch (e) {
    $("tree").replaceChildren(el("p", { className: "error-text" }, e.message));
    return;
  }
  renderPackageHeader(state.tree.package);
  renderTree();
  if (window.LiveDashboard) LiveDashboard.resetHistory();
  await tryLoadLiveValues({ silent: true });
  startLiveRefresh();
}

function renderPackageHeader(pkg) {
  const props = pkg.coreProperties || {};
  const base = apiBase(pkg.id);
  $("package-header").replaceChildren(
    pkg.hasThumbnail ? el("img", { src: `${base}/thumbnail`, alt: "thumbnail" }) : null,
    el(
      "div",
      {},
      el("h3", {}, props.title || pkg.fileName),
      el("div", { className: "meta" }, pkg.fileName),
      props.creator ? el("div", { className: "meta" }, `작성자: ${props.creator}`) : null,
      props.modified ? el("div", { className: "meta" }, `수정일: ${props.modified}`) : null,
      pkg.error
        ? null
        : el(
            "div",
            { className: "meta" },
            el("a", { href: `${base}/environment`, target: "_blank" }, "표준 AAS JSON (Environment)"),
          ),
    ),
  );
}

// -------------------------------------------------------------------- tree

function childrenOf(node) {
  if (node.modelType === "AssetAdministrationShell") return node.submodels || [];
  return node.children || [];
}

function labelOf(node) {
  if (node.idShort) return node.idShort;
  const index = node.path?.match(/\[\d+\]$/);
  return index ? index[0] : node.id || node.path || "(이름 없음)";
}

function hasLiveValue(node) {
  return Object.prototype.hasOwnProperty.call(node, "liveValue");
}

function valuePreview(node) {
  if (hasLiveValue(node)) {
    return node.liveValue == null ? "" : String(node.liveValue);
  }
  switch (node.modelType) {
    case "Property":
    case "File":
    case "ReferenceElement":
      return node.value ?? "";
    case "MultiLanguageProperty":
      return pickLang(node.value) ?? "";
    case "Range":
      return `${node.min ?? ""} ~ ${node.max ?? ""}`;
    case "Blob":
      return `${node.size} bytes`;
    default: {
      if (node.missing) return "";
      const count = childrenOf(node).length;
      return count ? `${count}개 항목` : "";
    }
  }
}

function conceptOf(node) {
  return node.conceptId ? state.tree?.conceptDescriptions?.[node.conceptId] : null;
}

function matchesFilter(node, query) {
  const concept = conceptOf(node);
  const haystack = [
    labelOf(node),
    node.id,
    valuePreview(node),
    node.semanticId,
    pickLang(node.displayName),
    pickLang(concept?.preferredName),
  ];
  return haystack.some((text) => text && String(text).toLowerCase().includes(query));
}

function renderNode(node, depth, query) {
  const childNodes = childrenOf(node)
    .map((child) => renderNode(child, depth + 1, query))
    .filter(Boolean);
  if (query && !childNodes.length && !matchesFilter(node, query)) return null;

  const hasChildren = childrenOf(node).length > 0;
  const collapsed = !query && depth >= 2 && !node.hasLive;
  const li = el("li", { className: hasChildren && collapsed ? "collapsed" : "" });

  const caret = el("span", {
    className: hasChildren ? "caret" : "caret leaf",
    onclick: (e) => {
      e.stopPropagation();
      li.classList.toggle("collapsed");
    },
  });
  const displayName = pickLang(node.displayName);
  const row = el(
    "div",
    {
      className: "row",
      onclick: () => selectNode(node, row),
    },
    caret,
    el("span", { className: `badge ${node.modelType}`, title: node.modelType }, BADGES[node.modelType] || node.modelType),
    el("span", { className: "label" }, labelOf(node)),
    displayName && displayName !== node.idShort ? el("span", { className: "muted" }, `(${displayName})`) : null,
    node.missing ? el("span", { className: "missing" }, "패키지에 없음") : null,
    hasLiveValue(node)
      ? el("span", { className: "badge live", title: node.liveNodeId || "OPC-UA" }, "LIVE")
      : null,
    el("span", { className: hasLiveValue(node) ? "value live" : "value" }, valuePreview(node)),
  );
  if (node.chartKey) row.dataset.liveKey = node.chartKey;
  li.append(row);
  if (childNodes.length) li.append(el("ul", {}, childNodes));
  return li;
}

function renderGroup(title, nodes, query) {
  const childNodes = nodes.map((n) => renderNode(n, 1, query)).filter(Boolean);
  if (!childNodes.length) return null;
  const li = el("li");
  const row = el(
    "div",
    { className: "row", onclick: () => li.classList.toggle("collapsed") },
    el("span", { className: "caret" }),
    el("span", { className: "badge group" }, "그룹"),
    el("span", { className: "label" }, title),
  );
  li.append(row, el("ul", {}, childNodes));
  return li;
}

function renderTree() {
  if (!state.tree) return;
  const query = state.filter.trim().toLowerCase();
  const items = [
    ...state.tree.shells.map((shell) => renderNode(shell, 0, query)),
    renderGroup("AAS에 연결되지 않은 Submodel", state.tree.orphanSubmodels, query),
  ].filter(Boolean);
  $("tree").replaceChildren(
    items.length ? el("ul", {}, items) : el("p", { className: "muted" }, query ? "검색 결과가 없습니다." : "표시할 AAS가 없습니다."),
  );
}

function setAllCollapsed(collapsed) {
  document.querySelectorAll("#tree li").forEach((li) => {
    if (li.querySelector(":scope > ul")) li.classList.toggle("collapsed", collapsed);
  });
}

// ------------------------------------------------------------------ detail

function selectNode(node, row) {
  state.selectedRow?.classList.remove("selected");
  row.classList.add("selected");
  state.selectedRow = row;
  state.selectedNode = node;
  renderDetail(node);
}

function langRows(label, langStrings) {
  if (!langStrings) return [];
  return Object.entries(langStrings).map(([lang, text]) => [`${label} (${lang})`, text]);
}

function renderDetail(node) {
  const base = apiBase(state.packageId);
  const concept = conceptOf(node);
  const rows = [
    ["modelType", node.modelType],
    ["id", node.id],
    ["idShort", node.idShort],
    ["path", node.path],
    ...langRows("displayName", node.displayName),
    ...langRows("description", node.description),
    ["category", node.category],
    ["kind", node.kind],
    ["semanticId", node.semanticId],
    ["valueType", node.valueType],
  ];

  if (hasLiveValue(node)) {
    rows.push(
      ["liveValue", node.liveValue == null ? "null" : String(node.liveValue)],
      ["liveStatus", node.liveStatus],
      ["liveNodeId", node.liveNodeId],
      ["templateValue", node.value],
    );
  } else if (node.modelType === "MultiLanguageProperty") {
    rows.push(...langRows("value", node.value));
  } else if (node.modelType === "Range") {
    rows.push(["min", node.min], ["max", node.max]);
  } else if (node.modelType !== "AssetAdministrationShell" && node.modelType !== "Submodel") {
    rows.push(["value", node.value]);
  }

  rows.push(
    ["valueId", node.valueId],
    ["contentType", node.contentType],
    ["size", node.size != null ? `${node.size} bytes` : null],
    ["first", node.first],
    ["second", node.second],
    ["entityType", node.entityType],
    ["globalAssetId", node.globalAssetId],
    ["typeValueListElement", node.typeValueListElement],
    ["valueTypeListElement", node.valueTypeListElement],
    ["variableRole", node.variableRole],
    ["observed", node.observed],
    ["direction", node.direction],
    ["state", node.state],
    ["messageTopic", node.messageTopic],
  );

  for (const sid of node.specificAssetIds || []) rows.push([`specificAssetId`, `${sid.name} = ${sid.value}`]);
  for (const q of node.qualifiers || []) rows.push([`qualifier`, `${q.type} = ${q.value ?? ""}`]);

  if (node.administration) {
    rows.push(["version", node.administration.version], ["revision", node.administration.revision], ["templateId", node.administration.templateId]);
  }

  const asset = node.assetInformation;
  if (asset) {
    rows.push(["assetKind", asset.assetKind], ["globalAssetId", asset.globalAssetId], ["assetType", asset.assetType]);
    for (const sid of asset.specificAssetIds || []) rows.push([`specificAssetId`, `${sid.name} = ${sid.value}`]);
  }

  if (concept) {
    rows.push(
      ...langRows("개념 이름", concept.preferredName),
      ...langRows("개념 정의", concept.definition),
      ["단위", concept.unit],
      ["데이터 타입", concept.dataType],
    );
  }

  const links = [];
  if (node.modelType === "AssetAdministrationShell") {
    links.push(el("a", { href: `${base}/shells/${node.idBase64}`, target: "_blank" }, "표준 JSON"));
  } else if (node.modelType === "Submodel" && !node.missing) {
    links.push(
      el("a", { href: `${base}/submodels/${node.idBase64}`, target: "_blank" }, "표준 JSON"),
      el("a", { href: `${base}/submodels/${node.idBase64}/tree`, target: "_blank" }, "트리 JSON"),
    );
  }
  if (node.url) links.push(el("a", { href: node.url, target: "_blank" }, "파일 열기"));
  if (concept) {
    links.push(el("a", { href: `${base}/concept-descriptions/${concept.idBase64}`, target: "_blank" }, "Concept Description"));
  }

  const thumbnail = asset?.thumbnail?.url;
  const isImageFile = node.modelType === "File" && node.url && (node.contentType || "").startsWith("image/");

  $("detail").replaceChildren(
    el("h3", {}, labelOf(node)),
    links.length ? el("div", { className: "links" }, links) : null,
    thumbnail ? el("img", { src: thumbnail, alt: "asset thumbnail" }) : null,
    isImageFile ? el("img", { src: node.url, alt: node.idShort }) : null,
    el(
      "table",
      {},
      rows
        .filter(([, value]) => value != null && value !== "")
        .map(([key, value]) => el("tr", {}, el("th", {}, key), el("td", {}, String(value)))),
    ),
  );
}

// -------------------------------------------------------------------- init

function applyLiveValues(values) {
  const byKey = new Map();
  for (const item of values || []) {
    if (item.submodelIdShort && item.path) {
      byKey.set(`${item.submodelIdShort}\n${item.path}`, item);
    }
  }

  const walk = (node, submodelIdShort) => {
    const sm = node.modelType === "Submodel" ? node.idShort : submodelIdShort;
    delete node.liveValue;
    delete node.liveStatus;
    delete node.liveNodeId;
    delete node.hasLive;
    const live = byKey.get(`${sm}\n${node.path || ""}`);
    if (live) {
      node.liveValue = live.value;
      node.liveStatus = live.status;
      node.liveNodeId = live.nodeId || null;
      node.chartKey = `${sm}/${node.path}`;
      node.hasLive = true;
    } else {
      delete node.chartKey;
    }
    let childLive = false;
    for (const child of childrenOf(node)) {
      if (walk(child, sm)) childLive = true;
    }
    if (childLive) node.hasLive = true;
    return Boolean(node.hasLive);
  };

  for (const shell of state.tree?.shells || []) {
    for (const submodel of shell.submodels || []) walk(submodel, submodel.idShort);
  }
}

function setLiveStatus(message, isError = false) {
  const node = $("live-status");
  if (!node) return;
  node.textContent = message || "";
  node.className = isError ? "error-text" : "muted";
}

function stopLiveRefresh() {
  if (state.liveTimer) {
    clearTimeout(state.liveTimer);
    state.liveTimer = null;
  }
}

function applyLiveFrame(result) {
  applyLiveValues(result.values);
  if (document.querySelector("#tree .row[data-live-key]")) updateLiveRowTexts();
  else renderTree();
  if (state.selectedNode) renderDetail(state.selectedNode);
  if (result.frame) window.LiveDashboard?.ingest?.(result.frame);
  const good = (result.values || []).filter((item) => item.usable || item.isGood).length;
  setLiveStatus(`AAS 현장 값 ${good}/${result.values?.length || 0}개`);
}

async function pullLiveFrame() {
  return fetchJson(`/api/opcua/packages/${encodeURIComponent(state.packageId)}/live`);
}

function startLiveRefresh() {
  stopLiveRefresh();
  const tick = async () => {
    if (!state.packageId || !state.tree) return;
    try {
      applyLiveFrame(await pullLiveFrame());
    } catch (e) {
      setLiveStatus(e.message, true);
    }
    state.liveTimer = setTimeout(tick, 500);
  };
  tick();
}

async function tryLoadLiveValues({ silent = false } = {}) {
  if (!state.packageId || !state.tree) return;
  if (!silent) setLiveStatus("현장 값을 읽는 중...");
  try {
    const status = await fetchJson("/api/opcua/status");
    if (!status.connected) {
      stopLiveRefresh();
      setLiveStatus(silent ? "" : "OPC-UA에 먼저 접속하세요. 유틸리티 → OPC-UA", !silent);
      return;
    }
    applyLiveFrame(await pullLiveFrame());
    startLiveRefresh();
  } catch (e) {
    if (!silent) setLiveStatus(e.message, true);
  }
}

function updateLiveRowTexts() {
  document.querySelectorAll("#tree .row[data-live-key]").forEach((row) => {
    const key = row.dataset.liveKey;
    const node = findNodeByChartKey(key);
    const valueEl = row.querySelector(".value");
    if (node && valueEl) valueEl.textContent = node.liveValue == null ? "" : String(node.liveValue);
  });
}

function findNodeByChartKey(key) {
  let found = null;
  const walk = (node, submodelIdShort) => {
    const sm = node.modelType === "Submodel" ? node.idShort : submodelIdShort;
    if (`${sm}/${node.path || ""}` === key) found = node;
    childrenOf(node).forEach((child) => walk(child, sm));
  };
  for (const shell of state.tree?.shells || []) {
    for (const submodel of shell.submodels || []) walk(submodel, submodel.idShort);
  }
  return found;
}

function setLiveView(on) {
  const dash = window.LiveDashboard;
  if (!dash) {
    setLiveStatus("그래프 스크립트를 불러오지 못했습니다. 새로고침하세요.", true);
    return;
  }
  if (on) dash.show();
  else dash.hide();
  $("live-view-btn").textContent = on ? "그래프 닫기" : "실시간 그래프";
}

if (window.LiveDashboard) {
  LiveDashboard.init();
}

$("reload-btn").addEventListener("click", () => loadPackages(true));
$("expand-btn").addEventListener("click", () => setAllCollapsed(false));
$("collapse-btn").addEventListener("click", () => setAllCollapsed(true));
$("live-btn").addEventListener("click", () => tryLoadLiveValues());
$("live-view-btn").addEventListener("click", () => setLiveView(!window.LiveDashboard?.isVisible()));

let searchTimer;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.filter = e.target.value;
    state.selectedRow = null;
    renderTree();
  }, 200);
});

loadPackages();
