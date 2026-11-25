"use strict";

// 로컬 MCAP 아카이브 탭.
// 스키마·인코딩·채널·청크·토픽 부분 읽기·그래프를 같은 파일에서 확인한다.

(() => {
  const API = "/api/mcap";
  const COLORS = ["#1d63c9", "#d92d20", "#1f7a45", "#8a5a00", "#7a3fc0", "#0f7c8a", "#c2410c", "#475569"];
  const MAX_PICK = 8;
  const STORAGE_KEY = "camtic.mcap.selectedFile";

  const mcap = {
    visible: false,
    panel: "overview",
    status: null,
    inspect: null,
    files: [],
    filter: "",
    selectedFile: "",
    channels: [],
    selected: new Set(),
    chart: null,
    busy: false,
  };

  const fmtBytes = (b) => {
    if (b == null) return "-";
    if (b > 1e9) return `${(b / 1e9).toFixed(2)} GB`;
    if (b > 1e6) return `${(b / 1e6).toFixed(1)} MB`;
    if (b > 1e3) return `${Math.round(b / 1024)} KB`;
    return `${b} B`;
  };
  const fmtNum = (v) => {
    if (v == null) return "-";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("ko-KR") : v.toFixed(3);
    return String(v);
  };
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("ko-KR") : "-");
  const fmtIsoLocal = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    const pad = (n, w = 2) => String(n).padStart(w, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  const localInputToIso = (value) => {
    if (!value) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  };
  const shortTopic = (topic) => topic.replace(/^\/+/, "").split("/").slice(-3).join("/");
  const fileLabel = (name) => name.split("/").pop() || name;
  const payloadText = (msg) => {
    if (msg.payload != null) return JSON.stringify(msg.payload);
    return msg.payload_text || "";
  };

  function showError(message) {
    const box = $("mcap-error");
    box.hidden = !message;
    box.textContent = message || "";
  }

  async function api(path) {
    try {
      const result = await fetchJson(`${API}${path}`);
      showError("");
      return result;
    } catch (e) {
      showError(`MCAP API 오류: ${e.message}`);
      throw e;
    }
  }

  function withFile(path) {
    if (!mcap.selectedFile) return path;
    const join = path.includes("?") ? "&" : "?";
    return `${path}${join}file=${encodeURIComponent(mcap.selectedFile)}`;
  }

  function rememberFile(name) {
    mcap.selectedFile = name || "";
    try {
      if (name) localStorage.setItem(STORAGE_KEY, name);
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }

  function rememberedFile() {
    try {
      return localStorage.getItem(STORAGE_KEY) || "";
    } catch {
      return "";
    }
  }

  function setPanel(name) {
    mcap.panel = name;
    document.querySelectorAll(".mcap-subtabs [data-mcap-panel]").forEach((btn) => {
      btn.setAttribute("aria-selected", String(btn.dataset.mcapPanel === name));
    });
    document.querySelectorAll("[data-mcap-panel-body]").forEach((panel) => {
      panel.hidden = panel.dataset.mcapPanelBody !== name;
    });
  }

  function renderCards(status) {
    const selected = status.selected || "";
    const selectedLabel = status.exists
      ? fileLabel(selected)
      : (selected ? "오류" : "미선택");
    const cards = [
      { k: "선택 파일", v: selectedLabel, s: selected && selected !== selectedLabel ? selected : "", title: selected || selectedLabel, file: true },
      { k: "크기", v: fmtBytes(status.size_bytes) },
      { k: "스키마", v: status.schemas?.toLocaleString("ko-KR") || "0" },
      { k: "채널", v: status.channels?.toLocaleString("ko-KR") || "0" },
      { k: "청크", v: status.chunks?.toLocaleString("ko-KR") || "0" },
      { k: "메시지", v: status.messages?.toLocaleString("ko-KR") || "0" },
    ];
    $("mcap-cards").replaceChildren(
      ...cards.map((card) =>
        el("div", {
          className: `tsdb-card${status.exists ? " ok" : ""}${card.file ? " mcap-file-card" : ""}`,
          title: card.title || undefined,
        },
          el("span", { className: "k" }, card.k),
          el("span", { className: "v", title: card.title || String(card.v) }, card.v),
          card.s ? el("span", { className: "s", title: card.s }, card.s) : null)),
    );
    const banner = $("mcap-banner");
    if (status.error) {
      banner.className = "tsdb-banner error";
      banner.textContent = status.error;
    } else if (!selected) {
      banner.className = "tsdb-banner";
      banner.replaceChildren(
        document.createTextNode("아카이브 "),
        el("code", { title: status.archive_dir || "-" }, status.archive_dir || "-"),
        document.createTextNode(" · 왼쪽에서 파일을 선택하세요."),
      );
    } else {
      banner.className = "tsdb-banner";
      banner.replaceChildren(
        document.createTextNode("아카이브 "),
        el("code", { title: status.archive_dir || "-" }, status.archive_dir || "-"),
        document.createTextNode(" · 파일 "),
        el("code", { title: selected }, selected),
        document.createTextNode(" · 스키마/청크/부분 읽기를 확인하세요."),
      );
    }
  }

  function renderFileList() {
    const needle = mcap.filter.trim().toLowerCase();
    const rows = mcap.files.filter((f) => !needle || f.name.toLowerCase().includes(needle));
    $("mcap-archive-meta").textContent = `${mcap.files.length}개`;
    if (!rows.length) {
      $("mcap-file-list").replaceChildren(el("p", { className: "muted" }, needle ? "검색 결과 없음" : "아카이브에 .mcap 이 없습니다."));
      return;
    }
    $("mcap-file-list").replaceChildren(
      ...rows.map((file) =>
        el("button", {
          type: "button",
          className: "mcap-file-item",
          "aria-selected": String(file.name === mcap.selectedFile),
          title: file.path,
          onclick: () => selectFile(file.name),
        },
          el("span", { className: "name" }, file.name),
          el("span", { className: "meta" }, `${fmtBytes(file.size_bytes)} · ${fmtTime(file.mtime)}`))),
    );
  }

  function preferredTopics(channels) {
    const prefer = [
      "JointSpeed/J1", "JointSpeed/J2", "JointSpeed/J3",
      "TCPPosition/Y", "JointPosition/J5", "TCPOrientation/Ry",
    ];
    const picked = [];
    for (const key of prefer) {
      const hit = channels.find((c) => c.topic.endsWith(key) && c.message_count > 0);
      if (hit) picked.push(hit.topic);
      if (picked.length >= 3) break;
    }
    if (picked.length) return picked;
    return [...channels].filter((c) => c.message_count > 0)
      .sort((a, b) => b.message_count - a.message_count)
      .slice(0, 3)
      .map((c) => c.topic);
  }

  function syncTopicSelection() {
    renderChannelPicker();
    renderChannelTable();
  }

  function toggleTopic(topic, checked) {
    if (checked) {
      if (mcap.selected.size >= MAX_PICK) {
        showError(`채널은 최대 ${MAX_PICK}개까지 고를 수 있습니다.`);
        syncTopicSelection();
        return;
      }
      mcap.selected.add(topic);
    } else {
      mcap.selected.delete(topic);
    }
    showError("");
    syncTopicSelection();
  }

  function renderChannelPicker() {
    const box = $("mcap-channels");
    if (!mcap.channels.length) {
      box.replaceChildren(el("span", { className: "muted" }, "채널 없음"));
      return;
    }
    box.replaceChildren(
      ...mcap.channels.map((ch) =>
        el("label", { className: "tsdb-check", title: ch.topic },
          el("input", {
            type: "checkbox",
            checked: mcap.selected.has(ch.topic),
            disabled: ch.message_count <= 0,
            onchange: (ev) => toggleTopic(ch.topic, ev.target.checked),
          }),
          `${ch.node_name || shortTopic(ch.topic)}${ch.unit ? ` (${ch.unit})` : ""} · ${ch.message_count.toLocaleString("ko-KR")}`)),
    );
  }

  function renderChannelTable() {
    $("mcap-channel-meta").textContent = `${mcap.channels.length} channels · selected ${mcap.selected.size}`;
    $("mcap-channel-body").replaceChildren(
      ...mcap.channels.map((ch) =>
        el("tr", {},
          el("td", { className: "num" }, ch.id),
          el("td", { className: "wrap", title: ch.topic }, ch.topic),
          el("td", {}, ch.message_encoding),
          el("td", {}, ch.schema_name || "-"),
          el("td", {}, ch.schema_encoding || "-"),
          el("td", {}, ch.unit || ""),
          el("td", { className: "num" }, ch.message_count.toLocaleString("ko-KR")),
          el("td", {},
            el("input", {
              type: "checkbox",
              checked: mcap.selected.has(ch.topic),
              disabled: ch.message_count <= 0,
              onchange: (ev) => toggleTopic(ch.topic, ev.target.checked),
            })))),
    );
  }

  function renderOverview(inspect) {
    if (!inspect) {
      $("mcap-overview-kv").replaceChildren();
      $("mcap-overview-meta").textContent = "";
      return;
    }
    const stats = inspect.statistics || {};
    $("mcap-overview-meta").textContent = `${fmtTime(inspect.start)} ~ ${fmtTime(inspect.end)}`;
    const rows = [
      ["path", inspect.path],
      ["size", fmtBytes(inspect.size_bytes)],
      ["messages", fmtNum(stats.message_count)],
      ["channels", fmtNum(stats.channel_count)],
      ["schemas", fmtNum(stats.schema_count)],
      ["chunks", fmtNum(stats.chunk_count)],
      ["metadata", fmtNum(stats.metadata_count)],
      ["attachments", fmtNum(stats.attachment_count)],
      ["compression", (inspect.compression || []).join(", ") || "-"],
    ];
    $("mcap-overview-kv").replaceChildren(
      ...rows.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v))),
    );
  }

  function fillCustomRange(status) {
    if (!status?.end) return;
    const end = new Date(status.end);
    const start = new Date(end.getTime() - 5 * 60 * 1000);
    $("mcap-start").value = fmtIsoLocal(start.toISOString());
    $("mcap-end").value = fmtIsoLocal(end.toISOString());
  }

  function selectedTopicsOrError() {
    if (!mcap.selectedFile) {
      showError("왼쪽에서 MCAP 파일을 선택하세요.");
      return null;
    }
    if (!mcap.selected.size) {
      showError("채널을 하나 이상 선택하세요.");
      return null;
    }
    return [...mcap.selected];
  }

  function appendTopics(params, topics) {
    for (const topic of topics) params.append("topic", topic);
  }

  function plotQueryParams() {
    const params = new URLSearchParams();
    params.set("file", mcap.selectedFile);
    appendTopics(params, [...mcap.selected]);
    params.set("points", $("mcap-points").value);
    if ($("mcap-range").value === "custom") {
      const start = localInputToIso($("mcap-start").value);
      const end = localInputToIso($("mcap-end").value);
      if (!start || !end) throw new Error("사용자 지정 기간을 입력하세요.");
      params.set("start", start);
      params.set("end", end);
    } else {
      params.set("range_s", $("mcap-range").value);
    }
    return params;
  }

  function drawChart(payload) {
    const datasets = payload.series.map((series, index) => ({
      label: `${series.node_name || shortTopic(series.topic)}${series.unit ? ` (${series.unit})` : ""}`,
      data: series.samples.map((s) => ({ x: new Date(s.ts).getTime(), y: typeof s.value === "number" ? s.value : null })),
      borderColor: COLORS[index % COLORS.length],
      backgroundColor: COLORS[index % COLORS.length],
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0,
      spanGaps: true,
    }));
    if (mcap.chart) mcap.chart.destroy();
    mcap.chart = new Chart($("mcap-chart"), {
      type: "line",
      data: { datasets },
      options: {
        animation: false,
        parsing: false,
        normalized: true,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: {
            type: "linear",
            min: Date.parse(payload.start),
            max: Date.parse(payload.end),
            ticks: {
              maxTicksLimit: 8,
              callback: (v) => {
                const d = new Date(v);
                const pad = (n) => String(n).padStart(2, "0");
                return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
              },
            },
          },
          y: { ticks: { maxTicksLimit: 6 } },
        },
        elements: { point: { radius: 0 }, line: { borderWidth: 1.5 } },
        plugins: {
          legend: { labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              title: (items) => new Date(items[0]?.parsed.x || 0).toLocaleString("ko-KR"),
            },
          },
        },
      },
    });
  }

  function renderStats(payload) {
    $("mcap-stats").replaceChildren(
      ...payload.series.map((series) => {
        const nums = series.samples.map((s) => s.value).filter((v) => typeof v === "number");
        return el("tr", {},
          el("td", { title: series.topic }, series.node_name || shortTopic(series.topic)),
          el("td", {}, series.unit || ""),
          el("td", { className: "num" }, series.count_raw.toLocaleString("ko-KR")),
          el("td", { className: "num" }, series.samples.length.toLocaleString("ko-KR")),
          el("td", { className: "num" }, fmtNum(nums.length ? Math.min(...nums) : null)),
          el("td", { className: "num" }, fmtNum(nums.length ? Math.max(...nums) : null)));
      }),
    );
    $("mcap-meta").textContent =
      `${payload.overlapping_chunks} chunks · ${payload.elapsed_ms} ms · ${fmtTime(payload.start)} ~ ${fmtTime(payload.end)}`;
  }

  function clearPlot() {
    if (mcap.chart) {
      mcap.chart.destroy();
      mcap.chart = null;
    }
    $("mcap-stats").replaceChildren();
    $("mcap-meta").textContent = "";
    $("mcap-channels").replaceChildren();
    $("mcap-channel-body").replaceChildren();
    $("mcap-chunks-body").replaceChildren();
    $("mcap-messages-body").replaceChildren();
    $("mcap-chunks-meta").textContent = "";
    $("mcap-messages-meta").textContent = "";
    renderOverview(null);
  }

  async function runPlot() {
    if (mcap.busy) return;
    const topics = selectedTopicsOrError();
    if (!topics) return;
    mcap.busy = true;
    $("mcap-run").disabled = true;
    try {
      const payload = await api(`/series?${plotQueryParams()}`);
      drawChart(payload);
      renderStats(payload);
    } catch (e) {
      if (e.message && !e.message.startsWith("MCAP")) showError(e.message);
    } finally {
      mcap.busy = false;
      $("mcap-run").disabled = false;
    }
  }

  async function runChunks() {
    if (mcap.busy) return;
    if (!mcap.selectedFile) {
      showError("왼쪽에서 MCAP 파일을 선택하세요.");
      return;
    }
    mcap.busy = true;
    $("mcap-chunks-run").disabled = true;
    try {
      const params = new URLSearchParams();
      params.set("file", mcap.selectedFile);
      params.set("range_s", $("mcap-chunks-range").value);
      params.set("limit", $("mcap-chunks-limit").value);
      for (const topic of mcap.selected) params.append("topic", topic);
      const payload = await api(`/chunks?${params}`);
      $("mcap-chunks-meta").textContent =
        `matched ${payload.matched_chunks.toLocaleString("ko-KR")} / total ${payload.total_chunks.toLocaleString("ko-KR")} · show ${payload.returned}`;
      $("mcap-chunks-body").replaceChildren(
        ...payload.chunks.map((chunk) =>
          el("tr", {},
            el("td", { className: "num" }, chunk.index),
            el("td", {}, chunk.compression),
            el("td", { className: "num" }, fmtBytes(chunk.compressed_size)),
            el("td", { className: "num" }, fmtBytes(chunk.uncompressed_size)),
            el("td", { className: "num" }, chunk.compression_ratio == null ? "-" : `${chunk.compression_ratio}x`),
            el("td", { className: "num" }, fmtTime(chunk.start)),
            el("td", { className: "num" }, fmtTime(chunk.end)),
            el("td", { className: "num" }, chunk.channel_ids.length),
            el("td", { className: "wrap", title: chunk.topics.join("\n") },
              chunk.topics.slice(0, 3).map(shortTopic).join(", ") + (chunk.topics.length > 3 ? ` +${chunk.topics.length - 3}` : "")))),
      );
    } catch {
      /* showError already */
    } finally {
      mcap.busy = false;
      $("mcap-chunks-run").disabled = false;
    }
  }

  async function runMessages() {
    if (mcap.busy) return;
    const topics = selectedTopicsOrError();
    if (!topics) return;
    mcap.busy = true;
    $("mcap-messages-run").disabled = true;
    try {
      const params = new URLSearchParams();
      params.set("file", mcap.selectedFile);
      params.set("range_s", $("mcap-messages-range").value);
      params.set("limit", $("mcap-messages-limit").value);
      appendTopics(params, topics);
      const payload = await api(`/messages?${params}`);
      $("mcap-messages-meta").textContent =
        `${payload.returned}${payload.truncated ? "+" : ""} msgs · ${payload.elapsed_ms} ms · topics ${payload.topics.length}`;
      $("mcap-messages-body").replaceChildren(
        ...payload.messages.map((msg) =>
          el("tr", {},
            el("td", { className: "num" }, fmtTime(msg.log_time)),
            el("td", { title: msg.topic }, shortTopic(msg.topic)),
            el("td", {}, msg.message_encoding),
            el("td", {}, msg.schema_name || "-"),
            el("td", { className: "num" }, fmtBytes(msg.data_bytes)),
            el("td", { className: "mcap-payload", title: payloadText(msg) }, payloadText(msg)))),
      );
    } catch {
      /* showError already */
    } finally {
      mcap.busy = false;
      $("mcap-messages-run").disabled = false;
    }
  }

  async function loadSelected() {
    if (!mcap.selectedFile) {
      clearPlot();
      renderCards({ exists: false, selected: "", archive_dir: mcap.status?.archive_dir });
      return;
    }
    mcap.status = await api(withFile("/status"));
    renderCards(mcap.status);
    renderFileList();
    if (!mcap.status.exists) {
      clearPlot();
      return;
    }
    mcap.inspect = await api(withFile("/inspect"));
    mcap.channels = mcap.inspect.channels || [];
    mcap.selected = new Set(preferredTopics(mcap.channels));
    renderOverview(mcap.inspect);
    syncTopicSelection();
    fillCustomRange(mcap.status);
    $("mcap-chunks-body").replaceChildren();
    $("mcap-messages-body").replaceChildren();
    $("mcap-chunks-meta").textContent = "청크 조회를 누르면 시간·토픽으로 걸러진 청크 인덱스를 봅니다.";
    $("mcap-messages-meta").textContent = "메시지 읽기를 누르면 선택 토픽만 부분 조회합니다.";
    if (mcap.panel === "plot" && mcap.selected.size) await runPlot();
    if (mcap.panel === "chunks") await runChunks();
    if (mcap.panel === "messages" && mcap.selected.size) await runMessages();
  }

  async function selectFile(name) {
    if (!name || name === mcap.selectedFile) return;
    rememberFile(name);
    renderFileList();
    try {
      await loadSelected();
    } catch {
      /* showError already */
    }
  }

  async function refreshFiles() {
    mcap.files = await api("/files");
    const names = new Set(mcap.files.map((f) => f.name));
    let chosen = mcap.selectedFile || rememberedFile() || "";
    if (chosen && !names.has(chosen)) chosen = "";
    rememberFile(chosen);
    renderFileList();
  }

  async function start() {
    mcap.visible = true;
    setPanel(mcap.panel || "overview");
    try {
      mcap.status = await api("/status");
      renderCards(mcap.status);
      if (!mcap.status.enabled || !mcap.status.archive_exists) return;
      await refreshFiles();
      if (mcap.selectedFile) await loadSelected();
      else clearPlot();
    } catch {
      /* showError already */
    }
  }

  function stop() {
    mcap.visible = false;
  }

  document.querySelectorAll(".mcap-subtabs [data-mcap-panel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      setPanel(btn.dataset.mcapPanel);
      if (!mcap.selectedFile) return;
      try {
        if (btn.dataset.mcapPanel === "chunks") await runChunks();
        if (btn.dataset.mcapPanel === "messages") await runMessages();
        if (btn.dataset.mcapPanel === "plot" && mcap.selected.size && !mcap.chart) await runPlot();
      } catch {
        /* showError already */
      }
    });
  });
  $("mcap-range")?.addEventListener("change", () => {
    $("mcap-custom").hidden = $("mcap-range").value !== "custom";
  });
  $("mcap-run")?.addEventListener("click", runPlot);
  $("mcap-chunks-run")?.addEventListener("click", runChunks);
  $("mcap-messages-run")?.addEventListener("click", runMessages);
  $("mcap-refresh-files")?.addEventListener("click", async () => {
    try {
      await refreshFiles();
      if (mcap.selectedFile) await loadSelected();
      else clearPlot();
    } catch {
      /* showError already */
    }
  });
  $("mcap-file-filter")?.addEventListener("input", (ev) => {
    mcap.filter = ev.target.value || "";
    renderFileList();
  });

  window.McapViewer = { start, stop };
})();
