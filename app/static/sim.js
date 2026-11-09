import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { LABELS, extractEntries, resolveChannels, bindingKey } from "./mapping.js";

const $ = (id) => document.getElementById(id);
const rad = (d) => (d * Math.PI) / 180;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fmt = (x, n = 3) => (Number.isFinite(x) ? x.toFixed(n) : "—");
const num = (x) => {
  if (typeof x === "number") return Number.isFinite(x) ? x : null;
  if (x == null || typeof x === "object") return null;
  const n = Number(String(x).trim());
  return Number.isFinite(n) ? n : null;
};

function canonicalPartName(name) {
  let key = String(name || "").trim();
  if (key.includes(":") && /^\d+$/.test(key.split(":").pop())) key = key.replace(/:\d+$/, "");
  return key;
}

// GLB: scene → world → 7 Mesh (base_link, link1…link6). 시뮬레이터 collectRobotParts와 동일.
function collectRobotParts(root) {
  const parts = {};
  root.traverse((obj) => {
    if (!obj.isMesh) return;
    const key = canonicalPartName(obj.name) || canonicalPartName(obj.geometry?.name);
    if (key === "base_link" || /^link[1-6]$/.test(key)) parts[key] = obj;
  });
  return parts;
}

function toWorldMesh(src, name) {
  src.updateWorldMatrix(true, false);
  const geometry = src.geometry.clone();
  geometry.applyMatrix4(src.matrixWorld);
  if (!geometry.attributes.normal) geometry.computeVertexNormals();
  const mesh = new THREE.Mesh(geometry);
  mesh.name = name;
  return mesh;
}

const state = {
  packageId: null,
  info: null,
  mode: "stop",
  available: false,
  parts: {},
  chain: [],
  angles: Array(6).fill(0),
  displayAngles: Array(6).fill(0),
  targetAngles: Array(6).fill(0),
  motion: { from: Array(6).fill(0), to: Array(6).fill(0), start: 0, duration: 0, active: false },
  tcpNode: null,
  mappings: Object.fromEntries(LABELS.map((k) => [k, "auto"])),
  axisBindings: {},
  axisCandidates: [],
  demoTimer: null,
  samples: 0,
  entries: [],
  lastPayload: null,
  lastChosen: null,
  desired: null,
  trace: [],
};

let scene, camera, renderer, controls, robotRoot, resizeObs, animating;
let resizeView = () => {};
let desiredMarker = null;
let trailLine = null;
const trailMaterial = new THREE.LineBasicMaterial({ color: 0x69e0ba, transparent: true, opacity: 0.85 });

function text(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function setTitle(name) {
  const node = $("sim-title");
  if (!node) return;
  const label = String(name || "").trim();
  node.textContent = label;
  node.hidden = !label;
}

function setFocusStatus(message) {
  text("sim-focus-status", message);
}

function setMode(mode) {
  state.mode = mode;
  if (state.demoTimer) {
    clearTimeout(state.demoTimer);
    state.demoTimer = null;
  }
  const label = { live: "LIVE", demo: "DEMO", stop: "STOP" }[mode] || mode;
  text("sim-mode", label);
  text("sim-focus-mode", label);
  for (const id of ["sim-host", "sim-focus-dialog"]) {
    $(id)?.classList.toggle("is-live", mode === "live");
    $(id)?.classList.toggle("is-demo", mode === "demo");
  }
  if (mode === "live") setFocusStatus("LIVE 수신 대기");
  else if (mode === "demo") setFocusStatus("데모 동작");
  else setFocusStatus("정지 · 마지막 자세 유지");
  if (mode === "demo") demoLoop();
}

function ensureScene() {
  const view = $("sim-viewport");
  if (!view || renderer) return;
  scene = new THREE.Scene();
  scene.background = new THREE.Color("#0d1c2a");
  camera = new THREE.PerspectiveCamera(43, 1, 0.02, 100);
  camera.up.set(0, 0, 1);
  camera.position.set(1.72, 1.48, 1.32);
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  renderer.domElement.className = "sim-canvas";
  view.insertBefore(renderer.domElement, view.firstChild);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0, 0.32);
  controls.enableDamping = true;
  controls.dampingFactor = 0.09;
  scene.add(new THREE.AmbientLight(0xffffff, 0.45));
  scene.add(new THREE.HemisphereLight(0xf6fbff, 0x203447, 2.1));
  const key = new THREE.DirectionalLight(0xffffff, 2.6);
  key.position.set(1.8, 2.2, 3);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xd9ebff, 1.2);
  fill.position.set(-2.4, -1.1, 1.9);
  scene.add(fill);
  const grid = new THREE.GridHelper(2.8, 28, 0x355e75, 0x1d3548);
  grid.rotation.x = Math.PI / 2;
  grid.position.z = -0.002;
  scene.add(grid);
  robotRoot = new THREE.Group();
  scene.add(robotRoot);
  desiredMarker = new THREE.Group();
  desiredMarker.visible = false;
  desiredMarker.add(new THREE.AxesHelper(0.12));
  desiredMarker.add(new THREE.Mesh(
    new THREE.SphereGeometry(0.012),
    new THREE.MeshBasicMaterial({ color: 0xf1b35e }),
  ));
  scene.add(desiredMarker);
  resizeView = () => {
    const w = view.clientWidth;
    const h = view.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  };
  resizeObs = new ResizeObserver(resizeView);
  resizeObs.observe(view);
  resizeView();
  if (!animating) {
    animating = true;
    const tick = (now) => {
      requestAnimationFrame(tick);
      stepMotion(now);
      if (state.tcpNode) {
        const actual = actualPose();
        if (actual) addTrace(actual.p);
      }
      if ($("sim-focus-dialog")?.open) refreshKpis();
      controls?.update();
      renderer?.render(scene, camera);
    };
    requestAnimationFrame(tick);
  }
}

