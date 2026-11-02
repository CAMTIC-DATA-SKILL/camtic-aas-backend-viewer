import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { LABELS, extractEntries, resolveChannels, bindingKey } from "./mapping.js";

const $ = (id) => document.getElementById(id);
const rad = (d) => (d * Math.PI) / 180;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
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
};

let scene, camera, renderer, controls, robotRoot, resizeObs, animating;

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

function setMode(mode) {
  state.mode = mode;
  if (state.demoTimer) {
    clearTimeout(state.demoTimer);
    state.demoTimer = null;
  }
  text("sim-mode", ({ live: "LIVE", demo: "DEMO", stop: "STOP" }[mode] || mode));
  $("sim-host")?.classList.toggle("is-live", mode === "live");
  $("sim-host")?.classList.toggle("is-demo", mode === "demo");
  if (mode === "demo") demoLoop();
}

function ensureScene() {
  const view = $("sim-viewport");
  if (!view || renderer) return;
  scene = new THREE.Scene();
  scene.background = new THREE.Color("#0d1c2a");
  camera = new THREE.PerspectiveCamera(43, 1, 0.02, 100);
  camera.up.set(0, 0, 1);
  camera.position.set(1.58, 1.36, 1.32);
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  renderer.domElement.className = "sim-canvas";
  view.insertBefore(renderer.domElement, view.firstChild);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0.2, 0, 0.57);
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
  const resize = () => {
    const w = view.clientWidth;
    const h = view.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  };
  resizeObs = new ResizeObserver(resize);
  resizeObs.observe(view);
  resize();
  if (!animating) {
    animating = true;
    const tick = (now) => {
      requestAnimationFrame(tick);
      stepMotion(now);
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
  if (bounds.isEmpty()) return;
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const radius = Math.max(0.44, size.length() * 0.62);
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(radius * 1.32, -radius * 1.92, radius * 1.32));
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
  state.tcpNode = tool;
  state.angles = Array(6).fill(0);
  state.displayAngles = Array(6).fill(0);
  state.targetAngles = Array(6).fill(0);
  applyPose(state.angles);
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

function ingest(payload) {
  if (!payload) return;
  const entries = extractEntries(payload);
  if (state.samples === 0 || state.samples % 12 === 0) fillMapOptions(entries);
  state.samples += 1;
  if (state.mode !== "live") return;
  const { chosen } = resolveChannels(entries, state.mappings, state.axisBindings);
  const values = LABELS.map((k) => chosen[k]?.value ?? null);
  const pose = values.slice(0, 6);
  let joints = values.slice(6);
  if (!joints.every(Number.isFinite)) joints = jointsFromFrame(payload) || joints;
  const jointOK = joints.every(Number.isFinite);
  const positionOK = pose.slice(0, 3).every(Number.isFinite);
  const online = payload.frame?.online !== false;
  if (!online || !state.chain.length) return;
  const mode = driveSelect();
  if (jointOK && (mode === "joint" || mode === "auto")) {
    setPose(jointValuesToRad(joints));
    return;
  }
  if (positionOK && (mode === "tcp" || mode === "auto")) {
    const scale = $("sim-pos-unit")?.value === "m" ? 1 : 0.001;
    const targetP = new THREE.Vector3(...pose.slice(0, 3).map((v) => v * scale));
    const result = inverseKinematics(targetP, null);
    if (result.q) setPose(result.q);
  }
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
  state.mappings = Object.fromEntries(LABELS.map((k) => [k, "auto"]));
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
  setMode("stop");
  clearRobot();
}

function bindUi() {
  $("sim-live")?.addEventListener("click", () => setMode("live"));
  $("sim-demo")?.addEventListener("click", () => {
    if (state.available) setMode("demo");
  });
  $("sim-stop")?.addEventListener("click", () => setMode("stop"));
  $("sim-fit")?.addEventListener("click", () => fitCamera());
  LABELS.forEach((name) => {
    $(`sim-map-${name}`)?.addEventListener("change", (event) => {
      state.mappings[name] = event.target.value;
    });
  });
}

bindUi();

const RobotSim = { load, ingest, unload, setMode };
window.RobotSim = RobotSim;
if (window.__pendingSimPackage) RobotSim.load(window.__pendingSimPackage);
