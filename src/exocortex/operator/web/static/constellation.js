// Memory constellation. Three.js, orthographic 2D scene with subtle z-axis
// parallax for depth.
//
// Visual layers (back to front):
//   0. ambient drift field (slow-moving tiny specks)
//   1. semantic edges (thin lines between similar records)
//   2. halos (soft glow behind each star)
//   3. main stars (the actual record points)
//   4. transient pulse arcs (new-write neighbor flares)
//
// Three orthogonal channels of meaning:
//   color      = scope        (existing palette)
//   brightness = confidence   (existing)
//   size       = activity     (new — recency × access_count)
//
// Every channel has a glyph / text companion in the tooltip + detail
// for color-blind accessibility. No flashing, no rainbow, no chromatic
// aberration. Animations easeOut 200–400ms.

import * as THREE from "three";

const SCOPE_COLOR = {
  session: new THREE.Color(0x6a8cc2),
  task:    new THREE.Color(0x58a6ff),
  project: new THREE.Color(0x7ee787),
  global:  new THREE.Color(0xf2c44f),
};
const SCOPE_GLYPH = { session: "·", task: "+", project: "■", global: "◆" };

const CONFIDENCE_BRIGHTNESS = {
  observed: 1.0,
  inferred: 0.82,
  asserted: 0.65,
  external_claim: 0.48,
};

// Base pixel sizes at zoom = 1.
const BASE_PX = 30.0;
const HALO_PX = 62.0;
const HOVER_MULT = 1.55;
const SELECTED_MULT = 1.35;
const RECENT_PX_BOOST = 8.0;
const BREATHE_AMP = 0.08;
const BREATHE_PERIOD_MS = 3800;

const RECENT_WINDOW_MS = 60 * 60 * 1000;
const CANVAS_PAD = 1.25;

// Semantic edges
const EDGE_THRESHOLD = 0.78;
const EDGE_CAP = 500;
const EDGE_ALPHA_BASE = 0.15;
const EDGE_ALPHA_PRIMARY = 0.6;
const EDGE_ALPHA_SECONDARY = 0.3;

// Cluster labels
const CLUSTER_RADIUS = 0.15;
const MAX_CLUSTER_LABELS = 12;

// Activity / search dimming
const SEARCH_DIM_ALPHA = 0.2;
const SEARCH_HIT_MULT = 1.45;

// DOM --------------------------------------------------------------------

const canvas = document.getElementById("constellation-canvas");
const tooltip = document.getElementById("tooltip");
const hoverPanel = document.getElementById("hover-panel");
const detail = document.getElementById("detail");
const canvasWrap = canvas.parentElement;
const clusterLabelsHost = document.getElementById("cluster-labels");
const searchInput = document.getElementById("search-input");
const searchHits = document.getElementById("search-hits");
const searchClear = document.getElementById("search-clear");

// State ------------------------------------------------------------------

const state = {
  records: [],
  visible: [],
  highlights: [],         // {recordIdx, start} bloom after memory.written
  pulseArcs: [],          // {fromIdx, toIdx, start} brief arcs
  hoveredIdx: -1,
  selectedIdx: -1,
  newThisSession: 0,
  edges: [],              // [{a, b, w}]  indexes into records
  edgeAdj: new Map(),     // recordIdx -> Set of neighbor recordIdx
  searchHits: null,       // null = no search; Set<recordId> otherwise
  cameraTween: null,      // {t0, dur, fromPos, toPos, fromZoom, toZoom}
  accessCounts: new Map(),// recordId -> int
  azimuth: 0,             // radians of slow camera orbit
  moodActivity: 0,
  chatTracks: [],         // live "thinking" animation tracks (memory.chat)
  filters: {
    scopes: new Set(["session", "task", "project", "global"]),
    sources: new Set(["codex", "claude_code", "hermes", "operator", "__other__"]),
    confidences: new Set(["observed", "inferred", "asserted", "external_claim"]),
    maxAgeDays: Infinity, // show ALL by default — this is a durable store, not
    // a 30-day window (the old default permanently hid older records).
  },
};

// Live-thinking animation (memory.chat) ---------------------------------
const CHAT_TRACK_CAP = 4;
const CHAT_RIPPLE_DUR_MS = 600;
const CHAT_RIPPLE_MAX_WORLD = 0.40;
const CHAT_PULSE_BOOST_MS = 200;
const CHAT_PULSE_RELAX_MS = 600;
const CHAT_PULSE_STAGGER_MS = 40;
const CHAT_BEAM_DELAY_MS = 220;     // beams start after retrieval pulse spike
const CHAT_BEAM_HOLD_MS = 1200;
const CHAT_BEAM_FADE_MS = 280;
const CHAT_BEAM_STAGGER_MS = 80;
const CHAT_RIPPLE_COLOR = new THREE.Color(0x58a6ff);
const CHAT_PULSE_COLOR  = new THREE.Color(0x7ee787);
const CHAT_BEAM_COLOR   = new THREE.Color(0xd29922);

// Scene ------------------------------------------------------------------

const scene = new THREE.Scene();
scene.background = null;

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

const camera = new THREE.OrthographicCamera(-1.25, 1.25, 1.25, -1.25, 0.1, 100);
camera.position.set(0, 0, 10);
camera.zoom = 1.0;

function resize() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  renderer.setSize(w, h, false);
  const aspect = w / h;
  camera.left = -CANVAS_PAD * aspect;
  camera.right = CANVAS_PAD * aspect;
  camera.top = CANVAS_PAD;
  camera.bottom = -CANVAS_PAD;
  camera.updateProjectionMatrix();
  layoutClusterLabels();
}
window.addEventListener("resize", resize);

// Ambient drift field ---------------------------------------------------

let ambientPoints;
(function initAmbient() {
  const N = 420;
  const geom = new THREE.BufferGeometry();
  const positions = new Float32Array(N * 3);
  const drifts = new Float32Array(N * 2);
  for (let i = 0; i < N; i++) {
    positions[i * 3 + 0] = (Math.random() - 0.5) * 4;
    positions[i * 3 + 1] = (Math.random() - 0.5) * 4;
    positions[i * 3 + 2] = -2;
    drifts[i * 2 + 0] = (Math.random() - 0.5) * 0.015;
    drifts[i * 2 + 1] = (Math.random() - 0.5) * 0.015;
  }
  geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geom.userData.drifts = drifts;
  const mat = new THREE.PointsMaterial({
    size: 2.0,
    color: 0x2f3a48,
    transparent: true,
    opacity: 0.65,
    sizeAttenuation: false,
  });
  ambientPoints = new THREE.Points(geom, mat);
  scene.add(ambientPoints);
})();

// Main + halo + edge layers --------------------------------------------

let mainPoints = null, haloPoints = null, edgeLines = null, arcLines = null;
let mainGeom = null, haloGeom = null, edgeGeom = null, arcGeom = null;
let mainMat = null, haloMat = null, edgeMat = null, arcMat = null;

const STAR_VS = `
  attribute float aBaseSize;
  attribute float aPhase;
  attribute float aHover;
  attribute float aSelected;
  attribute float aBloom;
  attribute float aActivity;
  attribute float aDim;
  varying vec3 vColor;
  varying float vDim;
  uniform float uZoom;
  uniform float uTime;
  uniform float uHoverMult;
  uniform float uSelectedMult;
  uniform float uBreatheAmp;
  uniform float uBreathePeriod;
  void main() {
    vColor = color;
    vDim = aDim;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    float breathe = 1.0 + uBreatheAmp * sin(6.2831853 * (uTime / uBreathePeriod + aPhase));
    float mult = 1.0
      + aHover * (uHoverMult - 1.0)
      + aSelected * (uSelectedMult - 1.0);
    float px = (aBaseSize * breathe * mult * aActivity + aBloom) * uZoom;
    gl_PointSize = max(2.0, px);
  }
`;

const STAR_FS = `
  varying vec3 vColor;
  varying float vDim;
  void main() {
    vec2 c = gl_PointCoord - vec2(0.5);
    float d = length(c);
    if (d > 0.5) discard;
    float body = smoothstep(0.5, 0.08, d);
    float core = smoothstep(0.22, 0.0, d) * 0.55;
    vec3 col = vColor + vec3(core);
    gl_FragColor = vec4(col, body * vDim);
  }
`;