function clearRobot() {
  state.available = false;
  state.parts = {};
  state.chain = [];
  state.tcpNode = null;
  state.motion.active = false;
  state.desired = null;
  if (desiredMarker) desiredMarker.visible = false;
  clearTrace();
  robotRoot?.clear();
  setTitle("");
}

function applyPose(q) {
  state.angles = q.slice();
  state.chain.forEach((joint, i) => joint.group.quaternion.setFromAxisAngle(joint.axis, state.angles[i] ?? 0));
  robotRoot?.updateWorldMatrix(true, true);
}

function shortestDelta(from, to) {
  let d = to - from;
  while (d > Math.PI) d -= Math.PI * 2;
  while (d < -Math.PI) d += Math.PI * 2;
  return d;
}

function setPose(q, { snap = false } = {}) {
  const to = q.slice();
  state.targetAngles = to;
  if (snap || !state.chain.length) {
    state.motion.active = false;
    state.displayAngles = to;
    applyPose(to);
    syncSliders();
    return;
  }
  state.motion.from = state.displayAngles.slice();
  state.motion.to = to;
  state.motion.start = performance.now();
  state.motion.duration = state.mode === "live" ? 480 : 200;
  state.motion.active = true;
}

function stepMotion(now) {
  if (!state.motion.active || !state.chain.length) return;
  const t = state.motion.duration <= 0 ? 1 : clamp((now - state.motion.start) / state.motion.duration, 0, 1);
  const e = t * t * (3 - 2 * t);
  const q = state.motion.from.map((a, i) => a + shortestDelta(a, state.motion.to[i]) * e);
  state.displayAngles = q;
  applyPose(q);
  if (t >= 1) {
    state.motion.active = false;
    state.displayAngles = state.motion.to.slice();
    applyPose(state.displayAngles);
  }
  syncSliders();
}

function actualPose() {
  if (!state.tcpNode) return null;
  state.tcpNode.updateWorldMatrix(true, false);
  return {
    p: state.tcpNode.getWorldPosition(new THREE.Vector3()),
    q: state.tcpNode.getWorldQuaternion(new THREE.Quaternion()),
  };
}

function fitCamera() {
  if (!robotRoot || !camera) return;
  robotRoot.updateWorldMatrix(true, true);
  const bounds = new THREE.Box3().setFromObject(robotRoot);
  const size = bounds.isEmpty() ? new THREE.Vector3(1, 1, 1) : bounds.getSize(new THREE.Vector3());
  const radius = Math.max(0.44, size.length() * 0.62);
  controls.target.set(0, 0, 0.32);
  camera.position.set(radius * 1.48, -radius * 2.15, radius * 1.32);
  camera.updateProjectionMatrix();
  controls.update();
}

