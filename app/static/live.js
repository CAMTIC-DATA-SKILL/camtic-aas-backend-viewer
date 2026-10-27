"use strict";

window.LiveDashboard = (() => {
  const MAX_KEEP = 3600e3;
  const MAX_PTS = 1000;
  const JOINTS = ["J1", "J2", "J3", "J4", "J5", "J6"];
  const CART = ["X", "Y", "Z", "Rx", "Ry", "Rz"];
  const LINE_COLORS = ["#ff6b6b", "#51cf66", "#4dabf7", "#fcc419", "#cc5de8", "#20c997"];

  const state = {
    visible: false,
    redrawTimer: null,
    winSec: 60,
    hist: [],
    callbacks: { getPackageId: () => null },
  };

  const $ = (id) => document.getElementById(id);
  const fmt = (v) => new Date(v).toLocaleTimeString("ko-KR", { hour12: false });
  const grid = { color: "#e5e7eb" };
  const tk = { color: "#4b5563" };

  let charts = { joint: null, cart: null };

  function chartOpts(y1, y2) {
    return {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      normalized: true,
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0 }, line: { borderWidth: 1.5 } },
      plugins: {
        legend: { labels: { color: "#374151" } },
        tooltip: { callbacks: { title: (items) => (items.length ? fmt(items[0].parsed.x) : "") } },
      },
      scales: {
        x: { type: "linear", grid, ticks: { ...tk, maxTicksLimit: 8, callback: fmt } },
        y: { position: "left", title: { display: true, text: y1, color: "#4b5563" }, grid, ticks: tk },
        y2: {
          position: "right",
          title: { display: true, text: y2, color: "#4b5563" },
          grid: { drawOnChartArea: false },
          ticks: tk,
        },
      },
    };
  }

  function ensureCharts() {
    if (typeof Chart === "undefined") return false;
    if (!charts.joint && $("chart-joint")) {
      const jointOpts = chartOpts("Position (deg)", "Velocity");
      jointOpts.plugins.legend.display = false;
      charts.joint = new Chart($("chart-joint"), {
        type: "line",
        options: jointOpts,
        data: {
          datasets: [
            ...JOINTS.map((name, index) => ({
              label: name,
              borderColor: LINE_COLORS[index],
              yAxisID: "y",
              data: [],
            })),
            ...JOINTS.map((name, index) => ({
              label: `${name} vel`,
              borderColor: LINE_COLORS[index],
              borderDash: [2, 2],
              yAxisID: "y2",
              data: [],
            })),
          ],
        },
      });
    }
    if (!charts.cart && $("chart-cart")) {
      charts.cart = new Chart($("chart-cart"), {
        type: "line",
        options: chartOpts("X / Y / Z (mm)", "Rx / Ry / Rz (deg)"),
        data: {
          datasets: CART.map((key, index) => ({
            label: key,
            borderColor: LINE_COLORS[index],
            yAxisID: index < 3 ? "y" : "y2",
            data: [],
          })),
        },
      });
      charts.cart.options.plugins.legend.display = false;
    }
    return Boolean(charts.joint && charts.cart);
  }

  function renderJointChecks() {
    const holder = $("joint-sel");
    if (!holder || holder.childElementCount) return;
    JOINTS.forEach((name, index) => {
      const label = document.createElement("label");
      label.style.color = LINE_COLORS[index];
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = true;
      input.addEventListener("change", () => {
        if (!charts.joint) return;
        charts.joint.data.datasets[index].hidden = !input.checked;
        charts.joint.data.datasets[index + JOINTS.length].hidden = !input.checked;
        charts.joint.update("none");
      });
      label.append(input, ` ${name}`);
      holder.append(label);
    });
  }

  function renderCartChecks() {
    const holder = $("cart-sel");
    if (!holder || holder.childElementCount) return;
    CART.forEach((key, index) => {
      const label = document.createElement("label");
      label.style.color = LINE_COLORS[index];
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = true;
      input.addEventListener("change", () => {
        if (charts.cart) {
          charts.cart.data.datasets[index].hidden = !input.checked;
          charts.cart.update("none");
        }
      });
      label.append(input, ` ${key}`);
      holder.append(label);
    });
  }

  function setBadge(online, error) {
    const badge = $("live-badge");
    if (!badge) return;
    if (error) {
      badge.textContent = "disconnected";
      badge.className = "status-badge error";
      return;
    }
    badge.textContent = online ? "online" : "offline";
    badge.className = online ? "status-badge ok" : "status-badge";
  }

  function showRec(status) {
    const btn = $("live-rec");
    const text = $("live-rec-text");
    if (!btn || !text) return;
    btn.classList.toggle("on", Boolean(status?.on));
    text.textContent = status?.on ? `REC ${status.file || ""} (${status.rows || 0})` : "REC";
  }

  function formatJson(frame) {
    return JSON.stringify(
      {
        packageId: frame.packageId,
        aasId: frame.aasId,
        timestamp: frame.timestamp,
        online: frame.online,
        ...(frame.snapshot || {}),
      },
      null,
      2,
    );
  }

  function push(frame) {
    const t = Date.now();
    const joint = {};
    for (const name of JOINTS) {
      const item = frame.joint?.[name];
      if (item) joint[name] = [item.Position, item.Velocity];
    }
    const cart = CART.map((key) => frame.cartesian?.[key]);
    state.hist.push({ t, joint, cart });
    while (state.hist.length && state.hist[0].t < t - MAX_KEEP) state.hist.shift();
    if ($("live-json")) $("live-json").textContent = formatJson(frame);
    if ($("live-ts")) {
      $("live-ts").textContent = frame.timestamp
        ? String(frame.timestamp)
        : `updated ${new Date().toLocaleTimeString("ko-KR")}`;
    }
    setBadge(Boolean(frame.online), false);
  }

  function windowPoints() {
    const now = Date.now();
    const from = now - state.winSec * 1000;
    let start = state.hist.findIndex((item) => item.t >= from);
    if (start < 0) start = state.hist.length;
    const visible = state.hist.slice(start);
    const step = Math.max(1, Math.ceil(visible.length / MAX_PTS));
    return {
      now,
      from,
      pts: visible.filter((_, index) => index % step === 0 || index === visible.length - 1),
    };
  }

  function redraw() {
    if (!state.visible) return;
    if (!ensureCharts()) return;
    const { now, from, pts } = windowPoints();
    JOINTS.forEach((name, index) => {
      charts.joint.data.datasets[index].data = pts
        .filter((item) => item.joint[name])
        .map((item) => ({ x: item.t, y: item.joint[name][0] }));
      charts.joint.data.datasets[index + JOINTS.length].data = pts
        .filter((item) => item.joint[name])
        .map((item) => ({ x: item.t, y: item.joint[name][1] }));
    });
    charts.cart.data.datasets.forEach((dataset, index) => {
      dataset.data = pts.map((item) => ({ x: item.t, y: item.cart[index] }));
    });
    for (const chart of [charts.joint, charts.cart]) {
      chart.options.scales.x.min = from;
      chart.options.scales.x.max = now;
      chart.update("none");
    }
  }

  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    if (res.status === 204) return { empty: true };
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return { empty: false, data: await res.json() };
  }

  async function recReq(body) {
    const options = body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : { cache: "no-store" };
    try {
      const result = await fetchJson("/api/live/record", options);
      if (!result.empty) showRec(result.data);
    } catch (_) { /* ignore */ }
  }

  function toggleRec() {
    const on = !$("live-rec").classList.contains("on");
    recReq({
      on,
      packageId: state.callbacks.getPackageId(),
      source: "opcua",
    });
  }

  function resetHistory() {
    state.hist = [];
    if ($("live-json")) $("live-json").textContent = "waiting...";
    redraw();
  }

  function init(callbacks) {
    Object.assign(state.callbacks, callbacks);
    renderJointChecks();
    renderCartChecks();
    $("live-window")?.addEventListener("change", (e) => {
      state.winSec = Number(e.target.value);
      redraw();
    });
    $("live-rec")?.addEventListener("click", toggleRec);
    recReq();
    setInterval(() => recReq(), 1000);
  }

  function show() {
    state.visible = true;
    $("layout")?.classList.add("live-mode");
    $("live-dash").hidden = false;
    requestAnimationFrame(() => {
      if (!ensureCharts()) {
        if ($("live-json")) $("live-json").textContent = "Chart.js 를 불러오지 못했습니다. 네트워크를 확인하세요.";
        return;
      }
      Object.values(charts).forEach((chart) => chart?.resize());
      redraw();
      if (!state.redrawTimer) state.redrawTimer = setInterval(redraw, 500);
    });
  }

  function hide() {
    state.visible = false;
    $("layout")?.classList.remove("live-mode");
    $("live-dash").hidden = true;
    if (state.redrawTimer) {
      clearInterval(state.redrawTimer);
      state.redrawTimer = null;
    }
  }

  function ingest(frame) {
    if (!frame) return;
    push(frame);
    if (state.visible) redraw();
  }

  return {
    init,
    show,
    hide,
    resetHistory,
    ingest,
    isVisible: () => state.visible,
  };
})();
