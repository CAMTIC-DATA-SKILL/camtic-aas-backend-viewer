"use strict";

// TimescaleDB 모니터링 탭. app.js 의 $, el, fetchJson, state(트리·선택 노드)를 그대로 쓴다.
// 100ms 저장과 화면 갱신을 분리한다: 스트림은 서버가 1초마다 묶어 보내고(SSE), 요약은 2초, DB 상태는 5초마다 읽는다.

(() => {
  const API = "/api/v1/timeseries";
  const RT_WINDOW_MS = 60_000;
  const COLORS = ["#1d63c9", "#d92d20", "#1f7a45", "#8a5a00", "#7a3fc0", "#0f7c8a", "#c2410c", "#475569"];

  const tsdb = {
    visible: false,
    equipment: [],
    nodesByEquipment: new Map(),
    timers: [],
    stream: null,
    streamPaused: false,
    streamRows: [],
    lastStreamRowAt: 0,
    rt: { equipment: "", nodes: [], chart: null, cursor: null, paused: false, data: new Map(), busy: false },
    hist: { chart: null },
    linked: null, // AAS 에서 고른 Property 에 연결된 노드
  };

  // ---------------------------------------------------------------- 공통

  const fmtTime = (iso) => {
    if (!iso) return "-";
    const d = new Date(iso);
    const pad = (n, w = 2) => String(n).padStart(w, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  };
  const fmtNum = (v, digits = 3) => {
    if (v == null) return "-";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("ko-KR") : v.toFixed(digits);
    return String(v);
  };
  const fmtBytes = (b) => (b == null ? "-" : b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
  const sourceBadge = (source) =>
    el("span", { className: "badge real" }, source || "REAL");
  const qualityTag = (q) => el("span", { className: `q-tag ${q}` }, q);
  const nodeLabel = (n) => `${n.node_name}${n.unit ? ` (${n.unit})` : ""}`;

  function showError(message) {
    const box = $("tsdb-error");
    box.hidden = !message;
    box.textContent = message || "";
  }

  async function api(path, options) {
    try {
      const result = await fetchJson(`${API}${path}`, options);
      showError("");
      return result;
    } catch (e) {
      showError(`TimescaleDB API 오류: ${e.message}`);
      throw e;
    }
  }

  function every(ms, fn) {
    fn();
    tsdb.timers.push(setInterval(() => tsdb.visible && fn(), ms));
  }

  function fillSelect(select, items, { value, label, empty } = {}) {
    const current = select.value;
    select.replaceChildren(
      ...(empty ? [el("option", { value: "" }, empty)] : []),
      ...items.map((item) => el("option", { value: value(item) }, label(item))),
    );
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  }

  // ---------------------------------------------------------------- 탭 전환

  function setTab(name) {
    const tsdbOn = name === "tsdb";
    $("layout").hidden = tsdbOn;
    $("tsdb-view").hidden = !tsdbOn;
    $("tab-aasx").setAttribute("aria-selected", String(!tsdbOn));
    $("tab-tsdb").setAttribute("aria-selected", String(tsdbOn));
    if (tsdbOn) start();
    else stop();
    if (location.hash !== (tsdbOn ? "#tsdb" : "")) history.replaceState(null, "", tsdbOn ? "#tsdb" : location.pathname + location.search);
  }

  async function start() {
    tsdb.visible = true;
    try {
      await loadEquipment();
    } catch {
      return;
    }
    await applyAasSelection();
    if (!tsdb.timers.length) {
      every(2000, refreshSummary);
      every(5000, refreshDbStatus);
      every(1000, refreshRealtime);
      every(2000, refreshAlarms);
      every(2000, refreshNodes);
    }
    openStream();
    if (!tsdb.hist.loadedOnce) runHistory();
  }

  function stop() {
    tsdb.visible = false;
    closeStream();
  }

  // ---------------------------------------------------------------- 설비·노드

  async function loadEquipment() {
    tsdb.equipment = await api("/equipment");
    const nodes = await api("/nodes");
    tsdb.nodesByEquipment = new Map();
    for (const n of nodes) {
      if (!tsdb.nodesByEquipment.has(n.equipment_id)) tsdb.nodesByEquipment.set(n.equipment_id, []);
      tsdb.nodesByEquipment.get(n.equipment_id).push(n);
    }
    const label = (e) => `${e.equipment_id} · ${e.equipment_name} [${e.data_source || "REAL"}]`;
    fillSelect($("stream-equipment"), tsdb.equipment, { value: (e) => e.equipment_id, label, empty: "전체" });
    fillSelect($("rt-equipment"), tsdb.equipment, { value: (e) => e.equipment_id, label });
    fillSelect($("hist-equipment"), tsdb.equipment, { value: (e) => e.equipment_id, label });
    fillSelect($("node-equipment"), tsdb.equipment, { value: (e) => e.equipment_id, label });
    if (!$("node-equipment").dataset.touched) {
      const real = tsdb.equipment.find((e) => e.data_source === "REAL");
      if (real) $("node-equipment").value = real.equipment_id;  // 처음에는 현장 설비 노드 상태를 보여 준다
      $("node-equipment").dataset.touched = "1";
    }
    refreshStreamNodes();
    renderNodePicker("rt");
    renderNodePicker("hist");
  }

  function numericNodes(equipmentId) {
    return (tsdb.nodesByEquipment.get(equipmentId) || []).filter((n) => !["String", "DateTime"].includes(n.data_type));
  }

  function renderNodePicker(kind, preselect) {
    const equipmentId = $(`${kind}-equipment`).value;
    const box = $(`${kind}-nodes`);
    const before = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
    const nodes = kind === "rt" ? numericNodes(equipmentId) : tsdb.nodesByEquipment.get(equipmentId) || [];
    let chosen = preselect ? new Set(preselect) : before;
    if (![...chosen].some((id) => nodes.some((n) => n.node_id === id))) {
      chosen = new Set(numericNodes(equipmentId).slice(0, 3).map((n) => n.node_id));
    }
    box.replaceChildren(
      ...nodes.map((n) =>
        el("label", { title: n.node_id },
          el("input", { type: "checkbox", value: n.node_id, checked: chosen.has(n.node_id) ? "checked" : null,
                        onchange: () => (kind === "rt" ? resetRealtime() : null) }),
          nodeLabel(n)),
      ),
    );
    if (!nodes.length) box.append(el("span", { className: "muted" }, "활성 노드가 없습니다."));
  }

  const pickedNodes = (kind) => [...$(`${kind}-nodes`).querySelectorAll("input:checked")].map((i) => i.value);

  // ---------------------------------------------------------------- AAS Property 연결

  function selectedProperty() {
    // app.js 의 state 는 최상위 const 라 window 에 없다. 같은 전역 스코프에서 이름으로 읽는다.
    const viewer = typeof state !== "undefined" ? state : null;
    const node = viewer?.selectedNode;
    if (!node || !viewer.tree) return null;
    for (const shell of viewer.tree.shells || []) {
      for (const submodel of shell.submodels || []) {
        if (submodel === node) return { node, submodel, path: null };
        const stack = [...(submodel.children || [])];
        while (stack.length) {
          const item = stack.pop();
          if (item === node) return { node, submodel, path: node.path };
          stack.push(...(item.children || []));
        }
      }
    }
    return { node, submodel: null, path: null };
  }

  async function applyAasSelection() {
    const box = $("tsdb-link");
    const picked = selectedProperty();
    tsdb.linked = null;
    if (!picked || !picked.path || picked.node.modelType !== "Property") {
      box.className = "tsdb-banner";
      box.replaceChildren(
        "AASX Viewer 에서 Property(예: RobotKinematicState › JointPosition › J1)를 고른 뒤 이 탭을 열면, 연결된 TimescaleDB 노드를 자동으로 선택합니다.",
      );
      return;
    }
    const where = `${picked.submodel.idShort} › ${picked.path}`;
    let result;
    try {
      result = await api(`/mapping?submodel_id=${encodeURIComponent(picked.submodel.id)}&path=${encodeURIComponent(picked.path)}`);
    } catch {
      return;
    }
    if (!result.nodes.length) {
      box.className = "tsdb-banner warn";
      box.replaceChildren("AAS 선택: ", el("b", {}, where), " — TimescaleDB 노드에 매핑되지 않았습니다 (저장 대상이 아니거나 등록되지 않은 Property).");
      return;
    }
    const target = result.nodes[0];
    if (!target.enabled) {
      box.className = "tsdb-banner warn";
      box.replaceChildren("AAS 선택: ", el("b", {}, where), " → ", el("b", {}, target.equipment_id), " ", el("code", {}, target.node_id),
        " — 매핑은 있지만 비활성 노드라 수집하지 않습니다 (현장 미지원).");
      return;
    }
    tsdb.linked = target;
    box.className = "tsdb-banner";
    box.replaceChildren(
      "AAS 선택: ", el("b", {}, where), " → ",
      ...result.nodes.flatMap((n, i) => [
        i ? ", " : "",
        sourceBadge(n.data_source), " ", el("b", {}, n.equipment_id), " ", el("code", {}, n.node_id),
        n.received_at ? ` (마지막 수신 ${fmtTime(n.received_at)})` : " (수신 기록 없음)",
      ]),
      result.nodes.length > 1 ? " — 지금 수신 중인 실제(REAL) 설비를 우선 선택했습니다." : "",
    );
    for (const kind of ["rt", "hist"]) {
      $(`${kind}-equipment`).value = target.equipment_id;
      renderNodePicker(kind, [target.node_id]);
    }
    $("stream-equipment").value = target.equipment_id;
    $("node-equipment").value = target.equipment_id;
    refreshNodes();
    refreshStreamNodes();
    $("stream-node").value = target.node_id;
    resetRealtime();
    openStream();
    runHistory();
  }

  // ---------------------------------------------------------------- 요약 카드

  async function refreshSummary() {
    let s;
    try {
      s = await api("/summary");
    } catch {
      renderCards(null);
      return;
    }
    renderCards(s);
  }

  function renderCards(s) {
    const card = (k, v, sub, tone) =>
      el("div", { className: `tsdb-card ${tone || ""}` }, el("span", { className: "k" }, k),
        el("span", { className: "v" }, v), el("span", { className: "s" }, sub || " "));
    if (!s) {
      $("tsdb-cards").replaceChildren(card("DB 연결", "끊김", "DB 연결 설정 확인", "bad"));
      return;
    }
    const ratio = s.target_rows_per_s_receiving ? s.actual_rows_per_s / s.target_rows_per_s_receiving : null;
    $("tsdb-cards").replaceChildren(
      card("전체 설비", s.equipment_total, `REAL ${s.equipment_by_source.REAL || 0}`),
      card("로봇", s.equipment_by_type.ROBOT),
      card("PLC", s.equipment_by_type.PLC),
      card("AMR", s.equipment_by_type.AMR),
      card("전체 노드", s.nodes_total, "활성 노드"),
      card("정상 수집 노드", `${s.nodes_receiving_good} / ${s.nodes_total}`, `수신 중 ${s.nodes_receiving}`,
        s.nodes_receiving_good === s.nodes_total ? "ok" : ""),
      card("최근 1분 수집", fmtNum(s.samples_last_minute), "DB 저장 행"),
      card("실제 초당 수집", `${s.actual_rows_per_s}/s`,
        `목표 ${s.target_rows_per_s_receiving}/s (수신 노드) · 전체 ${s.target_rows_per_s_all_nodes}/s`,
        ratio == null ? "" : ratio >= 0.95 ? "ok" : "bad"),
      card("DB 연결", s.db_connected ? "정상" : "끊김", "TimescaleDB", s.db_connected ? "ok" : "bad"),
      (() => {
        const a = s.active_alarms || {};
        const total = (a.CRITICAL || 0) + (a.WARNING || 0) + (a.INFO || 0);
        return card("진행 중 알람", total, `CRITICAL ${a.CRITICAL || 0} · WARNING ${a.WARNING || 0}`, total ? "bad" : "ok");
      })(),
    );
  }

  // ---------------------------------------------------------------- 실시간 스트림 (SSE)

  function refreshStreamNodes() {
    const equipmentId = $("stream-equipment").value;
    const nodes = equipmentId ? tsdb.nodesByEquipment.get(equipmentId) || [] : [];
    fillSelect($("stream-node"), nodes, { value: (n) => n.node_id, label: nodeLabel, empty: "전체" });
  }

  function openStream() {
    closeStream();
    if (!tsdb.visible) return;
    const params = new URLSearchParams({ limit: $("stream-limit").value });
    if ($("stream-source").value) params.set("data_source", $("stream-source").value);
    if ($("stream-equipment").value) params.set("equipment_id", $("stream-equipment").value);
    if ($("stream-node").value) params.set("node_id", $("stream-node").value);
    tsdb.streamRows = [];
    tsdb.lastStreamRowAt = Date.now();
    renderStream();
    const source = new EventSource(`${API}/stream?${params}`);
    tsdb.stream = source;
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (!payload.db_connected) {
        setStreamBadge("DB 끊김", "error");
        return;
      }
      if (payload.rows.length) {
        tsdb.lastStreamRowAt = Date.now();
        if (!tsdb.streamPaused) {
          tsdb.streamRows = keepNewest([...payload.rows, ...tsdb.streamRows], Number($("stream-limit").value));
          renderStream();
        }
      }
      const idle = (Date.now() - tsdb.lastStreamRowAt) / 1000;
      if (idle > 3) setStreamBadge(`수신 중단 ${Math.round(idle)}초`, "warn");
      else setStreamBadge(tsdb.streamPaused ? "일시정지" : "수신 중", tsdb.streamPaused ? "warn" : "ok");
      $("stream-meta").textContent = `· 서버 ${fmtTime(payload.server_time)} · 이번 갱신 ${payload.rows.length}행`;
    };
    source.onerror = () => setStreamBadge("재연결 중", "warn");
  }

  // 표시 건수를 설비별로 나눈다. 행이 많은 설비가 다른 설비 행을 밀어내지 않게.
  function keepNewest(rows, limit) {
    const byEquipment = new Map();
    for (const row of rows) {
      if (!byEquipment.has(row.equipment_id)) byEquipment.set(row.equipment_id, []);
      byEquipment.get(row.equipment_id).push(row);
    }
    const quota = Math.max(1, Math.floor(limit / Math.max(1, byEquipment.size)));
    return [...byEquipment.values()]
      .flatMap((list) => list.slice(0, quota))
      .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at))
      .slice(0, limit);
  }

  function closeStream() {
    if (tsdb.stream) tsdb.stream.close();
    tsdb.stream = null;
  }

  function setStreamBadge(text, tone) {
    const badge = $("stream-badge");
    badge.textContent = text;
    badge.className = `status-badge ${tone}`;
  }

  function renderStream() {
    const rows = tsdb.streamRows.map((r) =>
      el("tr", { className: r.quality_class === "BAD" ? "q-bad" : r.quality_class === "UNCERTAIN" ? "q-uncertain" : "" },
        el("td", { className: "num" }, fmtTime(r.received_at)),
        el("td", {}, sourceBadge(r.data_source), " ", r.equipment_id),
        el("td", {}, r.equipment_type),
        el("td", { title: r.node_id }, r.node_name),
        el("td", { className: "num" }, r.value == null ? "(값 없음)" : fmtNum(r.value)),
        el("td", {}, r.unit || ""),
        el("td", {}, qualityTag(r.quality_class), r.quality ? ` 0x${(r.quality >>> 0).toString(16).toUpperCase()}` : ""),
      ),
    );
    $("stream-body").replaceChildren(
      ...(rows.length ? rows : [el("tr", {}, el("td", { colspan: "7", className: "muted" }, "새로 저장된 데이터가 없습니다. OPC-UA 수집 상태(노드 상태 패널)를 확인하세요."))]),
    );
  }

  // ---------------------------------------------------------------- 실시간 그래프

  function resetRealtime() {
    tsdb.rt.cursor = null;
    tsdb.rt.data = new Map();
    if (tsdb.rt.chart) {
      tsdb.rt.chart.data.datasets = [];
      tsdb.rt.chart.update("none");
    }
    refreshRealtime();
  }

  function ensureChart(kind, canvasId) {
    if (tsdb[kind].chart) return tsdb[kind].chart;
    tsdb[kind].chart = new Chart($(canvasId), {
      type: "line",
      data: { datasets: [] },
      options: {
        animation: false,
        parsing: false,
        normalized: true,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: { type: "linear", ticks: { maxTicksLimit: 8, callback: (v) => fmtTime(v).slice(0, 8) } },
          y: { ticks: { maxTicksLimit: 6 } },
        },
        elements: { point: { radius: 0 }, line: { borderWidth: 1.5 } },
        plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } },
                   tooltip: { callbacks: { title: (items) => fmtTime(items[0]?.parsed.x) } } },
      },
    });
    return tsdb[kind].chart;
  }

  async function refreshRealtime() {
    const rt = tsdb.rt;
    const equipmentId = $("rt-equipment").value;
    const nodes = pickedNodes("rt");
    if (!tsdb.visible || rt.paused || rt.busy || !equipmentId || !nodes.length) return;
    rt.busy = true;
    try {
      const now = Date.now();
      const start = rt.cursor ? new Date(rt.cursor) : new Date(now - RT_WINDOW_MS);
      const params = new URLSearchParams({ equipment_id: equipmentId, start: start.toISOString(), mode: "raw", limit: "10000" });
      nodes.forEach((n) => params.append("node_id", n));
      const result = await api(`/history?${params}`);
      for (const series of result.series) {
        const points = rt.data.get(series.node_id) || [];
        for (const s of series.samples || []) {
          const t = Date.parse(s.ts);
          if (typeof s.value === "number" && (!points.length || t > points[points.length - 1].x)) points.push({ x: t, y: s.value });
          if (!rt.cursor || t > Date.parse(rt.cursor)) rt.cursor = s.ts;
        }
        while (points.length && points[0].x < now - RT_WINDOW_MS) points.shift();
        rt.data.set(series.node_id, points);
      }
      const byId = new Map((tsdb.nodesByEquipment.get(equipmentId) || []).map((n) => [n.node_id, n]));
      const chart = ensureChart("rt", "rt-chart");
      chart.data.datasets = nodes.map((id, i) => ({
        label: byId.get(id) ? nodeLabel(byId.get(id)) : id,
        data: rt.data.get(id) || [],
        borderColor: COLORS[i % COLORS.length],
        backgroundColor: COLORS[i % COLORS.length],
      }));
      chart.options.scales.x.min = now - RT_WINDOW_MS;
      chart.options.scales.x.max = now;
      chart.update("none");
    } catch {
      // 오류 배너는 api() 가 보여 준다
    } finally {
      rt.busy = false;
    }
  }

  // ---------------------------------------------------------------- 과거 데이터

  function historyRange() {
    const value = $("hist-range").value;
    if (value === "custom") {
      const start = $("hist-start").value ? new Date($("hist-start").value) : null;
      const end = $("hist-end").value ? new Date($("hist-end").value) : new Date();
      return start ? { start, end } : null;
    }
    const end = new Date();
    return { start: new Date(end.getTime() - Number(value) * 1000), end };
  }

  function historyParams() {
    const range = historyRange();
    const nodes = pickedNodes("hist");
    if (!range || !nodes.length) return null;
    // 1분 이하는 원시 데이터 전체(노드당 최대 10,000행), 그보다 길면 노드당 600점으로 다운샘플한다.
    const raw = range.end - range.start <= 60_000;
    const params = new URLSearchParams({ equipment_id: $("hist-equipment").value,
      start: range.start.toISOString(), end: range.end.toISOString(),
      mode: raw ? "raw" : "bucket", ...(raw ? { limit: "10000" } : { points: "600" }) });
    nodes.forEach((n) => params.append("node_id", n));
    return params;
  }

  async function runHistory() {
    const params = historyParams();
    if (!params) {
      $("hist-meta").textContent = "노드와 기간을 선택하세요.";
      return;
    }
    tsdb.hist.loadedOnce = true;
    $("hist-csv").href = `${API}/history.csv?${params}`;
    const started = performance.now();
    let result;
    try {
      result = await api(`/history?${params}`);
    } catch {
      return;
    }
    const elapsed = Math.round(performance.now() - started);
    const chart = ensureChart("hist", "hist-chart");
    const datasets = [];
    result.series.forEach((series, i) => {
      const color = COLORS[i % COLORS.length];
      const label = nodeLabel(series);
      if (result.mode === "raw") {
        datasets.push({ label, borderColor: color, backgroundColor: color,
          data: (series.samples || []).filter((s) => typeof s.value === "number").map((s) => ({ x: Date.parse(s.ts), y: s.value })) });
      } else {
        const buckets = series.buckets || [];
        const xy = (key) => buckets.filter((b) => b[key] != null).map((b) => ({ x: Date.parse(b.bucket), y: b[key] }));
        datasets.push({ label: `${label} 평균`, borderColor: color, backgroundColor: color, data: xy("avg") });
        // 최대선에서 다음 데이터셋(최소선)까지 채워 구간 범위를 보여 준다.
        datasets.push({ label: `${label} 최대`, borderColor: `${color}66`, backgroundColor: `${color}22`,
          borderWidth: 1, fill: "+1", data: xy("max") });
        datasets.push({ label: `${label} 최소`, borderColor: `${color}66`, backgroundColor: `${color}22`,
          borderWidth: 1, data: xy("min") });
      }
    });
    chart.data.datasets = datasets;
    chart.options.scales.x.min = Date.parse(result.start);
    chart.options.scales.x.max = Date.parse(result.end);
    chart.options.scales.x.ticks.callback = (v) => {
      const d = new Date(v);
      return Date.parse(result.end) - Date.parse(result.start) > 86_400_000 / 2
        ? `${d.getMonth() + 1}/${d.getDate()} ${fmtTime(v).slice(0, 5)}` : fmtTime(v).slice(0, 8);
    };
    chart.update("none");
    $("hist-stats").replaceChildren(...result.series.map((s) =>
      el("tr", {}, el("td", { title: s.node_id }, nodeLabel(s)),
        el("td", { className: "num" }, fmtNum(s.summary.avg)), el("td", { className: "num" }, fmtNum(s.summary.min)),
        el("td", { className: "num" }, fmtNum(s.summary.max)), el("td", { className: "num" }, fmtNum(s.summary.count)))));
    const total = result.series.reduce((n, s) => n + (s.summary.count || 0), 0);
    const source = `${result.data_source || "REAL"}(현장) 데이터`;
    $("hist-meta").textContent = `${source} · ` + (result.mode === "raw"
      ? `원시 데이터 · ${total.toLocaleString("ko-KR")}행 · 조회 ${elapsed}ms${result.next_cursor ? " · 1,000행씩 잘려 표시됨" : ""}`
      : `다운샘플 ${result.bucket_seconds.toFixed(1)}초 구간 (평균 + 최소~최대) · 원본 ${total.toLocaleString("ko-KR")}행 · 조회 ${elapsed}ms`);
  }

  // ---------------------------------------------------------------- DB 상태

  async function refreshDbStatus() {
    let s;
    try {
      s = await api("/db-status");
    } catch {
      return;
    }
    const badge = $("db-badge");
    badge.textContent = s.db_connected ? "연결됨" : "연결 끊김";
    badge.className = `status-badge ${s.db_connected ? "ok" : "error"}`;
    const h = s.hypertable || {};
    const sub = s.opcua_subscription || {};
    const rec = s.opcua_recorder || {};
    const q = s.latest_quality || {};
    const st = s.storage || {};
    const perf = s.ingest_performance || {};
    const al = s.alarm_engine || {};
    const rows = s.db_connected ? [
      ["DB", `${s.database} · PostgreSQL ${s.postgresql_version}`],
      ["TimescaleDB 확장", `${s.timescaledb_version} (license: ${s.timescaledb_license})`],
      ["Hypertable", `${h.name} · chunk ${h.chunk_interval} · ${h.chunks}개 (압축 ${h.compressed_chunks})`],
      ["저장 공간", `${fmtBytes(h.size_bytes)} · 행 약 ${fmtNum(h.approx_rows)} (근사) · DB 전체 ${fmtBytes(h.database_bytes)}`],
      ...(st.total_bytes ? [
        ["크기 구성", `테이블 ${fmtBytes(st.table_bytes)} · 인덱스 ${fmtBytes(st.index_bytes)} · TOAST ${fmtBytes(st.toast_bytes)}`],
        ["압축", st.compression ? `chunk ${st.compression.compressed_chunks}/${st.compression.chunks} 압축 · ${fmtBytes(st.compression.before_bytes)} → ${fmtBytes(st.compression.after_bytes)} (${st.compression.ratio}배)` : "-"],
        ["증가량", `최근 1시간 ${fmtNum(st.rows_last_1h)}행 (약 ${fmtBytes(st.growth_bytes_1h_estimate)}) · 최근 24시간 ${fmtNum(st.rows_last_24h)}행 · 이 속도면 하루 약 ${fmtBytes(st.growth_bytes_per_day_estimate)} (비압축, 행당 ${st.uncompressed_bytes_per_row} B)`],
      ] : []),
      ["정책", (s.policies || []).map((p) => `${p.proc_name} (${p.every})`).join(", ") + " · 자동 삭제(보관 기간) 없음"],
      ["적재 성능", perf.recent_batches ? `배치 ${perf.recent_batches}개 · 저장 시간 p50 ${perf.batch_ms_p50}ms / p95 ${perf.batch_ms_p95}ms · ${perf.rows_per_s}행/s · DB 사용률 ${Math.round((perf.db_busy_ratio || 0) * 100)}%` : "-"],
      ["알람 판정", al.enabled ? `규칙 ${al.rules}개 · 판정 ${fmtNum(al.evaluations)}회 · 발생 ${al.opened} · 복구 ${al.cleared}${al.last_error ? ` · 오류 ${al.last_error.slice(0, 80)}` : ""}` : "꺼짐 (MFG_ALARMS_ENABLED)"],
      ["마지막 저장 수신 시각", s.last_write_at ? `${fmtTime(s.last_write_at)} (${s.since_last_write_s}초 전)` : "-"],
      ["최근 1분 저장", `${fmtNum(s.samples_last_minute)}행 · ${s.rows_per_s}/s · `
        + Object.entries(s.samples_last_minute_by_source || {}).map(([k, v]) => `${k} ${fmtNum(v)}`).join(" · ")],
      ["수신 지연 (중앙값)", Object.entries(s.receive_delay_median_ms_by_source || {}).map(([k, v]) =>
        `${k} ${v} ms${v < 0 ? " (장비 시계가 이 PC 보다 빠름)" : ""}`).join(" · ") || "-"],
      ["최신값 품질", `GOOD ${q.good} · UNCERTAIN ${q.uncertain} · BAD ${q.bad}`],
      ["OPC-UA 구독", sub.endpoint ? `${sub.connected ? "연결" : "끊김"} · ${sub.endpoint} · 구독 ${sub.subscribed_nodes} / 수신 ${sub.receiving_nodes}` : "-"],
      ["구독 주기", sub.requestedPublishingMs ? `publishing ${sub.requestedPublishingMs}ms → 승인 ${sub.revisedPublishingMs}ms · sampling ${sub.requestedSamplingMs}ms → 승인 ${sub.revisedSamplingMs}ms · queue ${sub.requestedQueueSize} → 승인 ${sub.revisedQueueSize}` : "-"],
      ["현장 기록 (REAL)", rec.enabled ? `노드 ${rec.mapped_nodes} · 저장 ${fmtNum(rec.inserted)} · 대기 ${rec.pending} · 실제 간격 중앙값 ${rec.interval_median_ms ?? "-"} ms` : "꺼짐 (MFG_RECORD_OPCUA)"],
    ] : [["오류", s.error || "-"]];
    $("db-status").replaceChildren(...rows.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v))));
  }

  // ---------------------------------------------------------------- 알람

  async function refreshAlarms() {
    let result;
    try {
      result = await api(`/alarms?status=${$("alarm-filter").value}&limit=100`);
    } catch {
      return;
    }
    const active = Object.values(result.active).reduce((a, b) => a + b, 0);
    const badge = $("alarm-badge");
    badge.textContent = active ? `진행 중 ${active}건` : "진행 중 알람 없음";
    badge.className = `status-badge ${active ? "error" : "ok"}`;
    const rows = result.alarms.map((a) => {
      const open = a.status !== "CLEARED";
      const duration = a.ended_at ? ` (${((Date.parse(a.ended_at) - Date.parse(a.started_at)) / 1000).toFixed(1)}초)` : "";
      return el("tr", { className: open ? "alarm-active" : "" },
        el("td", {}, open ? el("b", {}, "진행 중") : "복구"),
        el("td", {}, el("span", { className: `sev-tag ${a.severity}` }, a.severity)),
        el("td", {}, a.alarm_type),
        el("td", {}, sourceBadge(a.data_source), " ", a.equipment_id),
        el("td", { title: a.node_id || "" }, a.node_name || "(설비 전체)"),
        el("td", { className: "wrap" }, a.message),
        el("td", { className: "num" }, fmtTime(a.started_at)),
        el("td", { className: "num" }, a.ended_at ? `${fmtTime(a.ended_at)}${duration}` : "-"));
    });
    $("alarm-body").replaceChildren(...(rows.length ? rows
      : [el("tr", {}, el("td", { colspan: "8", className: "muted" }, "기록된 알람이 없습니다."))]));
  }

  // ---------------------------------------------------------------- 노드 상태

  async function refreshNodes() {
    const equipmentId = $("node-equipment").value;
    if (!equipmentId) return;
    let nodes;
    try {
      nodes = await api(`/node-status?equipment_id=${encodeURIComponent(equipmentId)}`);
    } catch {
      return;
    }
    const count = (state) => nodes.filter((n) => n.state === state).length;
    $("node-meta").textContent = `수신 중 ${count("RECEIVING")} · 미수집 ${count("STALE")} · 현장 미지원 ${count("UNSUPPORTED")} · 비활성 ${count("DISABLED")} · 데이터 없음 ${count("NO_DATA")} / ${nodes.length}`;
    $("node-body").replaceChildren(...nodes.map((n) =>
      el("tr", {},
        el("td", { title: n.aas_submodel_id || "" }, n.aas_path || n.node_name),
        el("td", { className: "num" }, n.node_id),
        el("td", {}, n.data_type),
        el("td", {}, n.unit || ""),
        el("td", {}, el("span", { className: `state-tag ${n.state}` }, n.state_label)),
        el("td", { className: "num" }, n.value == null ? "-" : fmtNum(n.value)),
        el("td", { className: "num" }, n.received_at ? fmtTime(n.received_at) : "-"),
        el("td", { className: "num" }, n.interval_median_ms == null ? "-" : `${n.interval_median_ms} ms`),
        el("td", { className: "num" }, n.revised_sampling_ms == null ? "-" : `${n.revised_sampling_ms} ms · q ${n.revised_queue_size}`))));
  }

  // ---------------------------------------------------------------- 이벤트

  $("tab-aasx").addEventListener("click", () => setTab("aasx"));
  $("tab-tsdb").addEventListener("click", () => setTab("tsdb"));
  $("stream-equipment").addEventListener("change", () => { refreshStreamNodes(); openStream(); });
  $("stream-node").addEventListener("change", openStream);
  $("stream-limit").addEventListener("change", openStream);
  $("stream-source").addEventListener("change", openStream);
  $("alarm-filter").addEventListener("change", refreshAlarms);
  $("node-equipment").addEventListener("change", refreshNodes);
  $("stream-pause").addEventListener("click", () => {
    tsdb.streamPaused = !tsdb.streamPaused;
    $("stream-pause").textContent = tsdb.streamPaused ? "재개" : "일시정지";
  });
  $("rt-equipment").addEventListener("change", () => { renderNodePicker("rt"); resetRealtime(); });
  $("rt-pause").addEventListener("click", () => {
    tsdb.rt.paused = !tsdb.rt.paused;
    $("rt-pause").textContent = tsdb.rt.paused ? "재개" : "일시정지";
    if (!tsdb.rt.paused) resetRealtime();
  });
  $("hist-equipment").addEventListener("change", () => renderNodePicker("hist"));
  $("hist-range").addEventListener("change", () => { $("hist-custom").hidden = $("hist-range").value !== "custom"; });
  $("hist-run").addEventListener("click", runHistory);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) closeStream();
    else if (tsdb.visible) openStream();
  });

  window.TsdbMonitor = { setTab };
  if (location.hash === "#tsdb") setTab("tsdb");
})();