function buildRobot(defs) {
  if (!robotRoot) return;
  robotRoot.clear();
  state.chain = [];
  const nodes = state.parts;
  if (nodes.base_link) robotRoot.add(nodes.base_link);
  let parent = robotRoot;
  let prev = new THREE.Vector3();
  const count = Math.min(6, defs.length);
  for (let i = 0; i < count; i += 1) {
    const def = defs[i];
    const pivot = new THREE.Vector3(...(def.pivot || [0, 0, 0]));
    const joint = new THREE.Group();
    joint.name = `pivot-${i + 1}`;
    joint.position.copy(pivot).sub(prev);
    parent.add(joint);
    const mesh = nodes[`link${i + 1}`];
    if (mesh) {
      joint.add(mesh);
      mesh.position.copy(pivot).multiplyScalar(-1);
    }
    state.chain.push({
      group: joint,
      axis: new THREE.Vector3(...(def.axis || [1, 0, 0])).normalize(),
      lower: def.lower ?? -Math.PI,
      upper: def.upper ?? Math.PI,
    });
    parent = joint;
    prev = pivot;
  }
  const last = defs[count - 1] || {};
  const flange = new THREE.Group();
  flange.position.copy(new THREE.Vector3(...(last.pivot || [0, 0, 0])).sub(prev));
  flange.quaternion.fromArray(last.zeroQuaternion || [0, 0, 0, 1]);
  parent.add(flange);
  const tool = new THREE.Group();
  flange.add(tool);
  tool.add(new THREE.AxesHelper(0.12));
  state.tcpNode = tool;
  updateToolOffset();
  state.angles = Array(6).fill(0);
  state.displayAngles = Array(6).fill(0);
  state.targetAngles = Array(6).fill(0);
  state.chain.forEach((joint, i) => {
    const slider = $(`sim-slide-${i}`);
    if (!slider) return;
    slider.min = String((joint.lower * 180) / Math.PI);
    slider.max = String((joint.upper * 180) / Math.PI);
  });
  clearTrace();
  applyPose(state.angles);
  syncSliders();
}

function styleParts(parts) {
  Object.entries(parts).forEach(([name, obj]) => {
    obj.traverse((child) => {
      if (!child.isMesh || !child.geometry) return;
      if (!child.geometry.attributes.normal) child.geometry.computeVertexNormals();
      child.material = new THREE.MeshPhysicalMaterial({
        color: name === "base_link" ? 0xf2f6fb : 0xffffff,
        metalness: 0.18,
        roughness: name === "base_link" ? 0.24 : 0.16,
        clearcoat: 1,
        clearcoatRoughness: 0.08,
        sheen: 0.35,
        sheenRoughness: 0.3,
        sheenColor: new THREE.Color(0xffffff),
        specularIntensity: 1,
        reflectivity: 0.9,
        side: THREE.DoubleSide,
      });
      child.castShadow = true;
      child.receiveShadow = true;
    });
  });
}

async function loadMesh(info) {
  ensureScene();
  clearRobot();
  const gltf = await new GLTFLoader().loadAsync(info.meshUrl);
  const raw = collectRobotParts(gltf.scene);
  const expected = ["base_link", "link1", "link2", "link3", "link4", "link5", "link6"];
  const missing = expected.filter((name) => !raw[name]);
  if (!missing.length && info.joints?.length) {
    state.parts = Object.fromEntries(expected.map((name) => [name, toWorldMesh(raw[name], name)]));
    styleParts(state.parts);
    buildRobot(info.joints);
    state.available = true;
    setTitle(info.cadFile);
    fitCamera();
    return;
  }
  robotRoot.add(gltf.scene);
  state.available = false;
  state.chain = [];
  setTitle(info.cadFile);
  fitCamera();
}

function driveSelect() {
  return $("sim-drive")?.value || "tcp";
}

function jointValuesToRad(values) {
  const scale = $("sim-joint-unit")?.value === "rad" ? 1 : Math.PI / 180;
  return values.map((angle, i) => {
    const sign = Number($(`sim-sign-${i}`)?.value || 1);
    const zero = rad(num($(`sim-zero-${i}`)?.value) ?? 0);
    return sign * angle * scale + zero;
  });
}

function orientationError(target, actual) {
  let r = target.clone().multiply(actual.clone().invert()).normalize();
  if (r.w < 0) {
    r.x *= -1;
    r.y *= -1;
    r.z *= -1;
    r.w *= -1;
  }
  const s = Math.hypot(r.x, r.y, r.z);
  if (s < 1e-10) return new THREE.Vector3();
  return new THREE.Vector3(r.x, r.y, r.z).multiplyScalar((2 * Math.atan2(s, clamp(r.w, -1, 1))) / s);
}