const HALO_FS = `
  varying vec3 vColor;
  varying float vDim;
  void main() {
    vec2 c = gl_PointCoord - vec2(0.5);
    float d = length(c);
    if (d > 0.5) discard;
    float a = smoothstep(0.5, 0.0, d);
    a = pow(a, 1.7) * 0.45;
    gl_FragColor = vec4(vColor, a * vDim);
  }
`;

function commonUniforms() {
  return {
    uZoom: { value: camera.zoom },
    uTime: { value: 0.0 },
    uHoverMult: { value: HOVER_MULT },
    uSelectedMult: { value: SELECTED_MULT },
    uBreatheAmp: { value: BREATHE_AMP },
    uBreathePeriod: { value: BREATHE_PERIOD_MS },
  };
}

function rebuildLayers() {
  for (const layer of [mainPoints, haloPoints, edgeLines, arcLines]) {
    if (layer) scene.remove(layer);
  }
  if (mainGeom) mainGeom.dispose();
  if (haloGeom) haloGeom.dispose();
  if (edgeGeom) edgeGeom.dispose();
  if (arcGeom) arcGeom.dispose();
  if (mainMat) mainMat.dispose();
  if (haloMat) haloMat.dispose();
  if (edgeMat) edgeMat.dispose();
  if (arcMat) arcMat.dispose();

  const N = state.records.length;
  const positions = new Float32Array(N * 3);
  const colors = new Float32Array(N * 3);
  const baseSizes = new Float32Array(N);
  const haloSizes = new Float32Array(N);
  const phases = new Float32Array(N);
  const hovers = new Float32Array(N);
  const selecteds = new Float32Array(N);
  const blooms = new Float32Array(N);
  const activities = new Float32Array(N);
  const dims = new Float32Array(N);
  const now = Date.now();

  for (let i = 0; i < N; i++) {
    const r = state.records[i];
    // Subtle z-axis parallax: ±0.05 based on a hash of id.
    const zHash = simpleHash(r.id);
    const z = ((zHash % 1000) / 1000 - 0.5) * 0.1;
    positions[i * 3] = r.x;
    positions[i * 3 + 1] = r.y;
    positions[i * 3 + 2] = z;

    const base = SCOPE_COLOR[r.scope] || new THREE.Color(0xc0c8d2);
    const b = CONFIDENCE_BRIGHTNESS[r.confidence] ?? 0.6;
    colors[i * 3] = base.r * b;
    colors[i * 3 + 1] = base.g * b;
    colors[i * 3 + 2] = base.b * b;

    const age = now - Date.parse(r.timestamp);
    const recent = age < RECENT_WINDOW_MS ? RECENT_PX_BOOST : 0;
    baseSizes[i] = BASE_PX + recent;
    haloSizes[i] = HALO_PX + recent * 1.6;
    phases[i] = (zHash * 31 % 1000) / 1000;
    hovers[i] = 0;
    selecteds[i] = 0;
    blooms[i] = 0;
    activities[i] = computeActivityMult(r);
    dims[i] = 1.0;
  }

  // Main stars
  mainGeom = new THREE.BufferGeometry();
  mainGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  mainGeom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  mainGeom.setAttribute("aBaseSize", new THREE.BufferAttribute(baseSizes, 1));
  mainGeom.setAttribute("aPhase", new THREE.BufferAttribute(phases, 1));
  mainGeom.setAttribute("aHover", new THREE.BufferAttribute(hovers, 1));
  mainGeom.setAttribute("aSelected", new THREE.BufferAttribute(selecteds, 1));
  mainGeom.setAttribute("aBloom", new THREE.BufferAttribute(blooms, 1));
  mainGeom.setAttribute("aActivity", new THREE.BufferAttribute(activities, 1));
  mainGeom.setAttribute("aDim", new THREE.BufferAttribute(dims, 1));

  mainMat = new THREE.ShaderMaterial({
    uniforms: commonUniforms(),
    vertexShader: STAR_VS,
    fragmentShader: STAR_FS,
    vertexColors: true,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  mainPoints = new THREE.Points(mainGeom, mainMat);
  mainPoints.renderOrder = 3;

  // Halo
  haloGeom = new THREE.BufferGeometry();
  haloGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  haloGeom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  haloGeom.setAttribute("aBaseSize", new THREE.BufferAttribute(haloSizes, 1));
  haloGeom.setAttribute("aPhase", mainGeom.getAttribute("aPhase"));
  haloGeom.setAttribute("aHover", mainGeom.getAttribute("aHover"));
  haloGeom.setAttribute("aSelected", mainGeom.getAttribute("aSelected"));
  haloGeom.setAttribute("aBloom", mainGeom.getAttribute("aBloom"));
  haloGeom.setAttribute("aActivity", mainGeom.getAttribute("aActivity"));
  haloGeom.setAttribute("aDim", mainGeom.getAttribute("aDim"));

  haloMat = new THREE.ShaderMaterial({
    uniforms: commonUniforms(),
    vertexShader: STAR_VS,
    fragmentShader: HALO_FS,
    vertexColors: true,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  haloPoints = new THREE.Points(haloGeom, haloMat);
  haloPoints.renderOrder = 2;

  scene.add(haloPoints);
  scene.add(mainPoints);

  rebuildEdgeLayer();
  applyFilterMask();
}

function rebuildEdgeLayer() {
  if (edgeLines) scene.remove(edgeLines);
  if (edgeGeom) edgeGeom.dispose();
  if (edgeMat) edgeMat.dispose();

  const E = state.edges.length;
  if (E === 0 || !mainGeom) return;
  const pos = new Float32Array(E * 2 * 3);
  const col = new Float32Array(E * 2 * 4); // rgba
  const mainPos = mainGeom.getAttribute("position");
  for (let i = 0; i < E; i++) {
    const e = state.edges[i];
    const a = e.a, b = e.b;
    pos[i * 6 + 0] = mainPos.getX(a);
    pos[i * 6 + 1] = mainPos.getY(a);
    pos[i * 6 + 2] = -0.01;
    pos[i * 6 + 3] = mainPos.getX(b);
    pos[i * 6 + 4] = mainPos.getY(b);
    pos[i * 6 + 5] = -0.01;
    // color = average of endpoint colors, dim
    const ar = mainGeom.getAttribute("color");
    const r = (ar.getX(a) + ar.getX(b)) * 0.5;
    const g = (ar.getY(a) + ar.getY(b)) * 0.5;
    const bl = (ar.getZ(a) + ar.getZ(b)) * 0.5;
    for (const off of [0, 4]) {
      col[i * 8 + off + 0] = r;
      col[i * 8 + off + 1] = g;
      col[i * 8 + off + 2] = bl;
      col[i * 8 + off + 3] = EDGE_ALPHA_BASE;
    }
  }
  edgeGeom = new THREE.BufferGeometry();
  edgeGeom.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  edgeGeom.setAttribute("color", new THREE.BufferAttribute(col, 4));
  edgeMat = new THREE.ShaderMaterial({
    vertexShader: `
      attribute vec4 color;
      varying vec4 vColor;
      void main() {
        vColor = color;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      varying vec4 vColor;
      void main() { gl_FragColor = vColor; }
    `,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  edgeLines = new THREE.LineSegments(edgeGeom, edgeMat);
  edgeLines.renderOrder = 1;
  scene.add(edgeLines);
}

function applyFilterMask() {
  if (!mainGeom) return;
  const N = state.records.length;
  const baseMain = mainGeom.getAttribute("aBaseSize");
  const baseHalo = haloGeom.getAttribute("aBaseSize");
  const dimAttr = mainGeom.getAttribute("aDim");
  const now = Date.now();
  const ageCutoff = now - state.filters.maxAgeDays * 24 * 3600 * 1000;
  let visible = 0;
  state.visible.length = N;
  for (let i = 0; i < N; i++) {
    const r = state.records[i];
    const srcKey = state.filters.sources.has(r.source) ? r.source : "__other__";
    const show =
      state.filters.scopes.has(r.scope) &&
      state.filters.sources.has(srcKey) &&
      state.filters.confidences.has(r.confidence) &&
      Date.parse(r.timestamp) >= ageCutoff;
    state.visible[i] = show;
    if (show) {
      const age = now - Date.parse(r.timestamp);
      const recent = age < RECENT_WINDOW_MS ? RECENT_PX_BOOST : 0;
      baseMain.setX(i, BASE_PX + recent);
      baseHalo.setX(i, HALO_PX + recent * 1.6);
      visible += 1;
    } else {
      baseMain.setX(i, 0);
      baseHalo.setX(i, 0);
    }
    // Search dim
    let dim = 1.0;
    if (state.searchHits) {
      dim = state.searchHits.has(r.id) ? 1.0 : SEARCH_DIM_ALPHA;
    }
    dimAttr.setX(i, dim);
  }
  baseMain.needsUpdate = true;
  baseHalo.needsUpdate = true;
  dimAttr.needsUpdate = true;
  document.getElementById("kpi-visible").textContent = visible;
  layoutClusterLabels();
}

// Activity --------------------------------------------------------------

function computeActivityMult(rec) {
  const age = Date.now() - Date.parse(rec.timestamp);
  const recencyDays = Math.max(0, age / (24 * 3600 * 1000));
  // Recency falls off with half-life ~3 days
  const recencyFactor = Math.exp(-recencyDays / 3);
  const accesses = state.accessCounts.get(rec.id) || 0;
  const accessFactor = 1 + Math.min(accesses, 12) / 12;  // up to 2x
  // Composite activity score → size multiplier in [1.0, 1.4]
  const score = recencyFactor * accessFactor;
  return 1.0 + Math.min(0.4, score * 0.4);
}

function recomputeActivity() {
  if (!mainGeom) return;
  const a = mainGeom.getAttribute("aActivity");
  for (let i = 0; i < state.records.length; i++) {
    a.setX(i, computeActivityMult(state.records[i]));
  }
  a.needsUpdate = true;
}

// Edges (semantic) ------------------------------------------------------

function cosine(u, v) {
  let dot = 0, nu = 0, nv = 0;
  const n = Math.min(u.length, v.length);
  for (let i = 0; i < n; i++) {
    dot += u[i] * v[i];
    nu += u[i] * u[i];
    nv += v[i] * v[i];
  }
  if (nu === 0 || nv === 0) return 0;
  return dot / (Math.sqrt(nu) * Math.sqrt(nv));
}

async function loadEdges() {
  // 1. Try server-side edge endpoint.
  try {
    const r = await fetch("/api/memory/edges");
    if (r.ok) {
      const data = await r.json();
      const idIdx = new Map(state.records.map((r, i) => [r.id, i]));
      const out = [];
      for (const [a, b, w] of (data.edges || [])) {
        const ia = idIdx.get(a);
        const ib = idIdx.get(b);
        if (ia == null || ib == null) continue;
        if (typeof w === "number" && w < EDGE_THRESHOLD) continue;
        out.push({ a: ia, b: ib, w: w ?? 1.0 });
      }
      state.edges = capEdges(out);
      buildEdgeAdj();
      return;
    }
  } catch (_) { /* fall through */ }

  // 2. Compute client-side from embeddings (if API ever returns them).
  const haveEmbeddings = state.records.length > 0 &&
    state.records.every(r => Array.isArray(r.embedding) && r.embedding.length > 0);
  if (haveEmbeddings) {
    const out = [];
    const N = state.records.length;
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        const c = cosine(state.records[i].embedding, state.records[j].embedding);
        if (c >= EDGE_THRESHOLD) out.push({ a: i, b: j, w: c });
      }
    }
    state.edges = capEdges(out);
    buildEdgeAdj();
    return;
  }

  // 3. Fallback: spatial proximity in projected 2D space. Records that
  //    PCA-projected close together are highly likely embedding-similar
  //    too — approximate semantic edges so the visual is meaningful.
  const out = [];
  const N = state.records.length;
  // Spatial index: bucket by 0.08 grid for O(N) approx neighbor lookup.
  const cell = 0.08;
  const grid = new Map();
  function key(x, y) { return Math.round(x / cell) + "," + Math.round(y / cell); }
  for (let i = 0; i < N; i++) {
    const k = key(state.records[i].x, state.records[i].y);
    if (!grid.has(k)) grid.set(k, []);
    grid.get(k).push(i);
  }
  for (let i = 0; i < N; i++) {
    const r = state.records[i];
    const cx = Math.round(r.x / cell);
    const cy = Math.round(r.y / cell);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        const cell2 = (cx + dx) + "," + (cy + dy);
        const bucket = grid.get(cell2);
        if (!bucket) continue;
        for (const j of bucket) {
          if (j <= i) continue;
          const dx2 = state.records[j].x - r.x;
          const dy2 = state.records[j].y - r.y;
          const d = Math.sqrt(dx2 * dx2 + dy2 * dy2);
          if (d < 0.07) {
            // Map distance → pseudo-similarity in [0.78, 0.95]
            const w = Math.max(EDGE_THRESHOLD, 1.0 - d * 3);
            out.push({ a: i, b: j, w });
          }
        }
      }
    }
  }
  state.edges = capEdges(out);
  buildEdgeAdj();
}

function capEdges(edges) {
  if (edges.length <= EDGE_CAP) return edges;
  // Uniform sample
  const out = [];
  const stride = edges.length / EDGE_CAP;
  for (let i = 0; i < EDGE_CAP; i++) {
    out.push(edges[Math.floor(i * stride)]);
  }
  return out;
}

function buildEdgeAdj() {
  state.edgeAdj.clear();
  for (const e of state.edges) {
    if (!state.edgeAdj.has(e.a)) state.edgeAdj.set(e.a, new Set());
    if (!state.edgeAdj.has(e.b)) state.edgeAdj.set(e.b, new Set());
    state.edgeAdj.get(e.a).add(e.b);
    state.edgeAdj.get(e.b).add(e.a);
  }
}

function setEdgeAlphasForHover(hoveredIdx) {
  if (!edgeGeom) return;
  const col = edgeGeom.getAttribute("color");
  const adj = hoveredIdx >= 0 ? state.edgeAdj.get(hoveredIdx) : null;
  // second-degree
  const second = new Set();
  if (adj) for (const n of adj) {
    const an = state.edgeAdj.get(n);
    if (an) for (const m of an) if (m !== hoveredIdx && !adj.has(m)) second.add(m);
  }
  for (let i = 0; i < state.edges.length; i++) {
    const e = state.edges[i];
    let a = EDGE_ALPHA_BASE;
    if (hoveredIdx >= 0) {
      const isPrimary = e.a === hoveredIdx || e.b === hoveredIdx;
      const isSecondary =
        (adj && (adj.has(e.a) || adj.has(e.b))) ||
        (second.size && (second.has(e.a) || second.has(e.b)));
      if (isPrimary) a = EDGE_ALPHA_PRIMARY;
      else if (isSecondary) a = EDGE_ALPHA_SECONDARY;
      else a = 0.05;
    }
    col.setW(i * 2 + 0, a);
    col.setW(i * 2 + 1, a);
  }
  col.needsUpdate = true;
}

// Cluster auto-labels (DOM overlay) ------------------------------------

let clusters = []; // [{cx, cy, label, recordIdx}]

function recomputeClusters() {
  clusters = [];
  if (state.records.length === 0) {
    layoutClusterLabels();
    return;
  }
  const N = state.records.length;
  const assigned = new Uint8Array(N);
  for (let i = 0; i < N; i++) {
    if (assigned[i]) continue;
    const r = state.records[i];
    const members = [i];
    assigned[i] = 1;
    for (let j = i + 1; j < N; j++) {
      if (assigned[j]) continue;
      const dx = state.records[j].x - r.x;
      const dy = state.records[j].y - r.y;
      if (Math.sqrt(dx * dx + dy * dy) < CLUSTER_RADIUS) {
        members.push(j);
        assigned[j] = 1;
      }
    }
    if (members.length < 3) continue;
    // Pick most-recent decision-type record (or fallback: most recent)
    let pick = -1, pickTs = "";
    for (const idx of members) {
      const rec = state.records[idx];
      const isDecision = (rec.type || "").toLowerCase().includes("decision");
      const ts = rec.timestamp;
      if (isDecision && (pick < 0 || ts > pickTs)) {
        pick = idx;
        pickTs = ts;
      }
    }
    if (pick < 0) {
      for (const idx of members) {
        if (state.records[idx].timestamp > pickTs) {
          pick = idx;
          pickTs = state.records[idx].timestamp;
        }
      }
    }
    if (pick < 0) continue;
    let cx = 0, cy = 0;
    for (const idx of members) {
      cx += state.records[idx].x;
      cy += state.records[idx].y;
    }
    cx /= members.length;
    cy /= members.length;
    const words = (state.records[pick].content || "").trim().split(/\s+/).slice(0, 5).join(" ");
    clusters.push({ cx, cy, label: words || state.records[pick].type, size: members.length });
  }
  clusters.sort((a, b) => b.size - a.size);
  if (clusters.length > MAX_CLUSTER_LABELS) clusters.length = MAX_CLUSTER_LABELS;
  _labelCamSig = null;  // clusters changed → force a re-layout
  layoutClusterLabels();
}

function worldToScreen(x, y) {
  const v = new THREE.Vector3(x, y, 0);
  v.project(camera);
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  return { x: (v.x * 0.5 + 0.5) * w, y: (-v.y * 0.5 + 0.5) * h };
}

let _labelCamSig = null;
// Re-lay-out cluster labels only when the camera moved (or clusters changed,
// which nulls the sig) — labels track world centroids, so a static camera
// means identical labels. Avoids rebuilding ~12 DOM nodes every frame. (D4 perf)
function maybeLayoutClusterLabels() {
  if (!clusterLabelsHost) return;
  const sig = camera.zoom.toFixed(4) + "|" +
    camera.position.x.toFixed(2) + "|" + camera.position.y.toFixed(2);
  if (sig === _labelCamSig) return;
  _labelCamSig = sig;
  layoutClusterLabels();
}

function layoutClusterLabels() {
  if (!clusterLabelsHost) return;
  clusterLabelsHost.innerHTML = "";
  // Scale font with zoom: smaller when zoomed out.
  const zoom = camera.zoom;
  const fontPx = Math.max(9, Math.min(15, 10 + Math.log2(Math.max(0.5, zoom)) * 1.6));
  const opacity = Math.min(0.85, 0.35 + zoom * 0.18);
  for (const c of clusters) {
    const s = worldToScreen(c.cx, c.cy);
    const div = document.createElement("div");
    div.className = "cluster-label";
    div.textContent = c.label;
    div.style.left = s.x + "px";
    div.style.top = s.y + "px";
    div.style.fontSize = fontPx.toFixed(1) + "px";
    div.style.opacity = String(opacity);
    clusterLabelsHost.appendChild(div);
  }
}

// Pulse arcs (transient curves on memory.written) ----------------------

function spawnPulseArcs(toIdx) {
  // Pick top-3 nearest existing records (proxy for top-3 cosine)
  if (state.records.length < 2) return;
  const target = state.records[toIdx];
  const cands = [];
  for (let i = 0; i < state.records.length; i++) {
    if (i === toIdx) continue;
    const dx = state.records[i].x - target.x;
    const dy = state.records[i].y - target.y;
    cands.push({ i, d: Math.sqrt(dx * dx + dy * dy) });
  }
  cands.sort((a, b) => a.d - b.d);
  const N = Math.min(3, cands.length);
  const now = performance.now();
  for (let k = 0; k < N; k++) {
    state.pulseArcs.push({ fromIdx: cands[k].i, toIdx, start: now });
  }
}

function rebuildArcLayer() {
  // Build / refresh dynamic line segments for active arcs.
  if (arcLines) scene.remove(arcLines);
  if (arcGeom) arcGeom.dispose();
  if (arcMat) arcMat.dispose();
  arcLines = null;

  const SEG = 12;
  const live = state.pulseArcs;
  if (live.length === 0) return;

  const positions = new Float32Array(live.length * SEG * 2 * 3);
  const colors = new Float32Array(live.length * SEG * 2 * 4);
  const now = performance.now();
  let segIdx = 0;
  const remain = [];

  for (const arc of live) {
    const t = (now - arc.start) / 1500;
    if (t > 1) continue;
    remain.push(arc);
    if (!state.records[arc.fromIdx] || !state.records[arc.toIdx]) continue;
    const a = state.records[arc.fromIdx];
    const b = state.records[arc.toIdx];
    // Midpoint with lift for arc.
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 0.0001;
    const nx = -dy / len;
    const ny = dx / len;
    const lift = Math.min(0.18, len * 0.35);
    const cx = mx + nx * lift;
    const cy = my + ny * lift;
    const fade = 1.0 - t;
    // Sweep effect: only render up to progress fraction, glowing tip.
    const head = Math.min(1.0, t * 1.6);
    for (let s = 0; s < SEG; s++) {
      const u0 = (s / SEG) * head;
      const u1 = ((s + 1) / SEG) * head;
      // Quadratic Bézier
      const p0 = quadBezier(a.x, a.y, cx, cy, b.x, b.y, u0);
      const p1 = quadBezier(a.x, a.y, cx, cy, b.x, b.y, u1);
      const off = (segIdx) * 6;
      positions[off + 0] = p0[0];
      positions[off + 1] = p0[1];
      positions[off + 2] = -0.005;
      positions[off + 3] = p1[0];
      positions[off + 4] = p1[1];
      positions[off + 5] = -0.005;
      // Color (accent-2 blue), with brighter tip
      const tipBoost = (s / SEG);
      const alpha = fade * (0.4 + 0.6 * tipBoost);
      const cOff = segIdx * 8;
      for (const k of [0, 4]) {
        colors[cOff + k + 0] = 0.35 + 0.4 * tipBoost;
        colors[cOff + k + 1] = 0.65 + 0.3 * tipBoost;
        colors[cOff + k + 2] = 1.0;
        colors[cOff + k + 3] = alpha;
      }
      segIdx++;
    }
  }
  state.pulseArcs = remain;
  if (segIdx === 0) return;

  arcGeom = new THREE.BufferGeometry();
  arcGeom.setAttribute("position", new THREE.BufferAttribute(positions.subarray(0, segIdx * 6), 3));
  arcGeom.setAttribute("color", new THREE.BufferAttribute(colors.subarray(0, segIdx * 8), 4));
  arcMat = new THREE.ShaderMaterial({
    vertexShader: `
      attribute vec4 color;
      varying vec4 vColor;
      void main() {
        vColor = color;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      varying vec4 vColor;
      void main() { gl_FragColor = vColor; }
    `,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  arcLines = new THREE.LineSegments(arcGeom, arcMat);
  arcLines.renderOrder = 4;
  scene.add(arcLines);
}

function quadBezier(ax, ay, bx, by, cx, cy, t) {
  const u = 1 - t;
  return [
    u * u * ax + 2 * u * t * bx + t * t * cx,
    u * u * ay + 2 * u * t * by + t * t * cy,
  ];
}

// Live-thinking animation (memory.chat) --------------------------------

let chatHaloTexture = null;
function makeRadialTexture() {
  if (chatHaloTexture) return chatHaloTexture;
  const size = 128;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const ctx = c.getContext("2d");
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0.0, "rgba(255,255,255,1)");
  grad.addColorStop(0.35, "rgba(255,255,255,0.65)");
  grad.addColorStop(0.7, "rgba(255,255,255,0.18)");
  grad.addColorStop(1.0, "rgba(255,255,255,0)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  chatHaloTexture = new THREE.CanvasTexture(c);
  chatHaloTexture.minFilter = THREE.LinearFilter;
  chatHaloTexture.magFilter = THREE.LinearFilter;
  return chatHaloTexture;
}

function buildRippleMesh(cx, cy, color) {
  const SEG = 64;
  const positions = new Float32Array(SEG * 3);
  for (let i = 0; i < SEG; i++) {
    const a = (i / SEG) * Math.PI * 2;
    positions[i * 3 + 0] = Math.cos(a);
    positions[i * 3 + 1] = Math.sin(a);
    positions[i * 3 + 2] = -0.002;
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const mat = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity: 0.0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const mesh = new THREE.LineLoop(geom, mat);
  mesh.position.set(cx, cy, 0);
  mesh.scale.set(0.0001, 0.0001, 1);
  mesh.renderOrder = 5;
  return mesh;
}

function buildPulseHalo(x, y, color) {
  const tex = makeRadialTexture();
  const mat = new THREE.SpriteMaterial({
    map: tex,
    color,
    transparent: true,
    opacity: 0.0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const s = new THREE.Sprite(mat);
  s.position.set(x, y, -0.0015);
  s.scale.set(0.001, 0.001, 1);
  s.renderOrder = 4;
  return s;
}

function buildBeamMesh(ax, ay, bx, by, color) {
  // straight line from star (a) to focal anchor (b), with a midpoint vertex
  // so we can fade alpha along the segment if desired.
  const positions = new Float32Array(2 * 3);
  positions[0] = ax; positions[1] = ay; positions[2] = -0.003;
  positions[3] = bx; positions[4] = by; positions[5] = -0.003;
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const mat = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity: 0.0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const line = new THREE.Line(geom, mat);
  line.renderOrder = 5;
  return line;
}

function spawnChatTrack(payload) {
  if (!payload) return;
  const retrieved = Array.isArray(payload.retrieved_record_ids) ? payload.retrieved_record_ids : [];
  const cited = Array.isArray(payload.cited_record_ids) ? payload.cited_record_ids : [];
  // Drop gracefully if at concurrency cap. State is already updated by the
  // event consumers (bumpAccess etc.); we only skip the visual track.
  if (state.chatTracks.length >= CHAT_TRACK_CAP) return;

  const idIdx = new Map(state.records.map((r, i) => [r.id, i]));
  const retrievedIdx = retrieved.map((id) => idIdx.has(id) ? idIdx.get(id) : -1);
  const citedIdx = cited.map((id) => idIdx.has(id) ? idIdx.get(id) : -1);
  const cx = camera.position.x;
  const cy = camera.position.y;

  // Ripple at focal anchor.
  const ripple = buildRippleMesh(cx, cy, CHAT_RIPPLE_COLOR);
  scene.add(ripple);

  // Pulse halos for retrieved stars.
  const pulses = [];
  for (let k = 0; k < retrievedIdx.length; k++) {
    const idx = retrievedIdx[k];
    if (idx < 0) { pulses.push(null); continue; }
    const r = state.records[idx];
    const halo = buildPulseHalo(r.x, r.y, CHAT_PULSE_COLOR);
    scene.add(halo);
    pulses.push({ idx, halo, delay: k * CHAT_PULSE_STAGGER_MS, bloomed: false });
  }

  // Beams for cited stars.
  const beams = [];
  for (let k = 0; k < citedIdx.length; k++) {
    const idx = citedIdx[k];
    if (idx < 0) { beams.push(null); continue; }
    const r = state.records[idx];
    const line = buildBeamMesh(r.x, r.y, cx, cy, CHAT_BEAM_COLOR);
    scene.add(line);
    beams.push({ idx, line, delay: CHAT_BEAM_DELAY_MS + k * CHAT_BEAM_STAGGER_MS });
  }

  state.chatTracks.push({
    start: performance.now(),
    cx, cy,
    ripple,
    pulses,
    beams,
  });
}

function clearChatTrack(track) {
  if (track.ripple) {
    scene.remove(track.ripple);
    track.ripple.geometry.dispose();
    track.ripple.material.dispose();
    track.ripple = null;
  }
  for (const p of track.pulses) {
    if (!p || !p.halo) continue;
    scene.remove(p.halo);
    if (p.halo.material) p.halo.material.dispose();
    p.halo = null;
  }
  for (const b of track.beams) {
    if (!b || !b.line) continue;
    scene.remove(b.line);
    b.line.geometry.dispose();
    b.line.material.dispose();
    b.line = null;
  }
}

function tickChatAnims(nowPerf) {
  if (state.chatTracks.length === 0) return;
  const still = [];
  for (const track of state.chatTracks) {
    const t = nowPerf - track.start;
    let alive = false;

    // Ripple — scale 0 -> CHAT_RIPPLE_MAX_WORLD over CHAT_RIPPLE_DUR_MS.
    if (track.ripple) {
      const u = t / CHAT_RIPPLE_DUR_MS;
      if (u < 1) {
        const e = 1 - Math.pow(1 - u, 2); // easeOutQuad
        const r = e * CHAT_RIPPLE_MAX_WORLD;
        track.ripple.scale.set(r, r, 1);
        track.ripple.material.opacity = 0.55 * (1 - u);
        alive = true;
      } else {
        scene.remove(track.ripple);
        track.ripple.geometry.dispose();
        track.ripple.material.dispose();
        track.ripple = null;
      }
    }

    // Retrieval pulse halos — green tint, size×1.6 over 200ms then back over 600ms.
    for (const p of track.pulses) {
      if (!p || !p.halo) continue;
      const tp = t - p.delay;
      if (tp < 0) {
        p.halo.material.opacity = 0;
        alive = true;
        continue;
      }
      const dur = CHAT_PULSE_BOOST_MS + CHAT_PULSE_RELAX_MS;
      if (tp >= dur) {
        scene.remove(p.halo);
        if (p.halo.material) p.halo.material.dispose();
        p.halo = null;
        continue;
      }
      // Boost phase: ease-out 0 -> peak. Relax phase: ease-out peak -> 0.
      let scaleMult, opacity;
      if (tp < CHAT_PULSE_BOOST_MS) {
        const u = tp / CHAT_PULSE_BOOST_MS;
        const e = 1 - Math.pow(1 - u, 2);
        scaleMult = 0.04 + e * (0.16 - 0.04);
        opacity = 0.85 * e;
      } else {
        const u = (tp - CHAT_PULSE_BOOST_MS) / CHAT_PULSE_RELAX_MS;
        const e = 1 - Math.pow(1 - u, 2);
        scaleMult = 0.16 - e * (0.16 - 0.04);
        opacity = 0.85 * (1 - e);
      }
      // Scale by inverse zoom so the halo stays roughly constant on screen.
      const s = scaleMult / Math.max(0.5, camera.zoom);
      p.halo.scale.set(s, s, 1);
      p.halo.material.opacity = opacity;
      // Push a brightness boost on the underlying star (size).
      if (!p.bloomed && tp >= 0) {
        // Use the highlights system to bump aBloom.
        state.highlights.push({
          recordIdx: p.idx,
          start: nowPerf - 50,         // already started
          strong: false,
        });
        p.bloomed = true;
      }
      alive = true;
    }

    // Citation beams — appear after delay, hold, then fade.
    for (const b of track.beams) {
      if (!b || !b.line) continue;
      const tb = t - b.delay;
      if (tb < 0) {
        b.line.material.opacity = 0;
        alive = true;
        continue;
      }
      const total = CHAT_BEAM_HOLD_MS;
      const fadeStart = total - CHAT_BEAM_FADE_MS;
      if (tb >= total) {
        scene.remove(b.line);
        b.line.geometry.dispose();
        b.line.material.dispose();
        b.line = null;
        continue;
      }
      let opacity;
      if (tb < 120) {
        opacity = (tb / 120) * 0.9;
      } else if (tb < fadeStart) {
        opacity = 0.9;
      } else {
        opacity = 0.9 * (1 - (tb - fadeStart) / CHAT_BEAM_FADE_MS);
      }
      b.line.material.opacity = opacity;
      alive = true;
    }

    if (alive) still.push(track);
  }
  state.chatTracks = still;
}

// Pan / zoom ------------------------------------------------------------

let dragging = false;
let dragLast = null;
canvas.addEventListener("mousedown", (e) => {
  dragging = true;
  dragLast = { x: e.clientX, y: e.clientY };
});
window.addEventListener("mouseup", () => { dragging = false; dragLast = null; });
canvas.addEventListener("mousemove", (e) => {
  if (!dragging) {
    handleHover(e);
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const dx = (e.clientX - dragLast.x) / rect.width * (camera.right - camera.left) / camera.zoom;
  const dy = (e.clientY - dragLast.y) / rect.height * (camera.top - camera.bottom) / camera.zoom;
  camera.position.x -= dx;
  camera.position.y += dy;
  dragLast = { x: e.clientX, y: e.clientY };
  hideTooltip();
});
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const factor = Math.exp(e.deltaY * 0.0015);
  camera.zoom = Math.max(0.35, Math.min(25, camera.zoom / factor));
  camera.updateProjectionMatrix();
}, { passive: false });

// Picking + hover + click ----------------------------------------------

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

function worldPickThreshold() {
  const w = canvas.clientWidth || 1;
  const worldPerPx = (camera.right - camera.left) / w / camera.zoom;
  const starRadiusPx = (BASE_PX * HOVER_MULT) / 2 + 10;
  return starRadiusPx * worldPerPx;
}

function pickIndexAt(e) {
  if (!mainPoints) return -1;
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  raycaster.params.Points.threshold = worldPickThreshold();
  const hits = raycaster.intersectObject(mainPoints, false);
  for (const h of hits) {
    if (state.visible[h.index]) return h.index;
  }
  return -1;
}

function setHovered(idx) {
  if (state.hoveredIdx === idx) return;
  const attr = mainGeom.getAttribute("aHover");
  if (state.hoveredIdx >= 0) attr.setX(state.hoveredIdx, 0);
  if (idx >= 0) attr.setX(idx, 1);
  attr.needsUpdate = true;
  state.hoveredIdx = idx;
  setEdgeAlphasForHover(idx);
}

function setSelected(idx) {
  if (state.selectedIdx === idx) return;
  const attr = mainGeom.getAttribute("aSelected");
  if (state.selectedIdx >= 0) attr.setX(state.selectedIdx, 0);
  if (idx >= 0) attr.setX(idx, 1);
  attr.needsUpdate = true;
  state.selectedIdx = idx;
}

// Hover side-panel ------------------------------------------------------
// Debounced 150ms reveal on hover; edge-aware anchor; pointer-event-aware so
// the user can move into it to click "open record page".

const HOVER_DEBOUNCE_MS = 150;
const HOVER_PANEL_WIDTH = 360;
const HOVER_PANEL_HMAX = 280;
const hoverDebounce = {
  timer: null,
  pendingIdx: -1,
  pendingEvent: null,
  shownIdx: -1,
  insidePanel: false,
};

function handleHover(e) {
  const idx = pickIndexAt(e);
  setHovered(idx);
  if (idx < 0) {
    canvas.style.cursor = "default";
    scheduleHidePanel();
    cancelPendingShow();
    return;
  }
  canvas.style.cursor = "pointer";
  if (idx !== hoverDebounce.shownIdx) {
    schedulePanel(idx, e);
  } else if (hoverPanel) {
    // Already shown for this idx; reposition only if mouse moved a lot.
    // Skipping reposition keeps the panel stable while mouse hovers.
  }
}

function schedulePanel(idx, e) {
  cancelPendingShow();
  hoverDebounce.pendingIdx = idx;
  hoverDebounce.pendingEvent = { clientX: e.clientX, clientY: e.clientY };
  hoverDebounce.timer = setTimeout(() => {
    hoverDebounce.timer = null;
    if (hoverDebounce.pendingIdx === idx && state.records[idx]) {
      showHoverPanel(idx, hoverDebounce.pendingEvent);
    }
  }, HOVER_DEBOUNCE_MS);
}

function cancelPendingShow() {
  if (hoverDebounce.timer) {
    clearTimeout(hoverDebounce.timer);
    hoverDebounce.timer = null;
  }
  hoverDebounce.pendingIdx = -1;
  hoverDebounce.pendingEvent = null;
}

function scheduleHidePanel() {
  // Give the panel's mouseenter handler a tick to flip insidePanel = true if
  // the cursor moved into the panel rather than off the canvas entirely.
  setTimeout(() => {
    if (hoverDebounce.insidePanel) return;
    hideHoverPanel();
  }, 30);
}

function hideHoverPanel() {
  if (!hoverPanel) return;
  hoverPanel.classList.remove("visible");
  hoverPanel.setAttribute("aria-hidden", "true");
  hoverDebounce.shownIdx = -1;
  hoverDebounce.insidePanel = false;
}

function fmtTimestamp(ts) {
  if (!ts) return "—";
  return ts.slice(0, 19).replace("T", " ");
}

function showHoverPanel(idx, anchor) {
  if (!hoverPanel) return;
  const rec = state.records[idx];
  if (!rec) return;

  // Build content.
  hoverPanel.innerHTML = "";
  const head = document.createElement("div");
  head.className = "hp-head";
  const idShort = (rec.id || "").slice(0, 8);
  head.innerHTML = "";
  const pieces = [
    `id:${idShort}`,
    rec.type || "—",
    rec.source || "—",
    rec.confidence || "—",
  ];
  head.textContent = pieces.join("  ·  ");
  hoverPanel.appendChild(head);

  const meta = document.createElement("div");
  meta.className = "hp-meta";
  const scopeLine = document.createElement("div");
  scopeLine.className = "hp-meta-row";
  scopeLine.innerHTML =
    `<span class="k">scope</span>` +
    `<span class="v">${escapeHtml(rec.scope || "—")}` +
    (rec.scope_id ? ` / ${escapeHtml(rec.scope_id)}` : "") +
    `</span>`;
  meta.appendChild(scopeLine);

  const tsLine = document.createElement("div");
  tsLine.className = "hp-meta-row";
  tsLine.innerHTML =
    `<span class="k">written</span>` +
    `<span class="v">${escapeHtml(fmtTimestamp(rec.timestamp))}</span>`;
  meta.appendChild(tsLine);
  hoverPanel.appendChild(meta);

  const sep = document.createElement("div");
  sep.className = "hp-sep";
  hoverPanel.appendChild(sep);

  const body = document.createElement("div");
  body.className = "hp-body";
  body.textContent = rec.content || "";
  hoverPanel.appendChild(body);

  if (rec.tags && rec.tags.length) {
    const sep2 = document.createElement("div");
    sep2.className = "hp-sep";
    hoverPanel.appendChild(sep2);
    const tags = document.createElement("div");
    tags.className = "hp-tags";
    tags.textContent = "tags: " + rec.tags.join(", ");
    hoverPanel.appendChild(tags);
  }

  const link = document.createElement("a");
  link.className = "hp-link";
  link.href = "/memory#focus=" + encodeURIComponent(rec.id);
  link.textContent = "↗ open record page";
  link.addEventListener("click", (ev) => {
    // On the same /memory page, clicking the link should focus + bypass full reload.
    if (location.pathname === "/memory" || location.pathname === "/static/memory.html") {
      ev.preventDefault();
      location.hash = "focus=" + encodeURIComponent(rec.id);
      window.dispatchEvent(new CustomEvent("constellation:focus", { detail: { recordId: rec.id } }));
      hideHoverPanel();
    }
  });
  hoverPanel.appendChild(link);

  // Position — edge-aware.
  const rect = canvas.getBoundingClientRect();
  const cursorX = anchor.clientX - rect.left;
  const cursorY = anchor.clientY - rect.top;
  const offset = 16;
  let left = cursorX + offset;
  if (cursorX + offset + HOVER_PANEL_WIDTH > rect.width - 8) {
    left = cursorX - offset - HOVER_PANEL_WIDTH;
  }
  let top = cursorY + offset;
  if (top + HOVER_PANEL_HMAX > rect.height - 8) {
    top = Math.max(8, rect.height - HOVER_PANEL_HMAX - 8);
  }
  if (left < 8) left = 8;
  hoverPanel.style.left = left + "px";
  hoverPanel.style.top = top + "px";
  hoverPanel.classList.add("visible");
  hoverPanel.setAttribute("aria-hidden", "false");
  hoverDebounce.shownIdx = idx;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

if (hoverPanel) {
  hoverPanel.addEventListener("mouseenter", () => {
    hoverDebounce.insidePanel = true;
  });
  hoverPanel.addEventListener("mouseleave", () => {
    hoverDebounce.insidePanel = false;
    hideHoverPanel();
  });
}

// Tap-to-show on touch devices: tap = single touchstart with no drag.
let touchStartedAt = 0;
canvas.addEventListener("touchstart", (e) => {
  touchStartedAt = performance.now();
  if (e.touches.length !== 1) return;
  const t = e.touches[0];
  const idx = pickIndexAt({ clientX: t.clientX, clientY: t.clientY });
  if (idx >= 0) {
    setHovered(idx);
    showHoverPanel(idx, { clientX: t.clientX, clientY: t.clientY });
  } else {
    hideHoverPanel();
  }
}, { passive: true });

// Backwards-compat shims (other code may still call these names).
function hideTooltip() { hideHoverPanel(); }

canvas.addEventListener("click", (e) => {
  const idx = pickIndexAt(e);
  if (idx < 0) {
    setSelected(-1);
    detail.classList.remove("open");
    return;
  }
  setSelected(idx);
  bumpAccess(state.records[idx].id);
  openDetail(state.records[idx]);
});

canvas.addEventListener("mouseleave", () => {
  setHovered(-1);
  cancelPendingShow();
  // If cursor moved into the hover panel, keep the panel; only the canvas
  // hover state goes away.
  scheduleHidePanel();
});

function openDetail(rec) {
  document.getElementById("detail-content").textContent = rec.content;
  const prov = document.getElementById("detail-prov");
  prov.innerHTML = "";
  const pills = [
    { text: `${SCOPE_GLYPH[rec.scope] || ""} scope:${rec.scope}`, cls: "info" },
    { text: `source:${rec.source}`, cls: "" },
    { text: `conf:${rec.confidence}`, cls: "" },
    { text: `type:${rec.type}`, cls: "" },
  ];
  for (const p of pills) {
    const s = document.createElement("span");
    s.className = "pill " + p.cls;
    s.textContent = p.text;
    prov.appendChild(s);
  }
  const meta = document.getElementById("detail-meta");
  meta.innerHTML = "";
  const accesses = state.accessCounts.get(rec.id) || 0;
  const rows = [
    ["id", rec.id],
    ["scope_id", rec.scope_id],
    ["timestamp", rec.timestamp],
    ["tags", (rec.tags || []).join(", ") || "—"],
    ["has_embedding", String(rec.has_embedding)],
    ["accesses (session)", String(accesses)],
  ];
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    meta.appendChild(dt); meta.appendChild(dd);
  }
  detail.classList.add("open");
}

document.getElementById("detail-close").addEventListener("click", () => {
  detail.classList.remove("open");
  setSelected(-1);
});

// Keyboard shortcuts ----------------------------------------------------

window.addEventListener("keydown", (e) => {
  // Skip if typing in input fields.
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (e.key === "Escape") {
    detail.classList.remove("open");
    setSelected(-1);
    hideTooltip();
  } else if (e.key === "r" || e.key === "R") {
    camera.position.set(0, 0, 10);
    camera.zoom = 1;
    camera.updateProjectionMatrix();
    state.cameraTween = null;
  } else if (e.key === "f" || e.key === "F") {
    cinematicFitAll();
  } else if (e.key === "+" || e.key === "=") {
    camera.zoom = Math.min(25, camera.zoom * 1.2);
    camera.updateProjectionMatrix();
  } else if (e.key === "-" || e.key === "_") {
    camera.zoom = Math.max(0.35, camera.zoom / 1.2);
    camera.updateProjectionMatrix();
  }
});

function cinematicFitAll() {
  if (state.records.length === 0) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let i = 0; i < state.records.length; i++) {
    if (!state.visible[i]) continue;
    const r = state.records[i];
    if (r.x < minX) minX = r.x;
    if (r.x > maxX) maxX = r.x;
    if (r.y < minY) minY = r.y;
    if (r.y > maxY) maxY = r.y;
  }
  if (!isFinite(minX)) return;
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const spanX = (maxX - minX) || 0.5;
  const spanY = (maxY - minY) || 0.5;
  const aspect = (camera.right - camera.left) / (camera.top - camera.bottom);
  const targetZoom = 0.9 * Math.min(
    (camera.right - camera.left) / (spanX || 1),
    (camera.top - camera.bottom) / (spanY || 1),
  );
  state.cameraTween = {
    t0: performance.now(),
    dur: 380,
    fromPos: { x: camera.position.x, y: camera.position.y },
    toPos: { x: cx, y: cy },
    fromZoom: camera.zoom,
    toZoom: Math.max(0.4, Math.min(8, targetZoom)),
  };
}

function tickCameraTween(now) {
  const tw = state.cameraTween;
  if (!tw) return;
  const t = Math.min(1, (now - tw.t0) / tw.dur);
  const e = 1 - Math.pow(1 - t, 3); // easeOutCubic
  camera.position.x = tw.fromPos.x + (tw.toPos.x - tw.fromPos.x) * e;
  camera.position.y = tw.fromPos.y + (tw.toPos.y - tw.fromPos.y) * e;
  camera.zoom = tw.fromZoom + (tw.toZoom - tw.fromZoom) * e;
  camera.updateProjectionMatrix();
  if (t >= 1) state.cameraTween = null;
}

function focusOnRecord(recordId) {
  const idx = state.records.findIndex(r => r.id === recordId);
  if (idx < 0) return;
  const r = state.records[idx];
  state.cameraTween = {
    t0: performance.now(),
    dur: 380,
    fromPos: { x: camera.position.x, y: camera.position.y },
    toPos: { x: r.x, y: r.y },
    fromZoom: camera.zoom,
    toZoom: Math.max(camera.zoom, 4.0),
  };
  // Glow for 2s
  state.highlights.push({ recordIdx: idx, start: performance.now(), strong: true });
  setSelected(idx);
  bumpAccess(r.id);
}

window.addEventListener("constellation:focus", (ev) => {
  const id = ev.detail && ev.detail.recordId;
  if (id) focusOnRecord(id);
});

// Filters UI ------------------------------------------------------------

document.querySelectorAll("#filters input[type=checkbox]").forEach((cb) => {
  cb.addEventListener("change", () => {
    rebuildFiltersFromUi();
    applyFilterMask();
  });
});

document.getElementById("age-slider").addEventListener("input", (e) => {
  // The top of the range means "all" (unbounded) so old records in this
  // durable store stay reachable; anything below is a day cutoff.
  const v = Number(e.target.value);
  const isAll = v > 365;
  state.filters.maxAgeDays = isAll ? Infinity : v;
  document.getElementById("age-label").textContent = isAll ? "all" : v + "d";
  applyFilterMask();
});

function rebuildFiltersFromUi() {
  const sets = { Scope: new Set(), Source: new Set(), Confidence: new Set() };
  let current = null;
  for (const node of document.getElementById("filters").children) {
    if (node.tagName === "H3") current = node.textContent.trim().split(/\s/)[0];
    else if (node.tagName === "LABEL" && current in sets) {
      const cb = node.querySelector("input[type=checkbox]");
      if (cb && cb.checked) sets[current].add(cb.value);
    }
  }
  state.filters.scopes = sets.Scope;
  state.filters.sources = sets.Source;
  state.filters.confidences = sets.Confidence;
}

// Search ----------------------------------------------------------------

let searchTimer = null;
let searchSeq = 0;

if (searchInput) {
  searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (!q) {
      state.searchHits = null;
      searchHits.textContent = "";
      applyFilterMask();
      return;
    }
    searchTimer = setTimeout(() => runSearch(q), 180);
  });
}
if (searchClear) {
  searchClear.addEventListener("click", () => {
    searchInput.value = "";
    state.searchHits = null;
    searchHits.textContent = "";
    applyFilterMask();
  });
}

async function runSearch(q) {
  const seq = ++searchSeq;
  try {
    const r = await fetch("/api/memory/search?q=" + encodeURIComponent(q) + "&limit=50");
    if (!r.ok) {
      // local fallback
      localSearchFallback(q);
      return;
    }
    if (seq !== searchSeq) return;
    const data = await r.json();
    const ids = (data.records || data.results || []).map(rr => rr.id || rr.record_id).filter(Boolean);
    if (ids.length === 0) {
      localSearchFallback(q);
      return;
    }
    state.searchHits = new Set(ids);
    searchHits.textContent = ids.length + " hit" + (ids.length === 1 ? "" : "s");
    applyFilterMask();
  } catch (_) {
    localSearchFallback(q);
  }
}

function localSearchFallback(q) {
  const lq = q.toLowerCase();
  const hits = new Set();
  for (const r of state.records) {
    const hay = (r.content + " " + (r.tags || []).join(" ") + " " + r.type).toLowerCase();
    if (hay.includes(lq)) hits.add(r.id);
  }
  state.searchHits = hits;
  searchHits.textContent = hits.size + " hit" + (hits.size === 1 ? "" : "s") + " (local)";
  applyFilterMask();
}

// Activity tracking -----------------------------------------------------

function bumpAccess(recordId) {
  state.accessCounts.set(recordId, (state.accessCounts.get(recordId) || 0) + 1);
  recomputeActivity();
  updateMood();
}

// Mood / background ----------------------------------------------------

function updateMood() {
  // Aggregate activity over recent window: how many records accessed,
  // how many fresh writes. Quiet → grey-blue, busy → faint cyan.
  let total = 0;
  for (const v of state.accessCounts.values()) total += v;
  const score = Math.min(1, total / 30 + state.newThisSession / 12);
  state.moodActivity = score;
  if (canvasWrap) {
    canvasWrap.style.setProperty("--mood-blue", String(0.04 + 0.10 * score));
    canvasWrap.style.setProperty("--mood-green", String(0.02 + 0.05 * score));
  }
}

// Hash helper ----------------------------------------------------------

function simpleHash(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

// Animation -------------------------------------------------------------

function driftAmbient() {
  if (!ambientPoints) return;
  const pos = ambientPoints.geometry.getAttribute("position");
  const drifts = ambientPoints.geometry.userData.drifts;
  for (let i = 0; i < pos.count; i++) {
    let x = pos.getX(i) + drifts[i * 2] * 0.016;
    let y = pos.getY(i) + drifts[i * 2 + 1] * 0.016;
    if (x > 2) x = -2;
    if (x < -2) x = 2;
    if (y > 2) y = -2;
    if (y < -2) y = 2;
    pos.setXY(i, x, y);
  }
  pos.needsUpdate = true;
}

let _bloomsDirty = false;
function tickBlooms(nowPerf) {
  if (!mainGeom) return;
  const attr = mainGeom.getAttribute("aBloom");
  if (state.highlights.length === 0) {
    // No active blooms. Only clear + re-upload the whole point buffer once —
    // on the frame we transition to empty — then skip entirely while idle,
    // instead of zeroing every point and uploading every frame. (D4 perf)
    if (_bloomsDirty) {
      for (let i = 0; i < attr.count; i++) attr.setX(i, 0);
      attr.needsUpdate = true;
      _bloomsDirty = false;
    }
    return;
  }
  for (let i = 0; i < attr.count; i++) attr.setX(i, 0);
  const still = [];
  for (const h of state.highlights) {
    if (!state.visible[h.recordIdx]) continue;
    const t = nowPerf - h.start;
    const dur = h.strong ? 2000 : 2400;
    if (t > dur) continue;
    still.push(h);
    let boost;
    if (t < 300) {
      boost = (t / 300) * 28;
    } else if (t < 700) {
      boost = (1 - (t - 300) / 400) * 28 + 8;
    } else {
      boost = Math.max(0, (1 - (t - 700) / (dur - 700)) * 6);
    }
    if (h.strong) boost *= 1.4;
    attr.setX(h.recordIdx, (attr.getX(h.recordIdx) || 0) + boost);
  }
  attr.needsUpdate = true;
  state.highlights = still;
}

// Slow camera orbit (azimuth) ------------------------------------------

let azimuthLast = performance.now();
function updateAzimuth(now) {
  const dt = (now - azimuthLast) / 1000;
  azimuthLast = now;
  // ±0.5° = 0.00873 rad over 30s => period 60s
  state.azimuth += dt * (2 * Math.PI / 60);
  // Apply tiny offset to scene rotation to feel like orbital drift.
  // Hide if user is interacting heavily? Keep subtle: ±0.0087 rad of rotation.
  const angle = Math.sin(state.azimuth) * 0.00873;
  if (mainPoints) mainPoints.rotation.z = angle * 0.5;
  if (haloPoints) haloPoints.rotation.z = angle * 0.5;
  if (edgeLines) edgeLines.rotation.z = angle * 0.5;
}

function animate(nowPerf) {
  if (mainMat) {
    mainMat.uniforms.uTime.value = nowPerf;
    mainMat.uniforms.uZoom.value = camera.zoom;
    haloMat.uniforms.uTime.value = nowPerf;
    haloMat.uniforms.uZoom.value = camera.zoom;
  }
  driftAmbient();
  tickBlooms(nowPerf);
  rebuildArcLayer();
  tickChatAnims(nowPerf);
  tickCameraTween(nowPerf);
  updateAzimuth(nowPerf);
  maybeLayoutClusterLabels();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}

// Data loading + realtime ----------------------------------------------

async function loadConstellation() {
  const res = await fetch("/api/memory/constellation");
  if (!res.ok) return;
  const data = await res.json();
  // Guard the crash path: a missing `points` used to throw in rebuildLayers.
  state.records = Array.isArray(data.points) ? data.points : [];
  document.getElementById("kpi-records").textContent =
    data.count ?? state.records.length;
  state.hoveredIdx = -1;
  state.selectedIdx = -1;
  rebuildLayers();
  await loadEdges();
  rebuildEdgeLayer();
  recomputeClusters();
  updateMood();
}

let reloadTimer = null;
function scheduleReload(recordId) {
  if (reloadTimer) return;
  reloadTimer = setTimeout(async () => {
    reloadTimer = null;
    const prev = new Set(state.records.map(r => r.id));
    await loadConstellation();
    for (let i = 0; i < state.records.length; i++) {
      const r = state.records[i];
      if (!prev.has(r.id)) {
        state.highlights.push({ recordIdx: i, start: performance.now() });
        // pulse arcs from top-3 nearest neighbors
        spawnPulseArcs(i);
      }
    }
    if (recordId) {
      const idx = state.records.findIndex(r => r.id === recordId);
      if (idx >= 0) state.highlights.push({ recordIdx: idx, start: performance.now() });
    }
    applyFilterMask();
  }, 400);
}

function connectWs() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/api/events`);
  ws.addEventListener("close", () => setTimeout(connectWs, 2000));
  ws.addEventListener("message", (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (_) { return; }
    if (ev.kind === "memory.written") {
      state.newThisSession += 1;
      document.getElementById("kpi-new").textContent = state.newThisSession;
      const rid = (ev.payload && (ev.payload.record_id || ev.payload.id)) || null;
      scheduleReload(rid);
      updateMood();
    } else if (ev.kind === "memory_get" || ev.kind === "memory.read") {
      const rid = (ev.payload && (ev.payload.record_id || ev.payload.id)) || null;
      if (rid) bumpAccess(rid);
    } else if (ev.kind === "memory_search" || ev.kind === "memory.searched") {
      const ids = (ev.payload && ev.payload.result_ids) || [];
      for (const rid of ids) bumpAccess(rid);
    } else if (ev.kind === "memory.chat") {
      // Update access counts for retrieved/cited so size/activity reflect use.
      const p = ev.payload || {};
      const retrieved = Array.isArray(p.retrieved_record_ids) ? p.retrieved_record_ids : [];
      for (const rid of retrieved) bumpAccess(rid);
      // Live-thinking animation track (capped concurrency).
      spawnChatTrack(p);
      updateMood();
    }
  });
}

// Boot ------------------------------------------------------------------

function focusFromHash() {
  const h = location.hash || "";
  const m = h.match(/^#focus=([^&]+)/);
  if (!m) return;
  const rid = decodeURIComponent(m[1]);
  if (!rid) return;
  // Defer one tick so the records array is fully laid out.
  setTimeout(() => focusOnRecord(rid), 50);
}

window.addEventListener("hashchange", focusFromHash);

rebuildFiltersFromUi();
resize();
loadConstellation().then(() => {
  connectWs();
  requestAnimationFrame(animate);
  focusFromHash();
});