function solveLinear(A, b) {
  const n = b.length;
  const m = A.map((row, i) => [...row, b[i]]);
  for (let k = 0; k < n; k += 1) {
    let max = k;
    for (let i = k + 1; i < n; i += 1) if (Math.abs(m[i][k]) > Math.abs(m[max][k])) max = i;
    [m[k], m[max]] = [m[max], m[k]];
    if (Math.abs(m[k][k]) < 1e-11) return null;
    const v = m[k][k];
    for (let j = k; j <= n; j += 1) m[k][j] /= v;
    for (let i = 0; i < n; i += 1) {
      if (i === k) continue;
      const t = m[i][k];
      for (let j = k; j <= n; j += 1) m[i][j] -= t * m[k][j];
    }
  }
  return m.map((row) => row[n]);
}

function inverseKinematics(targetP, targetQ) {
  if (!state.tcpNode || state.chain.length !== 6) return { q: null, error: null };
  const visual = state.displayAngles.slice();
  let q = state.targetAngles.slice();
  let error = Infinity;
  for (let it = 0; it < 40; it += 1) {
    applyPose(q);
    const cur = actualPose();
    const dp = targetP.clone().sub(cur.p);
    const dr = targetQ ? orientationError(targetQ, cur.q) : new THREE.Vector3();
    error = dp.length();
    if (error < 0.001 && (!targetQ || dr.length() < 0.025)) break;
    const cols = state.chain.map(({ group, axis }) => {
      const jp = group.getWorldPosition(new THREE.Vector3());
      const rotation = group.getWorldQuaternion(new THREE.Quaternion());
      const av = axis.clone().applyQuaternion(rotation).normalize();
      const lv = av.clone().cross(cur.p.clone().sub(jp));
      return [lv.x, lv.y, lv.z, av.x * 0.16, av.y * 0.16, av.z * 0.16];
    });
    const ev = [dp.x, dp.y, dp.z, dr.x * 0.16, dr.y * 0.16, dr.z * 0.16];
    const a = Array.from({ length: 6 }, (_, i) =>
      Array.from({ length: 6 }, (_, j) => cols[i].reduce((s, v, k) => s + v * cols[j][k], 0) + (i === j ? 0.0025 : 0)),
    );
    const b = cols.map((col) => col.reduce((s, v, k) => s + v * ev[k], 0));
    const dq = solveLinear(a, b);
    if (!dq) break;
    q = q.map((v, i) => clamp(v + clamp(dq[i], -0.13, 0.13), state.chain[i].lower, state.chain[i].upper));
  }
  applyPose(visual);
  return { q, error };
}

function axisLabel(item) {
  if (!item) return "";
  const sub = item.submodelIdShort || "";
  const path = item.aasPath || item.path || "";
  return [sub, path].filter(Boolean).join(" / ");
}

function fillMapOptions(entries = []) {
  const seen = new Map();
  for (const item of state.axisCandidates) {
    const key = item.key || bindingKey(item);
    if (key) seen.set(key, { path: key, submodelIdShort: item.submodelIdShort, aasPath: item.path, axis: item.axis });
  }
  for (const item of entries) {
    if (item.path && !seen.has(item.path)) seen.set(item.path, item);
  }
  const options = [["auto", "AAS 기본 바인딩"], ...[...seen.values()].map((item) => [item.path, axisLabel(item) || item.path])];
  for (const name of LABELS) {
    const select = $(`sim-map-${name}`);
    if (!select) continue;
    const binding = state.axisBindings[name];
    const last = state.mappings[name];
    select.replaceChildren(
      ...options.map(([value, label]) => {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        return opt;
      }),
    );
    if (last && last !== "auto" && options.some((item) => item[0] === last)) select.value = last;
    else select.value = "auto";
    state.mappings[name] = select.value;
    const hint = $(`sim-bind-${name}`);
    if (hint) hint.textContent = binding ? axisLabel(binding) : "Submodel Property 없음";
  }
}

function jointsFromFrame(payload) {
  const joint = payload?.frame?.joint;
  if (!joint) return null;
  const values = [1, 2, 3, 4, 5, 6].map((i) => num(joint[`J${i}`]?.Position));
  return values.every(Number.isFinite) ? values : null;
}

function frameRotation() {
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(
    rad(num($("sim-frame-roll")?.value) ?? 0),
    rad(num($("sim-frame-pitch")?.value) ?? 0),
    rad(num($("sim-frame-yaw")?.value) ?? 0),
    "ZYX",
  ));
}

function frameTransform(position, quaternion) {
  const rotation = frameRotation();
  const moved = position.clone().applyQuaternion(rotation).add(new THREE.Vector3(
    (num($("sim-frame-x")?.value) ?? 0) / 1000,
    (num($("sim-frame-y")?.value) ?? 0) / 1000,
    (num($("sim-frame-z")?.value) ?? 0) / 1000,
  ));
  return { p: moved, q: quaternion ? rotation.clone().multiply(quaternion) : null };
}

function updateToolOffset() {
  if (!state.tcpNode) return;
  state.tcpNode.position.set(
    (num($("sim-tool-x")?.value) ?? 0) / 1000,
    (num($("sim-tool-y")?.value) ?? 0) / 1000,
    (num($("sim-tool-z")?.value) ?? 0) / 1000,
  );
}

function syncSliders() {
  for (let i = 0; i < 6; i += 1) {
    const degrees = ((state.displayAngles[i] ?? 0) * 180) / Math.PI;
    const label = $(`sim-jval-${i}`);
    if (label) label.textContent = `${degrees.toFixed(1)}°`;
    const slider = $(`sim-slide-${i}`);
    if (slider && document.activeElement !== slider) slider.value = String(degrees);
  }
}

function refreshKpis() {
  const pose = actualPose();
  if (!pose) return;
  text("sim-kpi-x", fmt(pose.p.x, 3));
  text("sim-kpi-y", fmt(pose.p.y, 3));
  text("sim-kpi-z", fmt(pose.p.z, 3));
  const euler = new THREE.Euler().setFromQuaternion(pose.q, $("sim-euler-order")?.value || "XYZ");
  text("sim-kpi-rx", fmt((euler.x * 180) / Math.PI, 1));
  text("sim-kpi-ry", fmt((euler.y * 180) / Math.PI, 1));
  text("sim-kpi-rz", fmt((euler.z * 180) / Math.PI, 1));
  text("sim-tcp-err", state.desired?.p ? `${fmt(pose.p.distanceTo(state.desired.p) * 1000, 1)} mm` : "—");
}

function setDesired(position, quaternion) {
  if (!position) {
    state.desired = null;
    if (desiredMarker) desiredMarker.visible = false;
    return;
  }
  state.desired = { p: position.clone(), q: quaternion ? quaternion.clone() : null };
  if (!desiredMarker) return;
  desiredMarker.visible = true;
  desiredMarker.position.copy(position);
  desiredMarker.quaternion.copy(quaternion || new THREE.Quaternion());
}

function clearTrace() {
  state.trace = [];
  if (!trailLine || !scene) return;
  scene.remove(trailLine);
  trailLine.geometry.dispose();
  trailLine = null;
}

function addTrace(point) {
  if (!scene || !point) return;
  const last = state.trace.at(-1);
  if (last && last.distanceTo(point) < 0.004) return;
  state.trace.push(point.clone());
  if (state.trace.length > 500) state.trace.shift();
  if (state.trace.length < 2) return;
  if (trailLine) {
    scene.remove(trailLine);
    trailLine.geometry.dispose();
  }
  trailLine = new THREE.Line(new THREE.BufferGeometry().setFromPoints(state.trace), trailMaterial);
  scene.add(trailLine);
}

function renderAudit(chosen) {
  const node = $("sim-map-audit");
  if (!node) return;
  node.textContent = LABELS.map((key) => {
    const entry = chosen?.[key];
    const binding = state.axisBindings[key];
    const rendered = entry
      ? `${entry.path} = ${entry.value}`
      : (binding ? `구독 ${axisLabel(binding)} · 값 없음` : "미매핑");
    return `${key.padEnd(2)} : ${rendered}`;
  }).join("\n");
}

function renderChannelTable() {
  const body = $("sim-data-table");
  if (!body || !$("sim-focus-dialog")?.open) return;
  const filter = ($("sim-data-filter")?.value || "").trim().toLowerCase();
  const rows = state.entries
    .filter((entry) => !filter || entry.path.toLowerCase().includes(filter) || String(entry.value).toLowerCase().includes(filter))
    .slice(0, 200);
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.textContent = state.entries.length ? "일치하는 채널 없음" : "데이터 수신 대기";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const entry of rows) {
    const tr = document.createElement("tr");
    const path = document.createElement("td");
    const value = document.createElement("td");
    path.textContent = entry.path;
    path.title = entry.path;
    value.textContent = String(entry.value);
    tr.append(path, value);
    body.appendChild(tr);
  }
}

function setModelNote(info) {
  const node = $("sim-model-note");
  if (!node) return;
  if (!info) {
    node.textContent = "";
    return;
  }
  const source = info.kinematicsSource === "urdf"
    ? `URDF ${info.urdf?.fileName || ""}`.trim()
    : (info.kinematicsSource === "suggested" ? "추정 관절축" : "");
  const warning = (info.warnings || []).filter(Boolean).slice(0, 2).join(" ");
  node.textContent = [source, info.cadFile, warning].filter(Boolean).join(" · ");
}

function targetQuaternion(pose) {
  if (!$("sim-use-orient")?.checked || !pose.slice(3, 6).every(Number.isFinite)) return null;
  const scale = $("sim-angle-unit")?.value === "rad" ? 1 : Math.PI / 180;
  const order = $("sim-euler-order")?.value || "XYZ";
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(...pose.slice(3, 6).map((value) => value * scale), order));
}

function driveFromState() {
  const payload = state.lastPayload;
  if (!payload) return;
  text("sim-channel-count", String(state.entries.length));
  text("sim-stamp", String(payload.frame?.timestamp || "—").slice(0, 24));
  const { chosen } = resolveChannels(state.entries, state.mappings, state.axisBindings);
  state.lastChosen = chosen;
  renderAudit(chosen);
  renderChannelTable();
  const values = LABELS.map((key) => chosen[key]?.value ?? null);
  const pose = values.slice(0, 6);
  let joints = values.slice(6);
  if (!joints.every(Number.isFinite)) joints = jointsFromFrame(payload) || joints;
  const jointOK = joints.every(Number.isFinite);
  const positionOK = pose.slice(0, 3).every(Number.isFinite);
  const orientOK = pose.slice(3, 6).every(Number.isFinite);
  const scale = $("sim-pos-unit")?.value === "m" ? 1 : 0.001;
  if (positionOK) {
    const received = ["X", "Y", "Z"].map((axis, index) => `${axis} ${fmt(pose[index], 1)}`).join("  ");
    const rotation = orientOK ? `   Rx ${fmt(pose[3], 1)} Ry ${fmt(pose[4], 1)} Rz ${fmt(pose[5], 1)}` : "";
    text("sim-rx-tcp", received + rotation);
    const target = frameTransform(
      new THREE.Vector3(...pose.slice(0, 3).map((value) => value * scale)),
      targetQuaternion(pose),
    );
    setDesired(target.p, target.q);
  } else {
    text("sim-rx-tcp", "TCP XYZ 없음");
    setDesired(null, null);
  }
  const online = payload.frame?.online !== false;
  if (state.mode === "live" && online && state.chain.length) {
    const mode = driveSelect();
    if (jointOK && (mode === "joint" || mode === "auto")) {
      setPose(jointValuesToRad(joints));
      setFocusStatus("실제 Joint 6축 구동");
    } else if (positionOK && (mode === "tcp" || mode === "auto")) {
      const result = inverseKinematics(state.desired.p, state.desired.q);
      if (result.q) setPose(result.q);
      setFocusStatus(`TCP 역기구학 · 잔차 ${fmt((result.error || 0) * 1000, 1)} mm`);
    } else {
      setFocusStatus("Joint 6개 또는 TCP XYZ를 찾지 못했습니다.");
    }
  } else if (state.mode === "live" && !online) {
    setFocusStatus("구독 OFFLINE · 마지막 자세 유지");
  }
  refreshKpis();
}

function ingest(payload) {
  if (!payload) return;
  state.lastPayload = payload;
  state.entries = extractEntries(payload);
  if (state.samples === 0 || state.samples % 12 === 0) fillMapOptions(state.entries);
  state.samples += 1;
  driveFromState();
}

function demoLoop() {
  if (state.mode !== "demo") return;
  const t = performance.now() / 1000;
  const q = [
    0.35 * Math.sin(t * 0.5),
    -0.3 * Math.sin(t * 0.65),
    0.3 * Math.sin(t * 0.6 + 0.7),
    0.6 * Math.sin(t * 0.9),
    0.3 * Math.sin(t * 0.95),
    0.85 * Math.sin(t * 0.75),
  ];
  if (state.chain.length) setPose(q);
  state.demoTimer = setTimeout(demoLoop, 200);
}

async function load(packageId) {
  state.packageId = packageId;
  state.info = null;
  state.samples = 0;
  state.axisBindings = {};
  state.axisCandidates = [];
  state.entries = [];
  state.lastPayload = null;
  state.lastChosen = null;
  state.mappings = Object.fromEntries(LABELS.map((k) => [k, "auto"]));
  setModelNote(null);
  setMode("stop");
  $("sim-host")?.removeAttribute("hidden");
  if (!packageId) {
    clearRobot();
    return;
  }
  try {
    const res = await fetch(`/api/packages/${encodeURIComponent(packageId)}/simulation`);
    const info = await res.json();
    if (!res.ok) throw new Error(info.detail || `${res.status} ${res.statusText}`);
    state.info = info;
    state.axisBindings = info.axisBindings || {};
    state.axisCandidates = info.axisCandidates || [];
    setModelNote(info);
    const drive = $("sim-drive");
    if (drive) drive.value = info.driveDefault || "tcp";
    fillMapOptions();
    if (!info.hasMesh) {
      ensureScene();
      clearRobot();
      return;
    }
    await loadMesh(info);
    setMode(info.hasKinematics ? "live" : "stop");
  } catch {
    ensureScene();
    clearRobot();
  }
}

function unload() {
  state.packageId = null;
  state.info = null;
  state.entries = [];
  state.lastPayload = null;
  focusClose();
  setModelNote(null);
  setMode("stop");
  clearRobot();
}

function focusOpen() {
  const dialog = $("sim-focus-dialog");
  const view = $("sim-viewport");
  const stage = $("sim-focus-stage");
  const drawer = $("sim-drawer");
  const drawerSlot = $("sim-focus-drawer");
  if (!dialog || !view || !stage) return;
  ensureScene();
  if (!dialog.open) dialog.showModal();
  stage.appendChild(view);
  if (drawer && drawerSlot) drawerSlot.appendChild(drawer);
  const note = $("sim-stage-note");
  if (note) note.hidden = false;
  $("sim-host")?.classList.add("is-focused");
  document.documentElement.classList.add("sim-modal-open");
  setFocusButton(true);
  renderChannelTable();
  requestAnimationFrame(() => requestAnimationFrame(resizeView));
}

function focusClose() {
  const dialog = $("sim-focus-dialog");
  const view = $("sim-viewport");
  const stage = $("sim-stage");
  const drawer = $("sim-drawer");
  const drawerHome = $("sim-drawer-home");
  if (view && stage && view.parentElement !== stage) stage.appendChild(view);
  if (drawer && drawerHome && drawer.parentElement !== drawerHome) drawerHome.appendChild(drawer);
  const note = $("sim-stage-note");
  if (note) note.hidden = true;
  $("sim-host")?.classList.remove("is-focused");
  document.documentElement.classList.remove("sim-modal-open");
  setFocusButton(false);
  if (dialog?.open) dialog.close();
  requestAnimationFrame(resizeView);
}

function setFocusButton(open) {
  const button = $("sim-focus");
  if (!button) return;
  button.classList.toggle("is-open", open);
  button.setAttribute("aria-pressed", open ? "true" : "false");
  button.setAttribute("aria-label", open ? "시뮬레이션 축소" : "시뮬레이션 확대");
  button.title = open ? "축소" : "확대";
}

function onManualJoint(index, value) {
  if (!state.chain.length) {
    const slider = $(`sim-slide-${index}`);
    if (slider) slider.value = String(((state.displayAngles[index] ?? 0) * 180) / Math.PI);
    return;
  }
  setMode("stop");
  const pose = state.displayAngles.slice();
  while (pose.length < 6) pose.push(0);
  pose[index] = rad(Number(value));
  setPose(pose, { snap: true });
  setFocusStatus("수동 관절");
  refreshKpis();
}

function buildSliders() {
  const root = $("sim-joint-sliders");
  if (!root || root.childElementCount) return;
  for (let i = 0; i < 6; i += 1) {
    const row = document.createElement("label");
    row.className = "sim-joint";
    const head = document.createElement("span");
    head.className = "sim-joint-head";
    const name = document.createElement("span");
    name.textContent = `J${i + 1}`;
    const val = document.createElement("b");
    val.id = `sim-jval-${i}`;
    val.textContent = "0.0°";
    head.append(name, val);
    const slider = document.createElement("input");
    slider.type = "range";
    slider.id = `sim-slide-${i}`;
    slider.min = "-180";
    slider.max = "180";
    slider.step = "0.1";
    slider.value = "0";
    slider.addEventListener("input", () => onManualJoint(i, slider.value));
    row.append(head, slider);
    root.appendChild(row);
  }
}

function alignTcp() {
  const chosen = state.lastChosen || {};
  if (!["X", "Y", "Z"].every((key) => Number.isFinite(chosen[key]?.value)) || !actualPose()) {
    setFocusStatus("정합 불가: TCP XYZ가 매핑되어 있어야 합니다.");
    return;
  }
  const scale = $("sim-pos-unit")?.value === "m" ? 1 : 0.001;
  const raw = new THREE.Vector3(chosen.X.value, chosen.Y.value, chosen.Z.value).multiplyScalar(scale);
  raw.applyQuaternion(frameRotation());
  const offset = actualPose().p.clone().sub(raw).multiplyScalar(1000);
  ["sim-frame-x", "sim-frame-y", "sim-frame-z"].forEach((id, index) => {
    const input = $(id);
    if (input) input.value = offset.getComponent(index).toFixed(2);
  });
  if (state.lastPayload) driveFromState();
  setFocusStatus("현재 자세 기준으로 TCP 위치 평행이동을 맞췄습니다.");
}

function redrive() {
  updateToolOffset();
  if (state.lastPayload) driveFromState();
  else refreshKpis();
}

function bindUi() {
  buildSliders();
  const goLive = () => setMode("live");
  const goDemo = () => {
    if (state.available) setMode("demo");
  };
  const goStop = () => setMode("stop");
  $("sim-live")?.addEventListener("click", goLive);
  $("sim-focus-live")?.addEventListener("click", goLive);
  $("sim-demo")?.addEventListener("click", goDemo);
  $("sim-focus-demo")?.addEventListener("click", goDemo);
  $("sim-stop")?.addEventListener("click", goStop);
  $("sim-focus-stop")?.addEventListener("click", goStop);
  $("sim-fit")?.addEventListener("click", () => fitCamera());
  $("sim-focus-fit")?.addEventListener("click", () => fitCamera());
  $("sim-focus")?.addEventListener("click", (event) => {
    event.stopPropagation();
    if ($("sim-focus-dialog")?.open) focusClose();
    else focusOpen();
  });
  $("sim-focus-close")?.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    focusClose();
  });
  $("sim-focus-dialog")?.addEventListener("close", () => {
    if ($("sim-viewport")?.parentElement?.id === "sim-focus-stage") focusClose();
  });
  const dialog = $("sim-focus-dialog");
  let backdropDown = false;
  dialog?.addEventListener("pointerdown", (event) => {
    backdropDown = event.target === dialog;
  });
  dialog?.addEventListener("click", (event) => {
    if (backdropDown && event.target === dialog) focusClose();
    backdropDown = false;
  });
  $("sim-home")?.addEventListener("click", () => {
    if (!state.chain.length) return;
    setMode("stop");
    setPose(Array(6).fill(0), { snap: true });
    setFocusStatus("J1~J6 = 0°");
    refreshKpis();
  });
  $("sim-clear-trace")?.addEventListener("click", clearTrace);
  $("sim-align-tcp")?.addEventListener("click", alignTcp);
  $("sim-data-filter")?.addEventListener("input", renderChannelTable);
  for (const id of ["sim-tool-x", "sim-tool-y", "sim-tool-z", "sim-frame-roll", "sim-frame-pitch", "sim-frame-yaw", "sim-frame-x", "sim-frame-y", "sim-frame-z"]) {
    $(id)?.addEventListener("input", redrive);
  }
  for (const id of ["sim-angle-unit", "sim-euler-order", "sim-use-orient", "sim-drive", "sim-pos-unit", "sim-joint-unit"]) {
    $(id)?.addEventListener("change", redrive);
  }
  for (let i = 0; i < 6; i += 1) {
    $(`sim-sign-${i}`)?.addEventListener("change", redrive);
    $(`sim-zero-${i}`)?.addEventListener("change", redrive);
  }
  LABELS.forEach((name) => {
    $(`sim-map-${name}`)?.addEventListener("change", (event) => {
      state.mappings[name] = event.target.value;
      if (state.lastPayload) driveFromState();
    });
  });
}

bindUi();

const RobotSim = { load, ingest, unload, setMode };
window.RobotSim = RobotSim;
if (window.__pendingSimPackage) RobotSim.load(window.__pendingSimPackage);
