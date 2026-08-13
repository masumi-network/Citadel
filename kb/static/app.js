// 2D Logseq-style force-directed graph via the vendored force-graph library
// (window.ForceGraph, loaded by a plain <script> in index.html). The legacy
// Three.js 3D scene was removed in favour of a flat, readable layout with the
// shared Central dataset pinned at the centre as the largest hub.

// ---- theme, shared with the public pages ----
// The same storage key and the same rule as info.js: light is the default and
// prefers-color-scheme is never consulted, so a theme chosen on the landing
// page carries into the dashboard and back out again. Applied at the top of the
// file so the attribute lands before anything renders.
const THEME_KEY = "citadel-info-theme";
const themeRoot = document.documentElement;
try {
  const savedTheme = localStorage.getItem(THEME_KEY);
  if (savedTheme === "light" || savedTheme === "dark") {
    themeRoot.setAttribute("data-theme", savedTheme);
  }
} catch {
  // storage blocked — stay on the light default
}

function currentTheme() {
  return themeRoot.getAttribute("data-theme") || "light";
}

// Every .themebtn on the page, so the sidebar toggle and the one in Home's
// title row stay in step instead of disagreeing about the current theme.
function initializeThemeToggle() {
  const buttons = Array.from(document.querySelectorAll(".themebtn"));
  if (!buttons.length) return;

  function paint() {
    const dark = currentTheme() === "dark";
    buttons.forEach((button) => {
      const icon = button.querySelector(".themebtn-icon");
      const label = button.querySelector(".themebtn-label");
      if (icon) icon.textContent = dark ? "☾" : "☀";
      if (label) label.textContent = dark ? "Dark" : "Light";
      button.setAttribute("aria-pressed", dark ? "true" : "false");
      button.title = dark ? "Switch to the light theme" : "Switch to the dark theme";
    });
  }

  paint();
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      themeRoot.setAttribute("data-theme", next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        // storage blocked — the choice holds for this page only
      }
      paint();
      // Node colours are resolved from the CSS tokens once and memoised; drop
      // the cache or the canvas keeps painting the theme it booted in.
      colorCache = null;
    });
  });
}

const state = {
  snapshot: null,
  nodes: new Map(),
  edges: [],
  selectedId: null,
  selectedEventId: null,
  paused: false,
  eventSource: null,
  role: null,
  seatSlug: null,
  nodeLabel: null,
  defaultDataset: null,
  searchDatasets: null,
  capabilities: null,
  githubSync: null,
  obsidianSources: null,
  accessSnapshot: null,
  settingsSnapshot: null,
  // Home and Review read these; whichever fetch lands first paints, the rest
  // fill in. Neither view blocks on the slowest request.
  meSummary: null,
  // Distinguishes "the fetch failed" from "not fetched yet" / "field absent".
  // Home renders a different note for each; a bare dash for all three would
  // hide a broken endpoint.
  meSummaryError: false,
  promotions: [],
  sources: [],
  auditFilter: "all",
  // Knowledge Mesh is the durable view; Vault Activity is restart-transient and
  // therefore empty on every fresh boot/redeploy — a bad first impression.
  graphMode: "knowledge",
  graphSpokes: true,
  // Knowledge-mode legend filter: node kinds hidden from the canvas. Chunks
  // start hidden — they dominate the node count and bury the entities.
  graphHiddenKinds: new Set(["chunk"]),
  // Knowledge Mesh overview folds documents/chunks into a concept map by
  // default. Off shows the raw graph so the legend chips can actually restore
  // Documents/Chunks — before this was wired, aggregation was permanently on
  // (nothing ever wrote the flag) and those chips were dead controls.
  graphAggregate: true,
  // Dataset filter for /api/mesh/graph ("" = org-wide view). Session-scoped
  // like the other graph toggles; nothing persists across reloads.
  graphDataset: "",
  realGraph: null,
  realGraphLoading: false,
  conflicts: [],
  conflictFilter: "open",
};

const canvas = document.getElementById("graphCanvas");
const systemStatus = document.querySelector(".system-status");
const connectionLabel = document.getElementById("connectionLabel");
const runtimeStatus = document.getElementById("runtimeStatus");
const sessionRole = document.getElementById("sessionRole");
const sessionSeat = document.getElementById("sessionSeat");
const roleSummary = document.getElementById("roleSummary");
const accessMode = document.getElementById("accessMode");
const graphMeta = document.getElementById("graphMeta");
const selectedNode = document.getElementById("selectedNode");
const indexList = document.getElementById("indexList");
const eventList = document.getElementById("eventList");
const eventCount = document.getElementById("eventCount");
const eventInspector = document.getElementById("eventInspector");
const timelineFreshness = document.getElementById("timelineFreshness");
const timelineStatValues = {
  indexed: document.querySelector('[data-timeline-stat="indexed"]'),
  pending: document.querySelector('[data-timeline-stat="pending"]'),
  failed: document.querySelector('[data-timeline-stat="failed"]'),
  lastIndexed: document.querySelector('[data-timeline-stat="lastIndexed"]'),
};
const upgradeStatus = document.getElementById("upgradeStatus");
const meshAlert = document.getElementById("meshAlert");
const meshAlertText = document.getElementById("meshAlertText");
const canvasEmpty = document.getElementById("canvasEmpty");
const githubSyncStatus = document.getElementById("githubSyncStatus");
const syncLastChecked = document.getElementById("syncLastChecked");
const syncTrackedRepos = document.getElementById("syncTrackedRepos");
const githubSourceLink = document.getElementById("githubSourceLink");
const syncRunSummary = document.getElementById("syncRunSummary");
const syncResult = document.getElementById("syncResult");
const syncPostToChat = document.getElementById("syncPostToChat");
const googleChatTestButton = document.getElementById("googleChatTestButton");
const obsidianVaultCount = document.getElementById("obsidianVaultCount");
const obsidianDocumentCount = document.getElementById("obsidianDocumentCount");
const obsidianSourceStatus = document.getElementById("obsidianSourceStatus");
const obsidianSourceList = document.getElementById("obsidianSourceList");
const feedbackStatus = document.getElementById("feedbackStatus");
const feedbackResult = document.getElementById("feedbackResult");
const accessTokenStatus = document.getElementById("accessTokenStatus");
const accessPrincipalList = document.getElementById("accessPrincipalList");
const accessTokenList = document.getElementById("accessTokenList");
const accessSeatsList = document.getElementById("accessSeatsList");
const accessSeatsStatus = document.getElementById("accessSeatsStatus");
const accessAuditList = document.getElementById("accessAuditList");
const newAccessToken = document.getElementById("newAccessToken");
const dashboardCronStatus = document.getElementById("dashboardCronStatus");
const dashboardCronMeta = document.getElementById("dashboardCronMeta");
const dashboardIngestStatus = document.getElementById("dashboardIngestStatus");
const dashboardIngestionList = document.getElementById("dashboardIngestionList");
const dashboardPromotionStatus = document.getElementById("dashboardPromotionStatus");
const dashboardPromotionList = document.getElementById("dashboardPromotionList");
const dashboardMcpClients = document.getElementById("dashboardMcpClients");
const dashboardMcpMeta = document.getElementById("dashboardMcpMeta");
const dashboardMcpStatus = document.getElementById("dashboardMcpStatus");
const dashboardMcpList = document.getElementById("dashboardMcpList");
const dashboardRecentLearning = document.getElementById("dashboardRecentLearning");
const dashboardIndexSummary = document.getElementById("dashboardIndexSummary");
const dashboardOpenIssue = document.getElementById("dashboardOpenIssue");
const knowledgeStatus = document.getElementById("knowledgeStatus");
const knowledgeSourceCount = document.getElementById("knowledgeSourceCount");
const knowledgeSnapshotCount = document.getElementById("knowledgeSnapshotCount");
const knowledgeConflictCount = document.getElementById("knowledgeConflictCount");
const knowledgeRecordCount = document.getElementById("knowledgeRecordCount");
const knowledgeDigestStatus = document.getElementById("knowledgeDigestStatus");
const knowledgeDailyUpdate = document.getElementById("knowledgeDailyUpdate");
const knowledgeSourceList = document.getElementById("knowledgeSourceList");
const knowledgeIndexList = document.getElementById("knowledgeIndexList");
const knowledgeRecentList = document.getElementById("knowledgeRecentList");
const agentsStatus = document.getElementById("agentsStatus");
const agentsTokenList = document.getElementById("agentsTokenList");
const auditStatus = document.getElementById("auditStatus");
const auditAccessList = document.getElementById("auditAccessList");
const auditRuntimeList = document.getElementById("auditRuntimeList");
const auditFilterButtons = Array.from(document.querySelectorAll("[data-audit-filter]"));
const auditMcpSummary = document.getElementById("auditMcpSummary");
const auditAccessTitle = document.getElementById("auditAccessTitle");
const auditAccessSubtitle = document.getElementById("auditAccessSubtitle");
const settingsStatus = document.getElementById("settingsStatus");
const settingsHealthGrid = document.getElementById("settingsHealthGrid");
const settingsPolicyList = document.getElementById("settingsPolicyList");
const settingsMirrorList = document.getElementById("settingsMirrorList");
const conflictsStatus = document.getElementById("conflictsStatus");
const conflictsList = document.getElementById("conflictsList");
const conflictNavBadge = document.getElementById("conflictNavBadge");
const conflictFilterButtons = Array.from(document.querySelectorAll("[data-conflict-filter]"));
const graphModeButtons = Array.from(document.querySelectorAll("[data-graph-mode]"));
const graphAggregateButton = document.getElementById("graphAggregateButton");
const graphDatasetFilter = document.getElementById("graphDatasetFilter");
const graphNodeSearch = document.getElementById("graphNodeSearch");
const graphNodeSearchInput = document.getElementById("graphNodeSearchInput");
const graphNodeSearchResults = document.getElementById("graphNodeSearchResults");
const graphViewMenu = document.getElementById("graphViewMenu");
const graphViewLabel = document.getElementById("graphViewLabel");
const realGraphEmpty = document.getElementById("realGraphEmpty");
const graphLegend = document.getElementById("graphLegend");
const toastStack = document.getElementById("toastStack");
const searchResultStatus = document.getElementById("searchResultStatus");
const pageButtons = Array.from(document.querySelectorAll("[data-page-target]"));
const pages = Array.from(document.querySelectorAll("[data-page]"));
const roleOrder = { reader: 1, writer: 2, admin: 3 };

// Grouped navigation: the sidebar shows 4 top-level items (Home, Search,
// Review, Admin); merged groups expose their sub-pages as a content sub-tab bar
// that drives the same setPage() switching. Each group's `nav` is the sidebar
// button (data-page-target) that represents it, so that button stays lit on any
// of the group's sub-pages. The knowledge, events and ingest groups no longer
// have a sidebar button — they are reached by route or by an in-page button, and
// their sub-tabs still work; nothing lights up in the sidebar, which is correct.
const NAV_GROUPS = [
  { nav: "knowledge", tabs: [
    { page: "knowledge", label: "Graph" },
    { page: "sources", label: "Sources" },
  ] },
  { nav: "events", tabs: [
    { page: "events", label: "Timeline" },
    { page: "conflicts", label: "Conflicts" },
  ] },
  { nav: "ingest", tabs: [
    { page: "ingest", label: "New note" },
    { page: "feedback", label: "Feedback" },
  ] },
  { nav: "agents", tabs: [
    { page: "agents", label: "Agents" },
    { page: "access", label: "Access" },
    { page: "audit", label: "Audit" },
    { page: "settings", label: "Settings" },
  ] },
];
const pageToGroup = {};
NAV_GROUPS.forEach((group) => {
  group.tabs.forEach((tab) => {
    pageToGroup[tab.page] = group;
  });
});
const subtabBar = document.getElementById("subtabBar");

function renderSubtabs(activePage) {
  if (!subtabBar) return;
  const group = pageToGroup[activePage];
  subtabBar.innerHTML = "";
  if (!group) {
    subtabBar.hidden = true;
    return;
  }
  group.tabs.forEach((tab) => {
    const targetPage = pages.find((page) => page.dataset.page === tab.page);
    const minRole = (targetPage && targetPage.dataset.minRole) || "reader";
    if (!canUse(minRole)) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `subtab${tab.page === activePage ? " active" : ""}`;
    button.textContent = tab.label;
    button.addEventListener("click", () => setPage(tab.page));
    subtabBar.append(button);
  });
  subtabBar.hidden = subtabBar.children.length < 2;
}
const sensitiveDetailPattern = /(token|secret|password|authorization|body|content|text|query)$/i;

// force-graph instance + render state. Selection/highlight live here so the
// node/link accessors can dim non-neighbours cheaply during hover.
const graph = {
  instance: null,
  width: 1,
  height: 1,
  viewInitialized: false,
  centralId: null,
  highlightNodes: new Set(),
  highlightLinks: new Set(),
  hoverId: null,
};
let graphNodeMatches = [];
let graphNodeMatchIndex = -1;

// Shared Central dataset (config.github_sync_dataset / access.CENTRAL_DATASET).
const CENTRAL_DATASET = "masumi-network";
const SEAT_DATASET_PREFIX = "seat:";

// Resolve a token from a CSS custom property so node colours stay in sync with
// the brand palette (--primary, --info, etc.) instead of hardcoded hex.
function cssToken(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

// Per-type node colours, mirroring the legacy palette but sourced from tokens.
function brandColors() {
  return {
    dataset: cssToken("--primary", "#a882ff"),
    document: cssToken("--text", "#dadada"),
    tag: cssToken("--quiet", "#8f8f8f"),
    index: cssToken("--info", "#7dd4c0"),
    query: cssToken("--warning", "#d6b86d"),
    feedback: cssToken("--danger", "#e8615f"),
    upgrade: cssToken("--success", "#6ecb8d"),
    source: cssToken("--info", "#7dd4c0"),
    repository: cssToken("--primary-strong", "#c4b1ff"),
    seat: cssToken("--primary-strong", "#c4b1ff"),
    // Knowledge-mode kinds (nodeKind); "dataset"/"document"/"seat" above are
    // shared. Legend swatches read the same map so chips always match nodes.
    chunk: cssToken("--quiet", "#8f8f8f"),
    entity: cssToken("--info", "#7dd4c0"),
    other: cssToken("--muted", "#b3b3b3"),
  };
}

let colorCache = null;
function typeColor(type) {
  if (!colorCache) colorCache = brandColors();
  return colorCache[String(type || "other")] || colorCache.other;
}

// The Central dataset node id. In live mode it is the dataset node whose label
// equals CENTRAL_DATASET; otherwise the highest-degree node — but never a
// seat hub, so one user's private seat is never promoted to org Central.
function resolveCentralId(nodes) {
  for (const node of nodes) {
    if (
      node.type === "dataset" &&
      (node.metadata?.dataset === CENTRAL_DATASET || node.label === CENTRAL_DATASET)
    ) {
      return node.id;
    }
  }
  let best = null;
  let bestDegree = -1;
  for (const node of nodes) {
    // Seat hubs collect one belongs_to edge per doc+chunk, so without this
    // guard the biggest seat's private hub would win the degree fallback and
    // get pinned at the origin as the org Central whenever no node matches
    // CENTRAL_DATASET (e.g. a renamed github_sync_dataset).
    if (isSeatNode(node)) continue;
    const degree = Array.isArray(node.neighbors) ? node.neighbors.length : 0;
    if (degree > bestDegree) {
      bestDegree = degree;
      best = node.id;
    }
  }
  return best;
}

function isSeatNode(node) {
  const dataset = node.metadata?.dataset || node.label || "";
  return node.type === "dataset" && String(dataset).startsWith(SEAT_DATASET_PREFIX);
}

// Coarse node kinds for the knowledge-mode legend, filter, and colors.
const GRAPH_KIND_ORDER = ["seat", "dataset", "document", "chunk", "entity"];
const GRAPH_KIND_LABELS = {
  seat: "Seats",
  dataset: "Datasets",
  document: "Documents",
  chunk: "Chunks",
  entity: "Entities",
};

// Map a node to its legend kind. Cognee document nodes may surface either the
// class name ("TextDocument") or a short type ("text"/"pdf"). Chunk is checked
// before document because "DocumentChunk" contains both words.
function nodeKind(node) {
  if (isSeatNode(node)) return "seat";
  const type = String(node.type || "").toLowerCase();
  if (type === "dataset") return "dataset";
  if (type.includes("chunk")) return "chunk";
  if (type.includes("document") || ["text", "pdf", "audio", "image"].includes(type)) {
    return "document";
  }
  return "entity";
}

// nodeVal tiers (area-proportional): Central is the biggest fixed hub, seat:
// datasets are the second tier, everything else scales by degree (link count).
function nodeValue(node) {
  if (node.id === graph.centralId) return 26;
  if (isSeatNode(node)) return 12;
  const degree = Array.isArray(node.neighbors) ? node.neighbors.length : 0;
  return clamp(2 + degree * 1.4, 2, 9);
}

function applyHubSpokes(nodes, links, byId) {
  // Spokes are decoration for the live dashboard projection only. Knowledge
  // mode renders real graph data (dataset attribution arrives as belongs_to
  // edges), so synthesizing Central→seat "hub" links there would draw edges
  // that do not exist in the data.
  if (state.graphMode === "knowledge") return { nodes, links };
  if (!state.graphSpokes || !graph.centralId || !byId.has(graph.centralId)) {
    return { nodes, links };
  }
  const nextLinks = [...links];
  const existing = new Set(nextLinks.map((link) => `${link.source}|${link.target}`));
  for (const node of nodes) {
    if (!isSeatNode(node) || node.id === graph.centralId) continue;
    const key = `${graph.centralId}|${node.id}`;
    if (existing.has(key)) continue;
    existing.add(key);
    nextLinks.push({ source: graph.centralId, target: node.id, label: "hub", synthetic: true });
  }
  return { nodes, links: nextLinks };
}

function rebuildNeighborLinks(nodes, links) {
  for (const node of nodes) {
    node.neighbors = [];
    node.links = [];
  }
  const byId = new Map(nodes.map((node) => [node.id, node]));
  for (const edge of links) {
    const sourceNode = byId.get(edge.source);
    const targetNode = byId.get(edge.target);
    if (!sourceNode || !targetNode) continue;
    sourceNode.neighbors.push(targetNode);
    targetNode.neighbors.push(sourceNode);
    sourceNode.links.push(edge);
    targetNode.links.push(edge);
  }
  return byId;
}

// Overview default for Knowledge Mesh: fold the raw document/chunk cloud into
// per-hub counts so the graph reads as a concept map — dataset/seat hubs, the
// entity TYPES (EntityType/NodeSet), and the well-connected entities that tie
// them together — instead of a 200-node hairball. Documents attributed to a
// dataset become a "· N docs" count on that hub; sparsely-linked entities and
// the document/chunk nodes themselves drop out. Degree is measured on the full
// link set before anything is removed.
const AGG_MIN_ENTITY_DEGREE = 6;

function aggregateKnowledgeMesh(nodes, links) {
  const degree = new Map();
  for (const link of links) {
    degree.set(link.source, (degree.get(link.source) || 0) + 1);
    degree.set(link.target, (degree.get(link.target) || 0) + 1);
  }
  const rawType = (node) => String(node.type || "").toLowerCase();
  const isConcept = (node) =>
    rawType(node).includes("entitytype") || rawType(node).includes("nodeset");
  const isDocLike = (node) => {
    const kind = nodeKind(node);
    return kind === "document" || kind === "chunk";
  };
  // Cognee TextSummary nodes are per-chunk summaries ("This chunk is about …"),
  // not concepts — drop them from the concept map so labels aren't all boilerplate.
  const isSummary = (node) => rawType(node).includes("summary");
  const keep = new Set();
  for (const node of nodes) {
    if (node.type === "dataset" || isConcept(node)) {
      keep.add(node.id);
    } else if (
      !isDocLike(node) &&
      !isSummary(node) &&
      (degree.get(node.id) || 0) >= AGG_MIN_ENTITY_DEGREE
    ) {
      keep.add(node.id);
    }
  }
  // Fold each dropped, document-like node into its dataset hub's count.
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const folded = new Map();
  for (const link of links) {
    if ((link.relationship || link.label) !== "belongs_to") continue;
    const a = byId.get(link.source);
    const b = byId.get(link.target);
    const hub = a && a.type === "dataset" ? a : b && b.type === "dataset" ? b : null;
    const other = hub === a ? b : a;
    if (hub && other && !keep.has(other.id) && isDocLike(other)) {
      folded.set(hub.id, (folded.get(hub.id) || 0) + 1);
    }
  }
  const keptNodes = nodes
    .filter((node) => keep.has(node.id))
    .map((node) => {
      const count = folded.get(node.id);
      if (!count) return node;
      return { ...node, docCount: count, label: `${node.label} · ${count} docs` };
    });
  const keptLinks = links.filter(
    (link) => keep.has(link.source) && keep.has(link.target),
  );
  return { nodes: keptNodes, links: keptLinks };
}

// Map the active {nodes,edges} into force-graph graphData and precompute
// node.neighbors + node.links once for fast hover highlighting.
function buildForceGraphData() {
  const source = activeGraphData();
  let nodes = Array.from(source.nodes.values()).map((node) => ({
    id: node.id,
    label: node.label || node.id,
    type: node.type || "other",
    status: node.status,
    size: node.size,
    metadata: node.metadata || {},
    neighbors: [],
    links: [],
  }));
  // Knowledge mode only: drop legend-hidden kinds and any edge touching them.
  // The projection (live) data and state.realGraph itself stay unfiltered.
  if (state.graphMode === "knowledge" && state.graphHiddenKinds.size) {
    nodes = nodes.filter((node) => !state.graphHiddenKinds.has(nodeKind(node)));
  }
  const nodeIds = new Set(nodes.map((node) => node.id));
  let links = [];
  for (const edge of source.edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) continue;
    // Projection edges carry "label", knowledge edges "relationship" —
    // normalize so linkLabel tooltips work in both modes.
    links.push({
      source: edge.source,
      target: edge.target,
      label: edge.label || edge.relationship,
      relationship: edge.relationship || edge.label,
    });
  }
  // Knowledge Mesh overview aggregates to a readable concept map by default;
  // the raw graph (state.realGraph) stays untouched for inspection/search.
  if (state.graphMode === "knowledge" && state.graphAggregate !== false) {
    ({ nodes, links } = aggregateKnowledgeMesh(nodes, links));
  }
  // resolveCentralId's highest-degree fallback reads node.neighbors, which is
  // only populated by rebuildNeighborLinks — so resolve AFTER the first pass,
  // else the fallback is dead and an arbitrary first node gets pinned as the
  // org Central hub (e.g. when github_sync_dataset is renamed).
  let byId = rebuildNeighborLinks(nodes, links);
  graph.centralId = resolveCentralId(nodes);
  ({ nodes, links } = applyHubSpokes(nodes, links, byId));
  byId = rebuildNeighborLinks(nodes, links);
  return { nodes, links, byId };
}

function activeGraphData() {
  if (state.graphMode === "knowledge") {
    return state.realGraph || { nodes: new Map(), edges: [] };
  }
  return { nodes: state.nodes, edges: state.edges };
}

function formatApiError(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  return null;
}

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  }).then(async (response) => {
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const message = formatApiError(data?.detail) || data?.message || "Request failed";
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  });
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  button.setAttribute("aria-busy", busy ? "true" : "false");
  if (label) {
    button.textContent = busy ? label.loading : label.idle;
  }
}

function setConnectionState(stateName, label) {
  connectionLabel.textContent = label;
  runtimeStatus.textContent = label;
  runtimeStatus.className = `status-chip status-${stateName}`;
  systemStatus.dataset.state = stateName;
}

function canUse(requiredRole = "reader") {
  if (!state.role) return false;
  return roleOrder[state.role] >= roleOrder[requiredRole];
}

function roleLabel(role) {
  if (role === "admin") return "Admin";
  if (role === "writer") return "Read write";
  return "Read only";
}

function datasetSourceBadge(dataset) {
  const name = String(dataset || "").trim();
  if (!name) return { label: "Unknown source", kind: "unknown" };
  if (name === CENTRAL_DATASET || name === "masumi-network") {
    return { label: "Central", kind: "central" };
  }
  if (name.startsWith(SEAT_DATASET_PREFIX)) {
    const slug = name.slice(SEAT_DATASET_PREFIX.length);
    if (state.seatSlug && slug === state.seatSlug) {
      return { label: "My Node", kind: "mine" };
    }
    return { label: `Seat ${slug}`, kind: "seat" };
  }
  if (name === "session-traces") {
    return { label: "Shared traces", kind: "traces" };
  }
  return { label: name, kind: "other" };
}

function applyAccessControls() {
  const label = state.role ? roleLabel(state.role) : "Locked";
  sessionRole.textContent = label;
  sessionRole.className = `status-chip ${state.role ? "status-enabled" : "status-error"}`;
  if (sessionSeat) {
    if (state.seatSlug) {
      sessionSeat.hidden = false;
      sessionSeat.textContent = `seat:${state.seatSlug}`;
      sessionSeat.title = state.nodeLabel || `seat:${state.seatSlug}`;
      sessionSeat.className = "seat-slug";
      const avatar = document.getElementById("seatAvatar");
      if (avatar) {
        avatar.textContent = state.seatSlug.slice(0, 1).toUpperCase();
      }
    } else {
      sessionSeat.hidden = true;
      sessionSeat.textContent = "—";
      const avatar = document.getElementById("seatAvatar");
      if (avatar) {
        avatar.textContent = state.role ? roleLabel(state.role).slice(0, 1) : "·";
      }
    }
  }
  roleSummary.textContent = state.role
    ? state.seatSlug
      ? `${label} · seat ${state.seatSlug}`
      : `${label} vault access`
    : "No vault session";
  accessMode.textContent = label;
  accessMode.className = sessionRole.className;

  document.querySelectorAll("[data-min-role]").forEach((element) => {
    const allowed = canUse(element.dataset.minRole);
    if (element.classList.contains("nav-link")) {
      element.disabled = !allowed;
      return;
    }
    if (element.matches("form, fieldset")) {
      element.querySelectorAll("button, input, textarea, select").forEach((control) => {
        control.disabled = !allowed;
      });
      return;
    }
    if (element.matches("button, input, textarea, select")) {
      element.disabled = !allowed;
      return;
    }
    if (!element.matches("[data-page]")) {
      element.hidden = !allowed;
    }
  });

  renderDashboardMcpSession();
  renderConflicts();
}

function renderDashboardMcpSession() {
  if (!dashboardMcpStatus || !dashboardMcpList) return;
  const label = state.role ? roleLabel(state.role) : "Locked";
  dashboardMcpStatus.textContent = state.role ? "Authorized" : "Locked";
  dashboardMcpStatus.className = `status-chip ${state.role ? "status-enabled" : "status-error"}`;
  if (dashboardMcpClients && !canUse("admin")) {
    dashboardMcpClients.textContent = state.role ? label : "Locked";
    dashboardMcpMeta.textContent = state.role ? "current session" : "login required";
  }
  const seatLine = state.seatSlug
    ? ` · seat ${escapeHtml(state.seatSlug)} (${escapeHtml(state.nodeLabel || `seat:${state.seatSlug}`)})`
    : "";
  dashboardMcpList.innerHTML = `
    <div class="entity-item">
      <div>
        <strong>Current session</strong>
        <p>${escapeHtml(label)} vault access${seatLine}${state.role ? " through cookie or bearer token" : ""}</p>
      </div>
      <span class="status-chip ${state.role ? "status-enabled" : "status-error"}">${escapeHtml(state.role || "none")}</span>
    </div>
  `;
}

async function loadSession() {
  try {
    const session = await api("/api/session");
    state.role = session.role;
    state.seatSlug = session.seat_slug || null;
    state.nodeLabel = session.node_label || null;
    state.defaultDataset = session.default_dataset || null;
    state.searchDatasets = session.search_datasets || null;
    state.capabilities = session.capabilities || null;
    applyAccessControls();
    // The dataset filter's options come from the session scope.
    updateGraphDatasetFilter();
  } catch {
    window.location.assign("/login");
    throw new Error("Session required");
  }
}

function initialPage() {
  const hash = window.location.hash.replace("#", "");
  if (pages.some((page) => page.dataset.page === hash)) return hash;
  // Home for everyone, including admins and seat-less tokens: Overview is no
  // longer a nav entry and is no longer routed to. Home already has a seat-less
  // state, so nobody lands on a page they cannot use.
  return "home";
}

function setPage(name) {
  const targetPage = pages.find((page) => page.dataset.page === name);
  const requiredRole = targetPage?.dataset.minRole || "reader";
  const allowed = targetPage && canUse(requiredRole);
  const resolvedName = allowed ? name : "locked";

  pages.forEach((page) => {
    page.hidden = page.dataset.page !== resolvedName;
    page.classList.toggle("page-active", page.dataset.page === resolvedName);
  });
  // Sidebar buttons reflect the active *group* (so e.g. the Admin button stays
  // lit on the Audit sub-page); in-content [data-page-target] buttons keep
  // exact-match highlighting.
  const activeGroup = pageToGroup[resolvedName];
  const activeNavTarget = activeGroup ? activeGroup.nav : name;
  pageButtons.forEach((button) => {
    const isNavLink = button.classList.contains("nav-link");
    const match = isNavLink
      ? button.dataset.pageTarget === activeNavTarget
      : button.dataset.pageTarget === name;
    button.classList.toggle("active", match && allowed);
  });
  renderSubtabs(resolvedName);
  if (allowed && window.location.hash !== `#${name}`) {
    window.history.replaceState(null, "", `#${name}`);
  }
  // force-graph keeps its own layout across page switches; just re-fit the
  // canvas to the (possibly newly-visible) container without re-seeding.
  resizeCanvas();
  if (resolvedName === "access") {
    loadAccess();
  }
  if (resolvedName === "home") {
    loadSeatHome();
  }
  if (resolvedName === "review") {
    loadReview();
  }
  if (resolvedName === "agents" || resolvedName === "audit") {
    loadAccess();
  }
  if (resolvedName === "settings") {
    loadSettings();
  }
  if (resolvedName === "conflicts") {
    loadConflicts();
  }
}

function initializeNavigation() {
  pageButtons.forEach((button) => {
    button.addEventListener("click", () => setPage(button.dataset.pageTarget));
  });
  window.addEventListener("hashchange", () => setPage(initialPage()));
}

function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  graph.width = Math.max(1, rect.width);
  graph.height = Math.max(1, rect.height);
  if (!graph.instance) return;
  graph.instance.width(graph.width).height(graph.height);
}

function mergeGraph(snapshot) {
  state.snapshot = snapshot;
  const nextIds = new Set();

  snapshot.nodes.forEach((node) => {
    nextIds.add(node.id);
    const existing = state.nodes.get(node.id);
    if (existing) {
      Object.assign(existing, node);
      return;
    }
    state.nodes.set(node.id, { ...node });
  });

  for (const id of state.nodes.keys()) {
    if (!nextIds.has(id)) {
      state.nodes.delete(id);
    }
  }

  state.edges = snapshot.edges;
  if (state.graphMode === "live") {
    renderGraph();
  }
  renderSnapshot(snapshot);
  if (state.graphMode === "live") {
    selectNode(state.nodes.get(state.selectedId) || null);
  }
}

function renderSnapshot(snapshot) {
  meshAlert.hidden = true;
  canvasEmpty.hidden = state.graphMode !== "live" || snapshot.nodes.length > 4;
  updateGraphMeta();
  // Activity counters reset with the process and are published only under
  // stats.since_restart (#196) — corpus totals stay top-level.
  const sinceRestart = snapshot.stats.since_restart || {};
  document.getElementById("statNodes").textContent = snapshot.stats.nodes;
  document.getElementById("statEdges").textContent = snapshot.stats.edges;
  document.getElementById("statDocuments").textContent = snapshot.stats.tracked_sources;
  document.getElementById("statSearches").textContent = sinceRestart.searches || 0;
  document.getElementById("statFeedback").textContent = sinceRestart.feedback || 0;
  document.getElementById("statUpgrades").textContent = sinceRestart.upgrades || 0;
  document.getElementById("statErrors").textContent = sinceRestart.errors || 0;
  if (knowledgeStatus) {
    const errorCount = Number(sinceRestart.errors || 0);
    knowledgeStatus.textContent = errorCount ? "Review" : "Current";
    knowledgeStatus.className = `status-chip ${errorCount ? "status-error" : "status-enabled"}`;
  }
  if (knowledgeSnapshotCount) {
    knowledgeSnapshotCount.textContent = String(snapshot.stats.tracked_sources || 0);
  }
  eventCount.textContent = String(snapshot.events.length);
  renderDashboardIndexes(snapshot.indexes);
  renderDashboardRecentEvent(snapshot.events);
  renderDashboardOpenIssue(snapshot);
  renderTimelineStats(snapshot);
  renderTimeline(snapshot);
  renderOverviewAnalytics(snapshot);
  renderActivityAnalytics(snapshot);
  renderHome();

  indexList.innerHTML = "";
  if (!snapshot.indexes.length) {
    indexList.append(emptyState("No indexes", "The runtime has not reported index status yet."));
  } else {
    snapshot.indexes.forEach((index) => {
      const item = document.createElement("div");
      item.className = "index-item";
      item.innerHTML = `
        <div>
          <div class="index-name">${escapeHtml(index.name)}</div>
          <div class="index-meta">${escapeHtml(index.records)} records</div>
        </div>
        <span class="status-chip status-${escapeHtml(index.status)}">${escapeHtml(index.status)}</span>
      `;
      indexList.append(item);
    });
  }

}

function renderTimelineStats(snapshot) {
  const stats = snapshot.stats || {};
  const since = stats.since_restart || {};
  const events = snapshot.events || [];
  if (eventCount) eventCount.textContent = String(events.length);
  if (timelineStatValues.indexed) {
    // indexed_chunks is no longer published at the top level (it only ever
    // duplicated `nodes`, by construction); the honest restart-scoped
    // accumulator lives under since_restart, same as pending/failed below.
    timelineStatValues.indexed.textContent = String(since.indexed_chunks || 0);
  }
  if (timelineStatValues.pending) {
    timelineStatValues.pending.textContent = String(since.pending_chunks || 0);
  }
  if (timelineStatValues.failed) {
    // failed_chunks was the errors counter surfaced twice (#197); the single
    // failure figure is the failed-operation count since restart.
    timelineStatValues.failed.textContent = String(since.errors || 0);
  }
  if (timelineStatValues.lastIndexed) {
    timelineStatValues.lastIndexed.textContent = stats.last_indexed_at
      ? formatDate(stats.last_indexed_at)
      : "waiting";
  }
  if (timelineFreshness) {
    const failed = Number(since.errors || 0);
    timelineFreshness.textContent = failed ? "Review" : events.length ? "Live" : "Waiting";
    timelineFreshness.className = `status-chip ${failed ? "status-error" : events.length ? "status-enabled" : "status-standby"}`;
  }
}

function renderTimeline(snapshot) {
  if (!eventList) return;
  const events = snapshot.events || [];
  const selectedExists = events.some((event) => event.id === state.selectedEventId);
  if (!selectedExists) {
    state.selectedEventId = events[0]?.id || null;
  }

  eventList.innerHTML = "";
  if (!events.length) {
    eventList.append(emptyListItem("No events yet", "Run a sync or save a vault note."));
    renderEventInspector(null);
    return;
  }

  events.slice(0, 40).forEach((event) => {
    eventList.append(timelineEventItem(event));
  });
  renderEventInspector(events.find((event) => event.id === state.selectedEventId) || events[0]);
}

function timelineEventItem(event) {
  const item = document.createElement("li");
  item.className = "timeline-event";
  const timeline = timelineEnvelope(event);
  const selected = event.id === state.selectedEventId;
  const statusClass = timelineStatusClass(event, timeline);
  const metrics = formatMetrics(timeline.metrics);
  const meta = [timeline.dataset, timeline.source, metrics].filter(Boolean).join(" - ");
  // Surface the rejection/error reason that the server already records on the
  // event payload (mesh.record_ingest -> details.reason) but the card never showed.
  // Skip "accepted" so normal indexed events stay clean.
  const rawReason = event.details && event.details.reason;
  const reason = rawReason && rawReason !== "accepted" ? String(rawReason) : null;
  const button = document.createElement("button");
  button.type = "button";
  button.className = `event-item timeline-event-button${selected ? " is-selected" : ""}`;
  button.setAttribute("aria-pressed", selected ? "true" : "false");
  button.innerHTML = `
    <span class="timeline-dot ${statusClass}" aria-hidden="true"></span>
    <span class="timeline-event-main">
      <span class="event-row">
        <span class="event-type">${escapeHtml(humanizeToken(timeline.kind))}</span>
        <time class="event-time">${escapeHtml(formatDate(event.created_at))}</time>
      </span>
      <span class="event-message">${escapeHtml(event.message || humanizeToken(event.type))}</span>
      <span class="event-details">${escapeHtml(meta || formatDetails(event.details || {}))}</span>
      ${reason ? `<span class="event-reason">reason: ${escapeHtml(reason)}</span>` : ""}
    </span>
    <span class="status-chip ${statusClass}">${escapeHtml(timeline.status || event.type)}</span>
  `;
  button.addEventListener("click", () => selectTimelineEvent(event));
  item.append(button);
  return item;
}

function selectTimelineEvent(event) {
  state.selectedEventId = event.id;
  focusGraphForEvent(event);
  if (state.snapshot) {
    renderTimeline(state.snapshot);
  }
}

function renderEventInspector(event) {
  if (!eventInspector) return;
  if (!event) {
    eventInspector.innerHTML = `
      <div class="empty-state compact-empty">
        <strong>No event selected</strong>
        <p>New knowledge activity will appear here.</p>
      </div>
    `;
    return;
  }

  const timeline = timelineEnvelope(event);
  const details = event.details || {};
  const relatedNode = relatedNodeForEvent(event);
  const detailText = formatDetails(details) || "none";
  const metricText = formatMetrics(timeline.metrics) || "none";
  eventInspector.innerHTML = `
    <div class="inspector-summary">
      <span class="status-chip ${timelineStatusClass(event, timeline)}">${escapeHtml(timeline.status || event.type)}</span>
      <strong>${escapeHtml(event.message || humanizeToken(event.type))}</strong>
      <p>${escapeHtml(humanizeToken(timeline.kind))} - ${escapeHtml(formatDate(event.created_at))}</p>
    </div>
    <dl class="inspector-grid">
      <div><dt>Dataset</dt><dd>${escapeHtml(timeline.dataset || "unknown")}</dd></div>
      <div><dt>Source</dt><dd>${escapeHtml(timeline.source || "runtime")}</dd></div>
      <div><dt>Event id</dt><dd>${escapeHtml(String(event.id))}</dd></div>
      <div><dt>Metrics</dt><dd>${escapeHtml(metricText)}</dd></div>
    </dl>
    <div class="inspector-section">
      <h4>Graph focus</h4>
      <p>${escapeHtml(relatedNode ? `${relatedNode.label} - ${relatedNode.type}` : "No graph node matched")}</p>
    </div>
    <div class="inspector-section">
      <h4>Details</h4>
      <p>${escapeHtml(detailText)}</p>
    </div>
  `;
}

function timelineEnvelope(event) {
  if (event.timeline) return event.timeline;
  const details = event.details || {};
  return {
    kind: event.type || "event",
    status: event.type === "error" ? "failed" : details.status || "recorded",
    dataset: details.dataset || details.org || details.vault_id || null,
    source: details.source || details.operation || "runtime",
    metrics: {},
  };
}

function timelineStatusClass(event, timeline = timelineEnvelope(event)) {
  const status = String(timeline.status || event.type || "").toLowerCase();
  if (event.type === "error" || status === "failed") return "status-error";
  if (["pending", "detected", "rejected"].includes(status)) return "status-standby";
  return "status-enabled";
}

function focusGraphForEvent(event) {
  const node = relatedNodeForEvent(event);
  if (node) {
    selectNode(node);
  }
  return node;
}

function relatedNodeForEvent(event) {
  const details = event.details || {};
  const timeline = timelineEnvelope(event);
  const dataset = timeline.dataset || details.dataset;
  if (dataset) {
    const datasetNode = findGraphNode((node) => {
      return node.type === "dataset" && (node.metadata?.dataset === dataset || node.label === dataset);
    });
    if (datasetNode) return datasetNode;
  }
  if (details.vault_id) {
    const vaultNode = findGraphNode((node) => node.metadata?.vault_id === details.vault_id);
    if (vaultNode) return vaultNode;
  }
  if (details.org) {
    const org = String(details.org).trim().toLowerCase();
    const sourceNode = findGraphNode((node) => {
      const label = String(node.label || "").trim().toLowerCase();
      return (
        node.type === "source" &&
        (label === `github / ${org}` || label === `repo content / ${org}`)
      );
    });
    if (sourceNode) return sourceNode;
  }
  return null;
}

function findGraphNode(predicate) {
  for (const node of activeGraphData().nodes.values()) {
    if (predicate(node)) return node;
  }
  return null;
}

function formatMetrics(metrics = {}) {
  return Object.entries(metrics)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${humanizeToken(key)}: ${value}`)
    .join(" | ");
}

function humanizeToken(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function analyticsOutcomeBucket(event) {
  const timeline = timelineEnvelope(event);
  const status = String(timeline.status || event.type || "").toLowerCase();
  if (event.type === "error" || status === "failed") return "failed";
  if (["rejected", "detected", "pending"].includes(status)) return "attention";
  return "ok";
}

function analyticsTimeSeries(events, slots = 10) {
  const parsed = (events || [])
    .map((event) => ({ event, t: new Date(event.created_at).getTime() }))
    .filter((row) => Number.isFinite(row.t));
  if (!parsed.length) return [];
  const newest = Math.max(...parsed.map((row) => row.t));
  const oldest = Math.min(...parsed.map((row) => row.t));
  const span = Math.max(newest - oldest, 60 * 60 * 1000);
  const slotMs = Math.max(Math.ceil(span / slots), 15 * 60 * 1000);
  const start = newest - slotMs * (slots - 1);
  const buckets = Array.from({ length: slots }, (_, index) => ({
    start: start + index * slotMs,
    count: 0,
  }));
  for (const row of parsed) {
    let index = Math.floor((row.t - start) / slotMs);
    if (index < 0) index = 0;
    if (index >= slots) index = slots - 1;
    buckets[index].count += 1;
  }
  const useDays = slotMs >= 20 * 60 * 60 * 1000;
  return buckets.map((bucket) => ({
    label: new Intl.DateTimeFormat(undefined, useDays
      ? { month: "short", day: "numeric" }
      : { hour: "2-digit", minute: "2-digit" }).format(new Date(bucket.start)),
    value: bucket.count,
  }));
}

function analyticsTypeSeries(events, limit = 6) {
  const counts = new Map();
  for (const event of events || []) {
    const key = String(event.type || timelineEnvelope(event).kind || "event");
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return Array.from(counts.entries())
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, limit)
    .map(([key, value]) => ({ label: humanizeToken(key), value, key }));
}

function analyticsStatusSeries(events) {
  const counts = { ok: 0, attention: 0, failed: 0 };
  for (const event of events || []) {
    counts[analyticsOutcomeBucket(event)] += 1;
  }
  return [
    { label: "Ok", value: counts.ok, tone: "ok" },
    { label: "Attention", value: counts.attention, tone: "attention" },
    { label: "Failed", value: counts.failed, tone: "failed" },
  ];
}

function analyticsOpsSeries(stats = {}) {
  const since = stats.since_restart || {};
  return [
    { label: "Sources", value: Number(stats.tracked_sources || 0), tone: "primary" },
    { label: "Nodes", value: Number(stats.nodes || 0), tone: "info" },
    { label: "Searches", value: Number(since.searches || 0), tone: "ok" },
    { label: "Feedback", value: Number(since.feedback || 0), tone: "attention" },
    { label: "Errors", value: Number(since.errors || 0), tone: "failed" },
  ];
}

function setAnalyticsMeta(element, label, tone = "standby") {
  if (!element) return;
  element.textContent = label;
  element.className = `status-chip status-${tone}`;
}

function analyticsEmptyMarkup(title, body) {
  return `
    <div class="empty-state compact-empty">
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(body)}</p>
    </div>
  `;
}

function analyticsToneClass(tone) {
  if (tone === "failed") return "analytics-bar-failed";
  if (tone === "attention") return "analytics-bar-attention";
  if (tone === "ok") return "analytics-bar-ok";
  if (tone === "info") return "analytics-bar-info";
  return "analytics-bar-primary";
}

function renderVerticalBarChart(container, series, { emptyTitle, emptyBody } = {}) {
  if (!container) return;
  const values = series || [];
  const hasData = values.some((row) => Number(row.value) > 0);
  if (!hasData) {
    container.className = "analytics-chart is-empty";
    container.innerHTML = analyticsEmptyMarkup(
      emptyTitle || "No activity yet",
      emptyBody || "Charts fill once the vault records events.",
    );
    return;
  }
  const max = Math.max(...values.map((row) => Number(row.value) || 0), 1);
  const width = 320;
  const height = 108;
  const padX = 10;
  const padTop = 8;
  const padBottom = 22;
  const gap = 5;
  const chartHeight = height - padTop - padBottom;
  const barWidth = (width - padX * 2 - gap * (values.length - 1)) / values.length;
  const bars = values.map((row, index) => {
    const value = Number(row.value) || 0;
    const barHeight = Math.max((value / max) * chartHeight, value > 0 ? 3 : 0);
    const x = padX + index * (barWidth + gap);
    const y = padTop + (chartHeight - barHeight);
    const tone = analyticsToneClass(row.tone);
    const label = escapeHtml(row.label);
    return `
      <g class="analytics-bar-group">
        <rect class="analytics-bar ${tone}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" rx="2" />
        <title>${label}: ${value}</title>
        <text class="analytics-axis-label" x="${(x + barWidth / 2).toFixed(1)}" y="${height - 6}" text-anchor="middle">${label}</text>
      </g>
    `;
  }).join("");
  container.className = "analytics-chart";
  container.innerHTML = `
    <svg class="analytics-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Bar chart">
      <line class="analytics-baseline" x1="${padX}" y1="${padTop + chartHeight}" x2="${width - padX}" y2="${padTop + chartHeight}" />
      ${bars}
    </svg>
  `;
}

function renderHorizontalBarChart(container, series, { emptyTitle, emptyBody } = {}) {
  if (!container) return;
  const values = series || [];
  const hasData = values.some((row) => Number(row.value) > 0);
  if (!hasData) {
    container.className = "analytics-chart is-empty";
    container.innerHTML = analyticsEmptyMarkup(
      emptyTitle || "No activity yet",
      emptyBody || "Charts fill once the vault records events.",
    );
    return;
  }
  const max = Math.max(...values.map((row) => Number(row.value) || 0), 1);
  // SVG rect widths stay CSP-safe (style-src 'self' blocks HTML style attributes).
  const rows = values.map((row) => {
    const value = Number(row.value) || 0;
    const widthPct = Math.max((value / max) * 100, value > 0 ? 4 : 0);
    const tone = analyticsToneClass(row.tone);
    return `
      <div class="analytics-hrow">
        <span class="analytics-hlabel">${escapeHtml(row.label)}</span>
        <div class="analytics-htrack" aria-hidden="true">
          <svg class="analytics-hbar-svg" viewBox="0 0 100 10" preserveAspectRatio="none" focusable="false">
            <rect class="analytics-bar ${tone}" x="0" y="1" width="${widthPct.toFixed(1)}" height="8" rx="1" />
          </svg>
        </div>
        <span class="analytics-hvalue">${escapeHtml(String(value))}</span>
      </div>
    `;
  }).join("");
  container.className = "analytics-chart";
  container.innerHTML = `<div class="analytics-hbar-list">${rows}</div>`;
}

function renderOverviewAnalytics(snapshot) {
  const events = snapshot?.events || [];
  const stats = snapshot?.stats || {};
  const volume = analyticsTimeSeries(events, 8);
  const ops = analyticsOpsSeries(stats);
  const status = analyticsStatusSeries(events);

  renderVerticalBarChart(document.getElementById("overviewChartVolume"), volume, {
    emptyTitle: "No events yet",
    emptyBody: "Sync, ingest, or search to build the activity series.",
  });
  renderHorizontalBarChart(document.getElementById("overviewChartOps"), ops, {
    emptyTitle: "No counters yet",
    emptyBody: "Ops counters appear after vault traffic.",
  });
  renderHorizontalBarChart(document.getElementById("overviewChartStatus"), status, {
    emptyTitle: "No outcomes yet",
    emptyBody: "Status mix appears once events are recorded.",
  });

  setAnalyticsMeta(
    document.getElementById("overviewAnalyticsVolumeMeta"),
    events.length ? `${events.length} events` : "Empty",
    events.length ? "enabled" : "standby",
  );
  const opsTotal = ops.reduce((sum, row) => sum + row.value, 0);
  setAnalyticsMeta(
    document.getElementById("overviewAnalyticsOpsMeta"),
    opsTotal ? `${opsTotal} · activity since restart` : "Empty",
    opsTotal ? "enabled" : "standby",
  );
  const failed = status.find((row) => row.tone === "failed")?.value || 0;
  setAnalyticsMeta(
    document.getElementById("overviewAnalyticsStatusMeta"),
    events.length ? (failed ? `${failed} failed` : "Healthy") : "Empty",
    events.length ? (failed ? "error" : "enabled") : "standby",
  );
}

function renderActivityAnalytics(snapshot) {
  const events = snapshot?.events || [];
  const volume = analyticsTimeSeries(events, 10);
  const types = analyticsTypeSeries(events, 6);
  const outcomes = analyticsStatusSeries(events).map((row) => ({
    ...row,
    label: row.tone === "ok" ? "Success" : row.label,
  }));

  renderVerticalBarChart(document.getElementById("activityChartVolume"), volume, {
    emptyTitle: "Timeline is quiet",
    emptyBody: "Event volume charts appear after sync or note activity.",
  });
  renderHorizontalBarChart(document.getElementById("activityChartTypes"), types, {
    emptyTitle: "No types yet",
    emptyBody: "Type mix fills from ingest, search, sync, and errors.",
  });
  renderHorizontalBarChart(document.getElementById("activityChartOutcome"), outcomes, {
    emptyTitle: "No outcomes yet",
    emptyBody: "Success vs fail appears once events are recorded.",
  });

  setAnalyticsMeta(
    document.getElementById("activityAnalyticsVolumeMeta"),
    events.length ? `${events.length} events` : "Empty",
    events.length ? "enabled" : "standby",
  );
  setAnalyticsMeta(
    document.getElementById("activityAnalyticsTypeMeta"),
    types.length ? `${types.length} types` : "Empty",
    types.length ? "enabled" : "standby",
  );
  const failed = outcomes.find((row) => row.tone === "failed")?.value || 0;
  setAnalyticsMeta(
    document.getElementById("activityAnalyticsOutcomeMeta"),
    events.length ? (failed ? `${failed} failed` : "All clear") : "Empty",
    events.length ? (failed ? "error" : "enabled") : "standby",
  );
}

function renderDashboardIndexes(indexes = []) {
  if (!dashboardIndexSummary) return;
  dashboardIndexSummary.innerHTML = "";
  renderKnowledgeIndexes(indexes);
  if (!indexes.length) {
    dashboardIndexSummary.append(emptyState("No indexes", "Index status has not reported yet."));
    return;
  }
  indexes.slice(0, 4).forEach((index) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(index.name)}</strong>
        <p>${escapeHtml(index.records)} records</p>
      </div>
      <span class="status-chip status-${escapeHtml(index.status)}">${escapeHtml(index.status)}</span>
    `;
    dashboardIndexSummary.append(item);
  });
}

function renderDashboardRecentEvent(events = []) {
  if (!dashboardRecentLearning) return;
  renderKnowledgeRecentEvents(events);
  renderAuditRuntimeEvents(events);
  const latest = events[0];
  if (!latest) {
    dashboardRecentLearning.className = "empty-state compact-empty";
    dashboardRecentLearning.innerHTML = `
      <strong>No events yet</strong>
      <p>Run sync, save a note, or search to teach the vault.</p>
    `;
    return;
  }
  dashboardRecentLearning.className = "dashboard-event-card";
  dashboardRecentLearning.innerHTML = `
    <strong>${escapeHtml(latest.message)}</strong>
    <p>${escapeHtml(latest.type)} - ${escapeHtml(formatDate(latest.created_at))}</p>
    <p>${escapeHtml(formatDetails(latest.details || {}))}</p>
  `;
}

function renderKnowledgeIndexes(indexes = []) {
  if (!knowledgeIndexList) return;
  knowledgeIndexList.innerHTML = "";
  const recordCount = indexes.reduce((total, index) => total + Number(index.records || 0), 0);
  if (knowledgeRecordCount) knowledgeRecordCount.textContent = String(recordCount);
  if (!indexes.length) {
    knowledgeIndexList.append(emptyState("No index status", "The retrieval indexes have not reported yet."));
    return;
  }
  indexes.forEach((index) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(index.name)}</strong>
        <p>${escapeHtml(index.description || "Knowledge retrieval index")}</p>
      </div>
      <span class="status-chip status-${escapeHtml(index.status)}">${escapeHtml(index.records)} records</span>
    `;
    knowledgeIndexList.append(item);
  });
}

function renderKnowledgeRecentEvents(events = []) {
  if (!knowledgeRecentList) return;
  knowledgeRecentList.innerHTML = "";
  if (!events.length) {
    knowledgeRecentList.append(emptyState("No vault events", "Search, ingest, sync, and feedback events will appear here."));
    return;
  }
  events.slice(0, 5).forEach((event) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(event.message || event.type)}</strong>
        <p>${escapeHtml(event.type)} - ${escapeHtml(formatDate(event.created_at))}</p>
      </div>
      <span class="status-chip ${event.type === "error" ? "status-error" : "status-enabled"}">${escapeHtml(event.type)}</span>
    `;
    knowledgeRecentList.append(item);
  });
}

function renderAuditRuntimeEvents(events = []) {
  if (!auditRuntimeList) return;
  auditRuntimeList.innerHTML = "";
  if (!events.length) {
    auditRuntimeList.append(emptyListItem("No runtime events", "Vault operations will appear after activity."));
    return;
  }
  events.slice(0, 20).forEach((event) => {
    auditRuntimeList.append(
      eventListItem({
        type: event.type,
        created_at: event.created_at,
        message: event.message,
        details: event.details,
        success: event.type !== "error",
      }),
    );
  });
}

function renderDashboardOpenIssue(snapshot) {
  if (!dashboardOpenIssue) return;
  const errorCount = Number((snapshot.stats.since_restart || {}).errors || 0);
  const hasEvents = Boolean(snapshot.events.length);
  if (errorCount > 0) {
    dashboardOpenIssue.className = "dashboard-event-card issue-card";
    dashboardOpenIssue.innerHTML = `
      <strong>${escapeHtml(errorCount)} runtime error${errorCount === 1 ? "" : "s"} since restart</strong>
      <p>Open the events page to inspect failed operations.</p>
    `;
    return;
  }
  if (!hasEvents) {
    dashboardOpenIssue.className = "empty-state compact-empty";
    dashboardOpenIssue.innerHTML = `
      <strong>No activity yet</strong>
      <p>Run source sync to start building the shared vault graph.</p>
    `;
    return;
  }
  dashboardOpenIssue.className = "dashboard-event-card";
  dashboardOpenIssue.innerHTML = `
    <strong>No blocking issues</strong>
    <p>Vault updates are flowing. Keep an eye on weak source links and token scope holds.</p>
  `;
}

function emptyState(title, body) {
  const item = document.createElement("div");
  item.className = "empty-state";
  item.innerHTML = `<strong>${escapeHtml(title)}</strong><p>${escapeHtml(body)}</p>`;
  return item;
}

function setSearchStatus(label, statusClass = "") {
  if (!searchResultStatus) return;
  searchResultStatus.textContent = label;
  searchResultStatus.className = `status-chip ${statusClass}`.trim();
}

function searchLoadingState() {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < 3; index += 1) {
    const item = document.createElement("div");
    item.className = "result-item result-skeleton";
    item.innerHTML = `
      <div class="skeleton-line short"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line medium"></div>
      <div class="skeleton-grid">
        <span></span><span></span><span></span>
      </div>
    `;
    fragment.append(item);
  }
  return fragment;
}

function searchErrorState(message) {
  const item = document.createElement("div");
  item.className = "empty-state issue-card search-error-state";
  item.innerHTML = `
    <strong>Search failed</strong>
    <p>${escapeHtml(message || "The vault could not complete this search.")}</p>
    <button class="secondary-button compact-button" type="button" data-search-retry>Retry</button>
  `;
  return item;
}

function searchEmptyState(response = {}) {
  const item = emptyState(
    "No source-linked results",
    response.note || "Try a broader query, choose a dataset, or add source material."
  );
  if (Array.isArray(response.known_datasets) && response.known_datasets.length) {
    const datasets = document.createElement("p");
    datasets.className = "known-datasets";
    datasets.textContent = `Known datasets: ${response.known_datasets.join(", ")}`;
    item.append(datasets);
  }
  const actions = document.createElement("div");
  actions.className = "empty-actions";
  actions.innerHTML = `
    <button class="secondary-button compact-button" data-page-target="sources" type="button">Review sources</button>
    <button class="secondary-button compact-button" data-page-target="ingest" data-min-role="writer" type="button">Add note</button>
  `;
  actions.querySelectorAll("[data-page-target]").forEach((button) => {
    if (button.dataset.minRole && !canUse(button.dataset.minRole)) {
      button.disabled = true;
    }
    button.addEventListener("click", () => setPage(button.dataset.pageTarget));
  });
  item.append(actions);
  return item;
}

function emptyListItem(title, body) {
  const item = document.createElement("li");
  item.className = "event-item empty-event";
  item.innerHTML = `<strong>${escapeHtml(title)}</strong><p>${escapeHtml(body)}</p>`;
  return item;
}

function eventListItem(event) {
  const item = document.createElement("li");
  const isMcp = isMcpAuditEvent(event);
  item.className = `event-item${isMcp ? " mcp-event" : ""}`;
  const detail = event.detail || event.details || {};
  const eventLabel = event.action || event.type || "event";
  const status = event.success === false ? "failed" : event.success === true ? "ok" : detail.status || "";
  item.innerHTML = `
    <div class="event-row">
      <span class="event-type">${escapeHtml(eventLabel)}</span>
      <time class="event-time">${escapeHtml(formatDate(event.created_at))}</time>
    </div>
    ${status ? `<span class="status-chip ${event.success === false ? "status-error" : "status-enabled"}">${escapeHtml(status)}</span>` : ""}
    <div class="event-message">${escapeHtml(event.actor_name || event.message || "System")}</div>
    <div class="event-details">${escapeHtml(formatDetails(detail))}</div>
  `;
  if (event.success === false) {
    item.classList.add("issue-card");
  }
  return item;
}

function formatDetails(details = {}) {
  return Object.entries(details)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => {
      const rendered = sensitiveDetailPattern.test(key)
        ? "[redacted]"
        : Array.isArray(value)
          ? value.join(", ")
          : value;
      return `${key}: ${rendered}`;
    })
    .join(" | ");
}

function isMcpAuditEvent(event) {
  return Boolean(event?.action?.startsWith("mcp.") || event?.detail?.surface === "mcp");
}

function filteredAuditEvents(events = []) {
  if (state.auditFilter === "mcp") return events.filter(isMcpAuditEvent);
  if (state.auditFilter === "access") return events.filter((event) => !isMcpAuditEvent(event));
  if (state.auditFilter === "failures") return events.filter((event) => event.success === false);
  return events;
}

function renderAuditFilterState(events = []) {
  const filtered = filteredAuditEvents(events);
  const mcpEvents = events.filter(isMcpAuditEvent);
  const mcpFailures = mcpEvents.filter((event) => event.success === false);
  const mcpActors = new Set(mcpEvents.map((event) => event.actor_id || event.actor_name).filter(Boolean));

  auditFilterButtons.forEach((button) => {
    const active = button.dataset.auditFilter === state.auditFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });

  if (auditMcpSummary) {
    auditMcpSummary.innerHTML = `
      <div><dt>MCP events</dt><dd>${escapeHtml(mcpEvents.length)}</dd></div>
      <div><dt>Failures</dt><dd>${escapeHtml(mcpFailures.length)}</dd></div>
      <div><dt>Actors</dt><dd>${escapeHtml(mcpActors.size)}</dd></div>
    `;
  }

  if (auditAccessTitle && auditAccessSubtitle) {
    const labels = {
      all: ["Agent And Access Actions", `${filtered.length} recent audit events`],
      mcp: ["MCP Tool Calls", `${filtered.length} agent tool events`],
      access: ["Access And Admin Actions", `${filtered.length} non-MCP audit events`],
      failures: ["Failed Actions", `${filtered.length} failed audit events`],
    };
    const [title, subtitle] = labels[state.auditFilter] || labels.all;
    auditAccessTitle.textContent = title;
    auditAccessSubtitle.textContent = subtitle;
  }

  return filtered;
}

function findFeedbackId(value) {
  if (!value || typeof value !== "object") return null;
  const directKeys = [
    "qa_id",
    "qaId",
    "question_answer_id",
    "questionAnswerId",
    "answer_id",
    "answerId",
  ];
  for (const key of directKeys) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  for (const key of ["metadata", "payload", "result"]) {
    const candidate = findFeedbackId(value[key]);
    if (candidate) return candidate;
  }
  return null;
}

function compactText(value, maxLength = 520) {
  if (value === null || value === undefined || value === "") return "";
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength - 1)}…`;
}

function resultEnvelope(result) {
  return result && typeof result === "object" && result._citadel
    ? result._citadel
    : {};
}

function resultProvenance(result) {
  const provenance = resultEnvelope(result).provenance;
  return provenance && typeof provenance === "object" ? provenance : {};
}

function resultTitle(result, index) {
  const provenance = resultProvenance(result);
  return (
    provenance.title ||
    result?.title ||
    result?.name ||
    result?.id ||
    `Result ${index + 1}`
  );
}

function resultSummary(result) {
  const body =
    result?.content ??
    result?.body ??
    result?.text ??
    result?.summary ??
    result?.answer ??
    result?.result;
  return compactText(body || result);
}

function resultMetaRows(result) {
  const envelope = resultEnvelope(result);
  const provenance = resultProvenance(result);
  return [
    ["Dataset", envelope.dataset || result?.dataset],
    ["Source", provenance.source || result?.source],
    ["Path", provenance.path],
    ["Session", provenance.session_id],
    ["Hash", envelope.content_sha256 ? envelope.content_sha256.slice(0, 12) : null],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
}

function safeDocumentEndpoint(result) {
  const envelope = resultEnvelope(result);
  const endpoint = envelope.document_endpoint;
  if (
    envelope.retrieval?.document_drilldown_available === true &&
    typeof endpoint === "string" &&
    endpoint.startsWith("/api/documents/")
  ) {
    return endpoint;
  }
  return null;
}

function safeExternalUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function resultBodyText(result) {
  const body =
    result?.body ??
    result?.content ??
    result?.text ??
    result?.summary ??
    result?.answer ??
    result?.result;
  return compactText(body || result, 1200);
}

function documentPreviewMarkup(document) {
  const metadata = document?.metadata && typeof document.metadata === "object" ? document.metadata : {};
  const title = document?.title || document?.path || document?.id || "Source document";
  const body = compactText(document?.body || document?.content || "No document body returned.", 1800);
  const rows = [
    ["Source", document?.source || document?.source_type],
    ["Dataset", document?.dataset],
    ["Path", document?.path || document?.normalized_path],
    ["Revision", document?.current_rev || document?.rev],
    ["Checked", metadata.checked_at || metadata.digest_at || document?.updated_at],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
  return `
    <div class="document-preview-card">
      <div class="document-preview-heading">
        <strong>${escapeHtml(title)}</strong>
        <span class="status-chip status-enabled">Source preview</span>
      </div>
      ${
        rows.length
          ? `<dl class="result-provenance document-preview-meta">${rows
              .map(
                ([label, value]) => `
                  <div>
                    <dt>${escapeHtml(label)}</dt>
                    <dd>${escapeHtml(value)}</dd>
                  </div>
                `
              )
              .join("")}</dl>`
          : ""
      }
      <pre class="result-body document-preview-body">${escapeHtml(body)}</pre>
    </div>
  `;
}

async function loadDocumentPreview(button, endpoint, panel) {
  const idleLabel = button.textContent;
  setBusy(button, true, { idle: idleLabel, loading: "Loading source" });
  panel.hidden = false;
  panel.innerHTML = `<div class="empty-state compact-empty"><strong>Loading source</strong><p>Fetching the supporting document.</p></div>`;
  try {
    const response = await api(endpoint);
    panel.innerHTML = documentPreviewMarkup(response.document || response);
  } catch (error) {
    panel.innerHTML = `
      <div class="empty-state issue-card compact-empty">
        <strong>Could not load source</strong>
        <p>${escapeHtml(error.message || "Try again in a moment.")}</p>
      </div>
    `;
  } finally {
    setBusy(button, false, { idle: idleLabel, loading: "Loading source" });
  }
}

function fillFeedbackForm(qaId, score = "1") {
  const form = document.getElementById("feedbackForm");
  const qaInput = form.querySelector("[name='qaId']");
  qaInput.value = qaId;
  qaInput.setAttribute("aria-invalid", "false");
  const scoreInput = form.querySelector(`[name='score'][value='${score}']`);
  if (scoreInput) scoreInput.checked = true;
  form.querySelector("[name='text']").focus();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Label is drawn only once the user zooms past this scale (Central is always
// labelled). Keeps the canvas readable at the default fit-out zoom.
const LABEL_ZOOM_THRESHOLD = 1.6;

// Painted radius in graph units is NODE_REL_SIZE * sqrt(nodeVal); at the
// fit-out zoom of a ~1000-node mesh that is 1-3 screen px for low-degree
// entities — visible but effectively unclickable. The pointer hit area is
// floored to this many SCREEN px (divided by globalScale, the same zoom
// compensation drawNodeLabel uses) without changing the painted size.
const NODE_REL_SIZE = 4;
const MIN_NODE_HIT_RADIUS_PX = 6;

function initializeGraph() {
  if (!window.ForceGraph) {
    console.error("force-graph library failed to load");
    return;
  }
  const rect = canvas.getBoundingClientRect();
  graph.width = Math.max(1, rect.width);
  graph.height = Math.max(1, rect.height);

  graph.instance = window
    .ForceGraph()(canvas)
    .width(graph.width)
    .height(graph.height)
    .backgroundColor("rgba(0,0,0,0)")
    .nodeId("id")
    .nodeRelSize(NODE_REL_SIZE)
    .nodeVal(nodeValue)
    .nodeColor(nodeColor)
    .nodeLabel((node) => escapeHtml(String(node.label || node.id)))
    // The vendored build injects string tooltips via innerHTML, so escape here
    // exactly like nodeLabel above.
    .linkLabel((link) => escapeHtml(String(link.relationship || link.label || "")))
    .nodeCanvasObjectMode(() => "after")
    .nodeCanvasObject(drawNodeLabel)
    // Hover/click detection paints into a hidden hit canvas; floor its radius
    // so small entities stay clickable at fit-out zoom (see MIN_NODE_HIT_RADIUS_PX).
    .nodePointerAreaPaint((node, color, ctx, globalScale) => {
      const painted = NODE_REL_SIZE * Math.sqrt(Math.max(nodeValue(node), 0));
      const radius = Math.max(painted, MIN_NODE_HIT_RADIUS_PX / (globalScale || 1));
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
      ctx.fill();
    })
    .linkColor(linkColor)
    .linkWidth(linkWidth)
    .linkDirectionalParticles(0)
    .cooldownTicks(120)
    .onNodeHover(handleNodeHover)
    .onNodeClick(handleNodeClick)
    .onBackgroundClick(() => selectNode(null))
    // Re-frame once the simulation settles, not on a fixed timer: the layout is
    // still collapsing at 400ms, so an early one-shot zoomToFit left the graph a
    // tiny blob in an empty canvas. needsFit is re-armed on every data load.
    .onEngineStop(() => {
      if (graph.instance && graph.needsFit) {
        graph.needsFit = false;
        graph.instance.zoomToFit(500, 60);
      }
    });

  // Spread the cloud so structure is legible instead of a dense ball: stronger
  // charge repulsion plus a real link distance (defaults clump ~200 nodes onto
  // the pinned Central hub).
  if (typeof graph.instance.d3Force === "function") {
    const charge = graph.instance.d3Force("charge");
    if (charge && typeof charge.strength === "function") charge.strength(-300);
    const link = graph.instance.d3Force("link");
    if (link && typeof link.distance === "function") link.distance(58);
  }
}

// Per-type node colour, dimmed to ~20% alpha when a hover highlight is active
// and this node is not part of the highlighted set.
function nodeColor(node) {
  // Knowledge mode colors by legend kind so nodes always match the legend
  // swatches; projection (live) mode keeps its per-type palette untouched.
  const base =
    state.graphMode === "knowledge"
      ? typeColor(nodeKind(node))
      : typeColor(isSeatNode(node) ? "seat" : node.type);
  if (graph.highlightNodes.size && !graph.highlightNodes.has(node.id)) {
    return withAlpha(base, 0.2);
  }
  if (node.id === state.selectedId) return cssToken("--primary-strong", "#c4b1ff");
  return base;
}

// Thin, desaturated links on the dark canvas; dim to ~20% when highlighting.
function linkColor(link) {
  const dim = graph.highlightLinks.size && !graph.highlightLinks.has(link);
  return dim ? "rgba(123, 123, 130, 0.08)" : "rgba(123, 123, 130, 0.34)";
}

function linkWidth(link) {
  return graph.highlightLinks.has(link) ? 1.6 : 0.5;
}

// Draw the node label after the circle. Central is always labelled; other
// nodes appear only past the zoom threshold so the default view stays clean.
function drawNodeLabel(node, ctx, globalScale) {
  const isCentral = node.id === graph.centralId;
  // Hubs and entity-type concepts carry the map, so they stay labelled at any
  // zoom; individual entities only appear once the user zooms past the
  // threshold, keeping the default framing readable.
  const rawType = String(node.type || "").toLowerCase();
  const isKeyNode =
    isCentral ||
    nodeKind(node) === "dataset" ||
    nodeKind(node) === "seat" ||
    rawType.includes("entitytype") ||
    rawType.includes("nodeset");
  if (!isKeyNode && globalScale < LABEL_ZOOM_THRESHOLD) return;
  if (graph.highlightNodes.size && !graph.highlightNodes.has(node.id) && !isKeyNode) return;
  const label = truncate(String(node.label || node.id), isCentral ? 28 : 22);
  const fontSize = (isCentral ? 13 : 11) / globalScale;
  ctx.font = `600 ${fontSize}px Inter, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const radius = Math.sqrt(Math.max(nodeValue(node), 0)) * 4;
  ctx.fillStyle = cssToken("--text", "#dadada");
  ctx.fillText(label, node.x, node.y + radius + 2 / globalScale);
}

function withAlpha(hex, alpha) {
  const value = String(hex).trim();
  const match = /^#?([0-9a-f]{6})$/i.exec(value);
  if (!match) return value;
  const int = parseInt(match[1], 16);
  const r = (int >> 16) & 255;
  const g = (int >> 8) & 255;
  const b = int & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// Load the active dataset into the force-graph instance, pin Central at the
// origin, and frame the result on first paint.
function renderGraph() {
  if (!graph.instance) return;
  const data = buildForceGraphData();

  // Pin Central at the centre so it always reads as the dominant hub.
  data.nodes.forEach((node) => {
    if (node.id === graph.centralId) {
      node.fx = 0;
      node.fy = 0;
    } else {
      delete node.fx;
      delete node.fy;
    }
  });

  graph.instance.graphData({ nodes: data.nodes, links: data.links });
  renderGraphNodeSearchResults();
  graph.instance.centerAt(0, 0);
  // Arm a settle-fit for this data set (handled by onEngineStop); keep a single
  // coarse early fit on first paint as a fallback if the engine never stops.
  graph.needsFit = true;
  if (!graph.viewInitialized) {
    graph.viewInitialized = true;
    window.setTimeout(() => graph.instance && graph.instance.zoomToFit(600, 60), 400);
  }
}

// Build highlight Sets for the hovered node + its neighbours and links, then
// trigger a recolour so non-neighbours dim to ~20%.
function handleNodeHover(node) {
  graph.highlightNodes.clear();
  graph.highlightLinks.clear();
  graph.hoverId = node ? node.id : null;
  if (node) {
    graph.highlightNodes.add(node.id);
    (node.neighbors || []).forEach((neighbor) => graph.highlightNodes.add(neighbor.id));
    (node.links || []).forEach((link) => graph.highlightLinks.add(link));
  }
  if (canvas) canvas.style.cursor = node ? "pointer" : "grab";
  if (graph.instance) {
    if (typeof graph.instance.nodeColor === "function") graph.instance.nodeColor(nodeColor);
    if (typeof graph.instance.linkColor === "function") graph.instance.linkColor(linkColor);
  }
}

// Centre on the clicked node and update the inspector panel.
function handleNodeClick(node) {
  if (!node) return;
  if (graph.instance && Number.isFinite(node.x) && Number.isFinite(node.y)) {
    graph.instance.centerAt(node.x, node.y, 600);
  }
  selectNode(activeGraphData().nodes.get(node.id) || node);
}

// Compatibility shim: callers across the app still ask to (re)build the scene;
// route them to the force-graph renderer.
function buildGraphScene() {
  renderGraph();
}

// Compatibility shim: the force-graph accessors read state.selectedId directly,
// so a repaint is enough to reflect a new selection.
function updateNodeSelection() {
  if (graph.instance && typeof graph.instance.nodeColor === "function") {
    graph.instance.nodeColor(nodeColor);
  }
}

// Re-frame the whole graph (wired to the Fit button + first paint).
function resetGraphView() {
  if (graph.instance) graph.instance.zoomToFit(600, 40);
}

// Pause/resume the force engine (wired to the Pause button).
function setGraphPaused(paused) {
  if (!graph.instance) return;
  if (paused) {
    if (typeof graph.instance.pauseAnimation === "function") graph.instance.pauseAnimation();
  } else if (typeof graph.instance.resumeAnimation === "function") {
    graph.instance.resumeAnimation();
  }
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function truncate(value, length) {
  return value.length > length ? `${value.slice(0, length - 1)}.` : value;
}

function selectNode(node) {
  state.selectedId = node?.id || null;
  if (!node) {
    selectedNode.textContent = "Select a note or node to inspect its links.";
    updateNodeSelection();
    return;
  }
  if (state.graphMode === "knowledge") {
    renderKnowledgeInspector(node);
  } else {
    selectedNode.innerHTML = `
      <div>
        <strong>${escapeHtml(node.label)}</strong>
        <span>${escapeHtml(node.type)} - ${escapeHtml(node.status)}</span>
      </div>
      <p>${escapeHtml(formatDetails(node.metadata || {}))}</p>
    `;
  }
  // Dataset hubs (seat presence + Central) never have stored document text —
  // the inspector shows their presence counts instead, so skip the fetch
  // rather than fire a guaranteed 404.
  if (state.graphMode === "knowledge" && node.id && node.type !== "dataset") {
    loadNodeDocument(node);
  }
  updateNodeSelection();
}

const MAX_INSPECTOR_NEIGHBORS = 8;

// Knowledge-mode inspector: label, kind + dataset, internal name when the
// backend provides one, then up to MAX_INSPECTOR_NEIGHBORS clickable
// connections from the unfiltered real graph (hidden kinds still listed).
// loadNodeDocument appends the stored document text after this content.
function renderKnowledgeInspector(node) {
  const kind = nodeKind(node);
  const dataset = node.metadata?.dataset;
  const kindLine = dataset ? `${kind} · ${dataset}` : kind;
  let markup = `
    <div>
      <strong>${escapeHtml(node.label || node.id)}</strong>
      <span>${escapeHtml(kindLine)}</span>
    </div>
  `;
  if (node.internal_name) {
    markup += `<p class="node-internal-name">${escapeHtml(node.internal_name)}</p>`;
  }
  // Dataset hubs carry presence counts instead of document text (selectNode
  // skips loadNodeDocument for them). Central gets its own label when it
  // matches exactly; seat:* hubs are "Seat presence" even at zero documents;
  // any other hub (auxiliary dataset, renamed central) is plain "Dataset".
  if (kind === "seat" || kind === "dataset") {
    const documents = Number(node.presence?.documents);
    if (Number.isFinite(documents)) {
      const isCentral = dataset === CENTRAL_DATASET || node.label === CENTRAL_DATASET;
      const isSeat = String(dataset || "").startsWith(SEAT_DATASET_PREFIX);
      const prefix = isCentral ? "Central" : isSeat ? "Seat presence" : "Dataset";
      const line = `${prefix} · ${documents} ${
        documents === 1 ? "document" : "documents"
      }`;
      markup += `<p class="node-presence">${escapeHtml(line)}</p>`;
    }
  }
  selectedNode.innerHTML = markup;

  const neighbors = knowledgeNeighbors(node.id);
  if (!neighbors.length) return;
  const container = document.createElement("div");
  container.className = "node-connections";
  const heading = document.createElement("strong");
  heading.textContent = "Connections";
  container.append(heading);
  neighbors.slice(0, MAX_INSPECTOR_NEIGHBORS).forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "node-connection";
    button.textContent = `${item.relationship} · ${item.node.label || item.node.id}`;
    // Resolve by id at click time: a graph refresh replaces state.realGraph
    // while this button persists, so the captured node object may be stale.
    const targetId = item.node.id;
    button.addEventListener("click", () => {
      const target = state.realGraph?.nodes.get(targetId);
      if (!target) return;
      focusGraphNode(targetId);
      selectNode(target);
    });
    container.append(button);
  });
  if (neighbors.length > MAX_INSPECTOR_NEIGHBORS) {
    const more = document.createElement("span");
    more.className = "node-connections-more";
    more.textContent = `+${neighbors.length - MAX_INSPECTOR_NEIGHBORS} more`;
    container.append(more);
  }
  selectedNode.append(container);
}

// Neighbors of a node straight from state.realGraph edges (either direction).
function knowledgeNeighbors(nodeId) {
  const real = state.realGraph;
  if (!real) return [];
  const result = [];
  for (const edge of real.edges) {
    let otherId = null;
    if (edge.source === nodeId) otherId = edge.target;
    else if (edge.target === nodeId) otherId = edge.source;
    else continue;
    const other = real.nodes.get(otherId);
    if (!other) continue;
    result.push({ relationship: edge.relationship || edge.label || "related", node: other });
  }
  return result;
}

// Entity nodes do not carry readable text themselves, but their graph
// neighbors may be the document-bearing records that do. Keep this check
// separate from nodeKind: TextSummary is useful in the inspector even though
// it is currently rendered as an entity in the legend.
function isDocumentBearingNode(node) {
  const type = String(node?.type || "").toLowerCase();
  return (
    type.includes("document") ||
    type.includes("chunk") ||
    type.includes("summary") ||
    ["text", "pdf", "audio", "image"].includes(type)
  );
}

// Structural edges that lead from any rendered node back to the TextDocument
// it came from: chunks/summaries hang off documents via is_part_of, and
// summaries point at their source via made_from.
const DOCUMENT_WALK_RELATIONSHIPS = new Set(["is_part_of", "made_from"]);
const DOCUMENT_WALK_MAX_HOPS = 3;

// BFS the LOADED payload's is_part_of/made_from edges from a clicked node to
// the nearest document-kind node (TextDocument — the id class /api/documents
// can actually resolve). The prior candidates were the node itself and its
// one-hop document-bearing neighbors, which for most rendered nodes are
// chunk/summary ids that 404 — measured live: nearly every inspector click
// dead-ended while a real document sat 2-3 hops away in the same payload.
function nearestDocumentThroughEdges(node) {
  const real = state.realGraph;
  if (!real || !node?.id) return null;
  if (nodeKind(node) === "document") return { node, hops: 0 };
  const adjacency = new Map();
  for (const edge of real.edges) {
    const relationship = String(edge.relationship || edge.label || "");
    if (!DOCUMENT_WALK_RELATIONSHIPS.has(relationship)) continue;
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
    if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
    adjacency.get(edge.source).push(edge.target);
    adjacency.get(edge.target).push(edge.source);
  }
  const visited = new Set([node.id]);
  let frontier = [node.id];
  for (let hops = 1; hops <= DOCUMENT_WALK_MAX_HOPS; hops += 1) {
    const next = [];
    for (const nodeId of frontier) {
      for (const otherId of adjacency.get(nodeId) || []) {
        if (visited.has(otherId)) continue;
        visited.add(otherId);
        const other = real.nodes.get(otherId);
        if (other && nodeKind(other) === "document") {
          return { node: other, hops };
        }
        next.push(otherId);
      }
    }
    frontier = next;
    if (!frontier.length) break;
  }
  return null;
}

// Chunks name the entities they mention through `contains` edges
// (DocumentChunk -> Entity in cognee's extraction; enumerated live 2026-08-13:
// 868 of the payload's 900 contains edges are chunk->entity). Those chunks —
// not whichever document happens to sit nearest — are what SOURCES an entity.
const SOURCING_RELATIONSHIP = "contains";

// The in-view parent document of a chunk, through its is_part_of edge.
function parentDocumentInView(chunkId) {
  const real = state.realGraph;
  if (!real) return null;
  for (const edge of real.edges) {
    if (String(edge.relationship || edge.label || "") !== "is_part_of") continue;
    const otherId =
      edge.source === chunkId ? edge.target : edge.target === chunkId ? edge.source : null;
    if (!otherId) continue;
    const other = real.nodes.get(otherId);
    if (other && nodeKind(other) === "document") return other;
  }
  return null;
}

// Documents that SOURCE the clicked node: the parents of every chunk that
// `contains` it, ranked by how many of their chunks mention it — the document
// most about the node outranks a daily-update digest that name-drops it once
// (which is exactly the wrong document the unranked one-hop chain used to
// surface for person entities). A parent outside the truncated payload keeps
// the chunk itself as the candidate; /api/documents resolves chunk ids to
// their parent server-side.
function sourcingDocumentCandidates(node) {
  const real = state.realGraph;
  if (!real || !node?.id) return [];
  const byTarget = new Map();
  const seenChunks = new Set();
  for (const edge of real.edges) {
    if (String(edge.relationship || edge.label || "") !== SOURCING_RELATIONSHIP) continue;
    const otherId =
      edge.source === node.id ? edge.target : edge.target === node.id ? edge.source : null;
    if (!otherId || seenChunks.has(otherId)) continue;
    seenChunks.add(otherId);
    const chunk = real.nodes.get(otherId);
    if (!chunk || nodeKind(chunk) !== "chunk") continue;
    const target = parentDocumentInView(otherId) || chunk;
    const entry = byTarget.get(target.id);
    if (entry) {
      entry.count += 1;
    } else {
      byTarget.set(target.id, { node: target, count: 1 });
    }
  }
  // Stable sort: ties keep payload order.
  return Array.from(byTarget.values())
    .sort((a, b) => b.count - a.count)
    .map((entry) => ({
      node: entry.node,
      relationship: SOURCING_RELATIONSHIP,
      note: "Sources this node",
    }));
}

function documentCandidates(node) {
  const candidates = [];
  const seen = new Set();
  const add = (candidate, relationship = null, note = null) => {
    const id = candidate?.id;
    if (!id || seen.has(id) || !isDocumentBearingNode(candidate)) return;
    seen.add(id);
    candidates.push({ node: candidate, relationship, note });
  };

  // 1) Real provenance first: documents whose chunks mention the node,
  //    strongest source on top.
  for (const item of sourcingDocumentCandidates(node)) {
    add(item.node, item.relationship, item.note);
  }
  // 2) The node itself and its one-hop document-bearing neighbors: a clicked
  //    chunk, summary, or document resolves through its own id (the backend
  //    resolves chunk ids to their parent), with the real edge label shown.
  add(node);
  for (const item of knowledgeNeighbors(node.id)) {
    add(item.node, item.relationship);
  }
  // 3) Last resort, and said out loud: the nearest document by structural
  //    edges is reachable content, but nothing ties it to the node as a
  //    source.
  const nearest = nearestDocumentThroughEdges(node);
  if (nearest && nearest.hops > 0) {
    add(nearest.node, null, "Nearest document in view — not a direct source");
  }
  return candidates;
}

// Centre the viewport on a rendered node by id, mirroring handleNodeClick.
// Nodes hidden by the legend filter are not rendered, so those just skip.
function focusGraphNode(nodeId) {
  if (!graph.instance || typeof graph.instance.graphData !== "function") return;
  const rendered = (graph.instance.graphData().nodes || []).find((item) => item.id === nodeId);
  if (rendered && Number.isFinite(rendered.x) && Number.isFinite(rendered.y)) {
    graph.instance.centerAt(rendered.x, rendered.y, 600);
  }
}

function renderedGraphNodes() {
  if (!graph.instance || typeof graph.instance.graphData !== "function") return [];
  const nodes = graph.instance.graphData()?.nodes;
  return Array.isArray(nodes) ? nodes : [];
}

function matchingGraphNodes(query) {
  const normalized = String(query || "").trim().toLowerCase();
  if (!normalized) return [];
  const tokens = normalized.split(/\s+/).filter(Boolean);
  return renderedGraphNodes()
    .map((node) => {
      const label = String(node.label || node.id || "");
      const id = String(node.id || "");
      const type = String(node.type || "");
      const dataset = String(node.metadata?.dataset || "");
      const searchable = `${label} ${id} ${type} ${dataset}`.toLowerCase();
      if (!tokens.every((token) => searchable.includes(token))) return null;
      const lowerLabel = label.toLowerCase();
      const rank =
        lowerLabel === normalized
          ? 0
          : lowerLabel.startsWith(normalized)
            ? 1
            : lowerLabel.includes(normalized)
              ? 2
              : 3;
      return { node, label, rank };
    })
    .filter(Boolean)
    .sort((left, right) => left.rank - right.rank || left.label.localeCompare(right.label))
    .slice(0, 8)
    .map((match) => match.node);
}

function closeGraphNodeSearchResults() {
  if (!graphNodeSearchInput || !graphNodeSearchResults) return;
  graphNodeSearchResults.hidden = true;
  graphNodeSearchInput.setAttribute("aria-expanded", "false");
  graphNodeSearchInput.removeAttribute("aria-activedescendant");
  graphNodeMatchIndex = -1;
}

function setGraphNodeMatchIndex(index) {
  if (!graphNodeSearchInput || !graphNodeSearchResults || !graphNodeMatches.length) return;
  graphNodeMatchIndex = (index + graphNodeMatches.length) % graphNodeMatches.length;
  const options = Array.from(graphNodeSearchResults.querySelectorAll("[role='option']"));
  options.forEach((option, optionIndex) => {
    const active = optionIndex === graphNodeMatchIndex;
    option.classList.toggle("active", active);
    option.setAttribute("aria-selected", active ? "true" : "false");
  });
  const activeOption = options[graphNodeMatchIndex];
  if (!activeOption) return;
  graphNodeSearchInput.setAttribute("aria-activedescendant", activeOption.id);
  activeOption.scrollIntoView({ block: "nearest" });
}

function chooseGraphNodeMatch(node) {
  if (!node || !graphNodeSearchInput) return;
  graphNodeSearchInput.value = String(node.label || node.id || "");
  closeGraphNodeSearchResults();
  handleNodeHover(null);
  handleNodeClick(node);
}

function renderGraphNodeSearchResults() {
  if (!graphNodeSearchInput || !graphNodeSearchResults) return;
  const query = graphNodeSearchInput.value.trim();
  graphNodeSearchResults.innerHTML = "";
  if (!query) {
    graphNodeMatches = [];
    closeGraphNodeSearchResults();
    return;
  }

  graphNodeMatches = matchingGraphNodes(query);
  graphNodeMatchIndex = graphNodeMatches.length ? 0 : -1;
  if (!graphNodeMatches.length) {
    const empty = document.createElement("div");
    empty.className = "graph-node-search-empty";
    empty.setAttribute("role", "option");
    empty.setAttribute("aria-disabled", "true");
    empty.textContent = "No nodes match this view.";
    graphNodeSearchResults.append(empty);
  } else {
    graphNodeMatches.forEach((node, index) => {
      const option = document.createElement("button");
      option.id = `graph-node-search-result-${index}`;
      option.type = "button";
      option.className = `graph-node-search-result${index === 0 ? " active" : ""}`;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", index === 0 ? "true" : "false");
      option.title = String(node.id || "");
      const label = document.createElement("strong");
      label.textContent = String(node.label || node.id || "Unnamed node");
      const meta = document.createElement("span");
      const kind = nodeKind(node);
      const dataset = String(node.metadata?.dataset || "");
      meta.textContent = dataset ? `${kind} / ${dataset}` : kind;
      option.append(label, meta);
      option.addEventListener("click", () => chooseGraphNodeMatch(node));
      graphNodeSearchResults.append(option);
    });
  }
  graphNodeSearchResults.hidden = false;
  graphNodeSearchInput.setAttribute("aria-expanded", "true");
  if (graphNodeMatches.length) {
    graphNodeSearchInput.setAttribute("aria-activedescendant", "graph-node-search-result-0");
  }
}

function clearGraphNodeSearch() {
  if (!graphNodeSearchInput) return;
  graphNodeSearchInput.value = "";
  graphNodeMatches = [];
  closeGraphNodeSearchResults();
}

function closeGraphViewMenu() {
  if (graphViewMenu) graphViewMenu.open = false;
}

function initializeGraphNodeSearch() {
  if (!graphNodeSearch || !graphNodeSearchInput || !graphNodeSearchResults) return;
  graphNodeSearchInput.addEventListener("input", renderGraphNodeSearchResults);
  graphNodeSearchInput.addEventListener("focus", () => {
    if (graphNodeSearchInput.value.trim()) renderGraphNodeSearchResults();
  });
  graphNodeSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" && graphNodeMatches.length) {
      event.preventDefault();
      setGraphNodeMatchIndex(graphNodeMatchIndex + 1);
    } else if (event.key === "ArrowUp" && graphNodeMatches.length) {
      event.preventDefault();
      setGraphNodeMatchIndex(graphNodeMatchIndex - 1);
    } else if (event.key === "Enter" && graphNodeMatchIndex >= 0) {
      event.preventDefault();
      chooseGraphNodeMatch(graphNodeMatches[graphNodeMatchIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeGraphNodeSearchResults();
    }
  });
  graphNodeSearch.addEventListener("focusout", (event) => {
    if (!graphNodeSearch.contains(event.relatedTarget)) closeGraphNodeSearchResults();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!graphNodeSearch.contains(event.target)) closeGraphNodeSearchResults();
    if (graphViewMenu?.open && !graphViewMenu.contains(event.target)) closeGraphViewMenu();
  });
  graphViewMenu?.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeGraphViewMenu();
    graphViewMenu.querySelector("summary")?.focus();
  });
}

// Fetch and render stored document text for a knowledge-graph node into the
// inspector panel. Entity nodes fall through to one-hop document-bearing
// neighbors, while a 404 remains a quiet empty state.
async function loadNodeDocument(node) {
  const container = document.createElement("div");
  container.className = "node-document";
  container.innerHTML = "<p>Loading document text…</p>";
  selectedNode.append(container);
  const candidates = documentCandidates(node);
  let firstError = null;

  for (const candidate of candidates) {
    if (state.selectedId !== node.id) return;
    try {
      const data = await api(`/api/documents/${encodeURIComponent(candidate.node.id)}`);
      if (state.selectedId !== node.id) return;
      const doc = data?.document;
      if (!doc || !doc.body) continue;
      const title =
        doc.title && doc.title !== candidate.node.label
          ? `<strong>${escapeHtml(doc.title)}</strong>`
          : "";
      // Provenance line: a sourcing/nearest candidate says WHY this document
      // is shown; other candidates keep the raw edge label; the node's own
      // document needs no line.
      const source = candidate.note
        ? `<p class="node-document-source">${escapeHtml(candidate.note)} · ${escapeHtml(
            candidate.node.label || candidate.node.id
          )}</p>`
        : candidate.node.id !== node.id
          ? `<p class="node-document-source">From ${escapeHtml(
              candidate.relationship || "related"
            )} · ${escapeHtml(candidate.node.label || candidate.node.id)}</p>`
          : "";
      container.innerHTML = `${source}${title}<pre>${escapeHtml(doc.body)}</pre>`;
      return;
    } catch (error) {
      if (state.selectedId !== node.id) return;
      const message = String(error?.message || "Request failed");
      if (error?.status === 404 || /not found/i.test(message)) continue;
      firstError = message;
      break;
    }
  }

  // Typed empty states, never a silent dead-end: say whether the loaded graph
  // simply has no document to walk to, or candidates existed and none resolved.
  container.classList.add("node-document-empty");
  if (firstError) {
    container.innerHTML = `<p>Could not load document text: ${escapeHtml(firstError)}</p>`;
  } else if (!candidates.length) {
    container.innerHTML =
      "<p>No document reachable from this node in the loaded graph.</p>";
  } else {
    container.innerHTML = "<p>No document text stored for this node.</p>";
  }
}

async function loadMesh(showConnection = true) {
  try {
    const snapshot = await api("/api/mesh");
    mergeGraph(snapshot);
    if (showConnection) {
      setConnectionState("enabled", "Live");
    }
  } catch (error) {
    setConnectionState("error", "Offline");
    meshAlert.hidden = false;
    meshAlertText.textContent = error.message || "Try refreshing the vault.";
    console.error(error);
  }
}

function showToast(message, kind = "info") {
  if (!toastStack) return;
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  toast.textContent = message;
  toastStack.append(toast);
  window.setTimeout(() => {
    toast.classList.add("toast-leaving");
    window.setTimeout(() => toast.remove(), 200);
  }, 4200);
}

function updateGraphMeta(message) {
  if (message) {
    graphMeta.textContent = message;
    return;
  }
  if (state.graphMode === "knowledge") {
    const payload = state.realGraph?.payload;
    if (!payload) {
      graphMeta.textContent = "Loading Knowledge Mesh";
      return;
    }
    // A fallback payload has no real content (only presence hubs), so the
    // "Showing X of Y nodes" line would misread as a healthy but empty mesh.
    // Say what actually happened instead.
    if (payload.fallback === true) {
      const reason = String(payload.fallback_reason || "");
      graphMeta.textContent =
        reason.startsWith("graph_access_unavailable") ||
        reason.startsWith("graph_engine_error")
          ? "Knowledge Mesh unavailable — seat presence only"
          : "Knowledge Mesh is empty — ingest notes or run source sync";
      return;
    }
    const hiddenPresent = new Set();
    for (const node of state.realGraph.nodes.values()) {
      const kind = nodeKind(node);
      if (state.graphHiddenKinds.has(kind)) hiddenPresent.add(kind);
    }
    // Count through the same pipeline the renderer uses (kind filter +
    // aggregation) so "Showing N" never overstates what is on the canvas.
    // Content nodes only: synthetic dataset hubs are excluded so the units
    // match visible_nodes (a content count).
    const shown = buildForceGraphData().nodes.filter(
      (node) => node.type !== "dataset",
    ).length;
    // Isolation-aware payloads report visible_nodes (caller scope) alongside
    // total_nodes (org-wide); older payloads only have total_nodes.
    const visible = Number(payload.visible_nodes);
    let meta;
    if (Number.isFinite(visible)) {
      if (visible === 0) {
        // Isolation can leave a caller with zero content while every seat
        // still renders as a presence hub — say that, not "Showing N of 0".
        let seatCount = 0;
        for (const node of state.realGraph.nodes.values()) {
          if (nodeKind(node) === "seat") seatCount += 1;
        }
        meta = `No content in your scope · ${seatCount} seats visible`;
      } else {
        meta = `Showing ${shown} of ${visible} in your scope`;
        // The server caps the node list at mesh_graph_max_nodes; without this
        // the cut reads as if the legend filter hid the nodes.
        if (payload.truncated && Number.isFinite(Number(payload.limit))) {
          meta += ` · capped at ${payload.limit}`;
        }
        const orgWide = Number(payload.total_nodes);
        if (Number.isFinite(orgWide) && orgWide > visible) {
          meta += ` · ${orgWide} org-wide`;
        }
      }
    } else {
      // Same units as `shown`: content nodes only, so synthetic presence hubs
      // never inflate the denominator (total_nodes is already a raw content
      // count when the payload is truncated).
      let contentTotal = 0;
      for (const node of state.realGraph.nodes.values()) {
        if (node.type !== "dataset") contentTotal += 1;
      }
      const total = payload.truncated ? payload.total_nodes : contentTotal;
      meta = `Showing ${shown} of ${total} nodes`;
      if (payload.truncated && Number.isFinite(Number(payload.limit))) {
        meta += ` · capped at ${payload.limit}`;
      }
    }
    if (hiddenPresent.size) {
      const names = GRAPH_KIND_ORDER.filter((kind) => hiddenPresent.has(kind)).map((kind) =>
        GRAPH_KIND_LABELS[kind].toLowerCase(),
      );
      meta += ` · ${names.join(", ")} hidden`;
    }
    graphMeta.textContent = meta;
    return;
  }
  if (state.snapshot) {
    graphMeta.textContent = `${state.snapshot.default_dataset} - rev ${state.snapshot.revision} - ${formatDate(state.snapshot.generated_at)}`;
  } else {
    graphMeta.textContent = "Waiting for snapshot";
  }
}

function updateRealGraphEmpty() {
  if (!realGraphEmpty) return;
  const payload = state.realGraph?.payload;
  // mesh_presence_hubs() always returns >=1 hub, so nodes.size is never 0 in
  // knowledge mode — the old `!nodes.size` check was dead and a broken engine
  // rendered as a healthy, empty mesh. Key the overlay off payload.fallback
  // instead: a fallback payload (graph engine/access error, or a genuinely
  // empty vault) carries only presence hubs and no real content.
  const show =
    state.graphMode === "knowledge" && Boolean(payload) && payload.fallback === true;
  realGraphEmpty.hidden = !show;
  if (show) {
    const text = realGraphEmpty.querySelector("p");
    if (text) {
      const reason = String(payload.fallback_reason || "");
      const degraded =
        reason.startsWith("graph_access_unavailable") ||
        reason.startsWith("graph_engine_error");
      text.textContent = degraded
        ? `Knowledge Mesh unavailable — showing seat presence only. (${reason})`
        : "The graph has no data yet. Ingest notes or run source sync, then check back.";
    }
  }
}

// Legend chips for the knowledge mode: one per kind present in the unfiltered
// real graph, showing swatch + label + count; pressed = visible. Hidden in the
// projection (live) mode.
function renderGraphLegend() {
  if (!graphLegend) return;
  if (state.graphMode !== "knowledge" || !state.realGraph) {
    graphLegend.hidden = true;
    return;
  }
  const counts = new Map();
  for (const node of state.realGraph.nodes.values()) {
    const kind = nodeKind(node);
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  graphLegend.innerHTML = "";
  // Counts are over the capped in-view slice, not the whole vault — say so on
  // hover so "Chunks 140" doesn't read as vault composition.
  graphLegend.title = "counts in this view";
  for (const kind of GRAPH_KIND_ORDER) {
    const count = counts.get(kind);
    if (!count) continue;
    const visible = !state.graphHiddenKinds.has(kind);
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `legend-chip${visible ? " active" : ""}`;
    chip.setAttribute("aria-pressed", visible ? "true" : "false");
    chip.title = visible ? `Hide ${GRAPH_KIND_LABELS[kind].toLowerCase()}` : `Show ${GRAPH_KIND_LABELS[kind].toLowerCase()}`;
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = typeColor(kind);
    chip.append(swatch, document.createTextNode(`${GRAPH_KIND_LABELS[kind]} ${count}`));
    chip.addEventListener("click", () => toggleGraphKind(kind));
    graphLegend.append(chip);
  }
  graphLegend.hidden = !graphLegend.children.length;
}

function toggleGraphKind(kind) {
  if (state.graphHiddenKinds.has(kind)) {
    state.graphHiddenKinds.delete(kind);
  } else {
    state.graphHiddenKinds.add(kind);
  }
  renderGraphLegend();
  buildGraphScene();
  updateGraphMeta();
}

function setGraphAggregate(enabled) {
  state.graphAggregate = Boolean(enabled);
  clearGraphNodeSearch();
  if (graphAggregateButton) {
    graphAggregateButton.classList.toggle("active", state.graphAggregate);
    graphAggregateButton.setAttribute(
      "aria-pressed",
      state.graphAggregate ? "true" : "false",
    );
    graphAggregateButton.title = state.graphAggregate
      ? "Concept map: documents and chunks fold into hub counts. Click for the raw graph."
      : "Raw graph: every payload node renders (legend chips filter kinds). Click to aggregate.";
  }
  buildGraphScene();
  resetGraphView();
  updateGraphMeta();
}

// Dataset filter options: the org-wide view, the caller's own Node first, then
// the rest of the caller's searchable datasets (ADR-0009: dataset NAMES are
// presence-class and org-visible; content stays scoped server-side).
function updateGraphDatasetFilter() {
  if (!graphDatasetFilter) return;
  const previous = String(state.graphDataset || "");
  const names = [];
  const seen = new Set();
  const push = (name) => {
    const value = String(name || "").trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    names.push(value);
  };
  push(state.defaultDataset);
  for (const name of Array.isArray(state.searchDatasets) ? state.searchDatasets : []) {
    push(name);
  }
  graphDatasetFilter.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All datasets";
  graphDatasetFilter.append(all);
  for (const name of names) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name === state.defaultDataset ? `My seat (${name})` : name;
    graphDatasetFilter.append(option);
  }
  // Keep a selection alive across option rebuilds; an option that vanished
  // falls back to the org-wide view rather than silently filtering on it.
  if (previous && seen.has(previous)) {
    graphDatasetFilter.value = previous;
  } else {
    graphDatasetFilter.value = "";
    state.graphDataset = "";
  }
}

function shapeRealGraph(payload) {
  const degree = new Map();
  const rawEdges = Array.isArray(payload.edges) ? payload.edges : [];
  rawEdges.forEach((edge) => {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  });

  const nodes = new Map();
  const list = Array.isArray(payload.nodes) ? payload.nodes : [];
  list.forEach((node) => {
    const links = degree.get(node.id) || 0;
    const dataset = node.dataset || null;
    const isDatasetHub = (node.type || "node") === "dataset";
    const isSeatHub =
      isDatasetHub && String(dataset || node.label || "").startsWith(SEAT_DATASET_PREFIX);
    const metadata = { type: node.type || "node", links };
    if (dataset) metadata.dataset = dataset;
    // Presence metadata rides on dataset hubs ({documents: N}); pass it
    // through untouched so the inspector can show counts without a fetch.
    const presence =
      node.presence && typeof node.presence === "object" ? node.presence : null;
    nodes.set(node.id, {
      id: node.id,
      label: node.label || node.id,
      type: node.type || "node",
      internal_name: node.internal_name || null,
      presence,
      status: isDatasetHub ? (isSeatHub ? "seat" : "dataset") : "linked",
      size: isDatasetHub ? 72 : clamp(26 + links * 5, 24, 58),
      metadata,
    });
  });

  return {
    nodes,
    edges: rawEdges.filter((edge) => nodes.has(edge.source) && nodes.has(edge.target)),
    payload,
  };
}

// Backoff schedule for a 429 from /api/mesh/graph. The mesh is the default
// dashboard view, so ~15 seats hit it at once on login; a hard error toast on a
// transient backpressure 429 is wrong. Retry a couple of times, jittered so the
// seats don't resync onto the same retry instant, then degrade to a soft status.
const MESH_GRAPH_RETRY_BASES_MS = [400, 1200];

function meshGraphRetryDelay(index) {
  const base = MESH_GRAPH_RETRY_BASES_MS[index] ?? 1200;
  // Random jitter (per client) is what de-syncs 15 seats; the index scales it.
  return Math.round(base + Math.random() * (base / 2));
}

// Pull the fuller graph (server caps at 1000): the overview aggregates it
// down to a concept map, and the raw 200-node slice leaves too few entity
// types/hubs once documents are folded away. An active dataset filter rides
// along as `dataset=<name>` (server filters pre-cap, same response shape);
// no filter = the exact org-wide request this has always sent.
function meshGraphUrl() {
  const base = "/api/mesh/graph?limit=1000";
  const dataset = String(state.graphDataset || "");
  return dataset ? `${base}&dataset=${encodeURIComponent(dataset)}` : base;
}

async function fetchMeshGraphWithBackoff() {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await api(meshGraphUrl());
    } catch (error) {
      const retriable =
        error && error.status === 429 && attempt < MESH_GRAPH_RETRY_BASES_MS.length;
      if (!retriable) throw error;
      if (state.graphMode === "knowledge") {
        updateGraphMeta("Knowledge Mesh is busy — retrying");
      }
      await new Promise((resolve) => setTimeout(resolve, meshGraphRetryDelay(attempt)));
    }
  }
}

async function loadKnowledgeGraph(force = false) {
  // The realGraphLoading guard spans the whole retry sequence, so backoff retries
  // can never stack a second in-flight load (e.g. a concurrent setGraphMode).
  if (state.realGraphLoading) return;
  if (state.realGraph && !force) {
    buildGraphScene();
    resetGraphView();
    updateGraphMeta();
    updateRealGraphEmpty();
    renderGraphLegend();
    return;
  }
  state.realGraphLoading = true;
  updateGraphMeta("Loading Knowledge Mesh");
  try {
    let payload;
    try {
      payload = await fetchMeshGraphWithBackoff();
    } catch (error) {
      // Contract degrade: a node that rejects (or predates) the dataset param
      // must not brick the whole view. Drop the filter, say so visibly, and
      // load the org-wide graph this view has always shown.
      const rejected =
        state.graphDataset && [400, 404, 422].includes(Number(error?.status));
      if (!rejected) throw error;
      showToast("Dataset filter unavailable on this node — showing all datasets", "info");
      state.graphDataset = "";
      if (graphDatasetFilter) graphDatasetFilter.value = "";
      payload = await fetchMeshGraphWithBackoff();
    }
    state.realGraph = shapeRealGraph(payload);
    updateGraphDatasetFilter();
    if (state.graphMode === "knowledge") {
      buildGraphScene();
      resetGraphView();
      updateGraphMeta();
      updateRealGraphEmpty();
      renderGraphLegend();
    }
  } catch (error) {
    if (error && error.status === 429) {
      // Retries exhausted: soft, non-error status (no toast) so a login-burst
      // 429 doesn't read as a failure. Next view switch / refresh retries.
      if (state.graphMode === "knowledge") {
        updateGraphMeta("Knowledge Mesh is busy — refresh in a moment");
      }
    } else {
      showToast(`Could not load the Knowledge Mesh: ${error.message}`, "error");
      if (state.graphMode === "knowledge") {
        updateGraphMeta("Knowledge Mesh unavailable");
      }
    }
  } finally {
    state.realGraphLoading = false;
  }
}

function setGraphMode(mode) {
  if (state.graphMode === mode) return;
  state.graphMode = mode;
  clearGraphNodeSearch();
  if (graphViewLabel) {
    graphViewLabel.textContent = mode === "knowledge" ? "Knowledge Mesh" : "Vault Activity";
  }
  graph.viewInitialized = false;
  graphModeButtons.forEach((button) => {
    const active = button.dataset.graphMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  state.selectedId = null;
  selectedNode.textContent = "Select a note or node to inspect its links.";
  // Aggregate + dataset filter act on the Knowledge Mesh only; hide them in
  // live mode so they never sit on the toolbar as dead controls.
  if (graphAggregateButton) graphAggregateButton.hidden = mode !== "knowledge";
  if (graphDatasetFilter) graphDatasetFilter.hidden = mode !== "knowledge";
  if (mode === "knowledge") {
    canvasEmpty.hidden = true;
    buildGraphScene();
    updateGraphMeta();
    updateRealGraphEmpty();
    renderGraphLegend();
    loadKnowledgeGraph();
    return;
  }
  realGraphEmpty.hidden = true;
  if (graphLegend) graphLegend.hidden = true;
  buildGraphScene();
  resetGraphView();
  updateGraphMeta();
  canvasEmpty.hidden = !state.snapshot || state.snapshot.nodes.length > 4;
}

function conflictSideMarkup(label, side = {}) {
  return `
    <div class="conflict-side">
      <div class="conflict-side-head">
        <span class="conflict-side-label">${escapeHtml(label)}</span>
        <time class="event-time">${escapeHtml(side.timestamp ? formatDate(side.timestamp) : "no timestamp")}</time>
      </div>
      <p class="conflict-source">${escapeHtml(side.source || "unknown source")}</p>
      <pre class="conflict-excerpt">${escapeHtml(side.excerpt || "(empty excerpt)")}</pre>
    </div>
  `;
}

function updateConflictBadge(openCount) {
  if (!conflictNavBadge) return;
  conflictNavBadge.textContent = String(openCount);
  conflictNavBadge.hidden = !openCount;
  if (knowledgeConflictCount) knowledgeConflictCount.textContent = String(openCount);
}

function renderConflicts() {
  if (!conflictsList) return;
  conflictFilterButtons.forEach((button) => {
    const active = button.dataset.conflictFilter === state.conflictFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  conflictsList.innerHTML = "";
  if (!state.conflicts.length) {
    const labels = {
      open: ["No open conflicts", "Disagreements between sources will appear here for review."],
      resolved: ["No resolved conflicts yet", "Resolved conflicts keep their resolution note for the audit trail."],
      all: ["No conflicts recorded", "Disagreements between sources will appear here for review."],
    };
    const [title, body] = labels[state.conflictFilter] || labels.all;
    conflictsList.append(emptyState(title, body));
    return;
  }
  const canResolve = canUse("writer");
  state.conflicts.forEach((conflict) => {
    const open = conflict.status === "open";
    const card = document.createElement("article");
    card.className = `conflict-card${open ? " conflict-open" : ""}`;
    card.innerHTML = `
      <div class="conflict-head">
        <div class="conflict-head-meta">
          <span class="conflict-kind">${escapeHtml(conflict.kind || "conflict")}</span>
          <span class="conflict-id">${escapeHtml(conflict.id || "")}</span>
        </div>
        <div class="conflict-head-meta">
          <time class="event-time">${escapeHtml(formatDate(conflict.detected_at))}</time>
          <span class="status-chip ${open ? "status-standby" : "status-enabled"}">${escapeHtml(conflict.status || "open")}</span>
        </div>
      </div>
      <p class="conflict-summary">${escapeHtml(conflict.summary || "Conflicting knowledge detected.")}</p>
      <div class="conflict-sides">
        ${conflictSideMarkup("Side A", conflict.side_a)}
        ${conflictSideMarkup("Side B", conflict.side_b)}
      </div>
    `;
    if (open) {
      const form = document.createElement("form");
      form.className = "conflict-resolve";
      const input = document.createElement("input");
      input.type = "text";
      input.name = "resolutionNote";
      input.placeholder = "Resolution note (which side wins, and why)";
      input.autocomplete = "off";
      input.required = true;
      input.maxLength = 400;
      input.disabled = !canResolve;
      input.setAttribute("aria-label", "Resolution note");
      const button = document.createElement("button");
      button.type = "submit";
      button.className = "primary-button";
      button.textContent = "Resolve";
      button.disabled = !canResolve;
      if (!canResolve) {
        button.title = "Writer access required";
      }
      form.append(input, button);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const note = input.value.trim();
        if (!note) {
          input.focus();
          return;
        }
        setBusy(button, true, { idle: "Resolve", loading: "Resolving" });
        try {
          await api(`/api/conflicts/${encodeURIComponent(conflict.id)}/resolve`, {
            method: "POST",
            body: JSON.stringify({ resolution_note: note }),
          });
          showToast("Conflict resolved.", "success");
          await loadConflicts();
        } catch (error) {
          showToast(`Could not resolve conflict: ${error.message}`, "error");
          setBusy(button, false, { idle: "Resolve", loading: "Resolving" });
        }
      });
      card.append(form);
    } else {
      const resolution = document.createElement("div");
      resolution.className = "conflict-resolution";
      resolution.innerHTML = `
        <strong>Resolved</strong>
        <span>${escapeHtml(conflict.resolution_note || "No resolution note.")}</span>
        <span>${escapeHtml(conflict.resolved_by || "unknown")} - ${escapeHtml(formatDate(conflict.resolved_at))}</span>
      `;
      card.append(resolution);
    }
    conflictsList.append(card);
  });
}

async function loadConflicts() {
  if (!conflictsList) return;
  if (conflictsStatus) {
    conflictsStatus.textContent = "Loading";
    conflictsStatus.className = "status-chip status-standby";
  }
  try {
    const query = state.conflictFilter === "all" ? "" : `?status=${state.conflictFilter}`;
    const payload = await api(`/api/conflicts${query}`);
    state.conflicts = payload.conflicts || [];
    const openCount = Number(payload.open_count || 0);
    updateConflictBadge(openCount);
    if (conflictsStatus) {
      conflictsStatus.textContent = openCount ? `${openCount} open` : "Clear";
      conflictsStatus.className = `status-chip ${openCount ? "status-standby" : "status-enabled"}`;
    }
    renderConflicts();
  } catch (error) {
    if (conflictsStatus) {
      conflictsStatus.textContent = "Error";
      conflictsStatus.className = "status-chip status-error";
    }
    state.conflicts = [];
    conflictsList.innerHTML = "";
    conflictsList.append(emptyState("Could not load conflicts", error.message));
  }
}

async function loadPromotionQueue() {
  // No early return on the legacy dashboard list: Review reads state.promotions,
  // so this has to run whether or not the old Overview markup is still around.
  if (dashboardPromotionStatus) {
    dashboardPromotionStatus.textContent = "Loading";
    dashboardPromotionStatus.className = "status-chip status-standby";
  }
  try {
    const response = await api("/api/promotion/pending?status=pending");
    state.promotions = response.items || [];
    if (dashboardPromotionList) renderPromotionQueue(state.promotions);
    renderHome();
    renderReview();
    const count = response.count || 0;
    if (dashboardPromotionStatus) {
      dashboardPromotionStatus.textContent = `${count} pending`;
      dashboardPromotionStatus.className = `status-chip ${count ? "status-enabled" : "status-standby"}`;
    }
  } catch (error) {
    state.promotions = [];
    renderHome();
    renderReview();
    if (dashboardPromotionList) {
      dashboardPromotionList.innerHTML = "";
      dashboardPromotionList.append(
        emptyState("Could not load promotion queue", error.message),
      );
    }
    if (dashboardPromotionStatus) {
      dashboardPromotionStatus.textContent = "Error";
      dashboardPromotionStatus.className = "status-chip status-error";
    }
  }
}

function renderPromotionQueue(items = []) {
  if (!dashboardPromotionList) return;
  dashboardPromotionList.innerHTML = "";
  if (!items.length) {
    dashboardPromotionList.append(
      emptyState("No pending promotions", "New org projects will appear here for approval."),
    );
    return;
  }
  items.slice(0, 8).forEach((item) => {
    const row = document.createElement("div");
    row.className = "entity-item";
    const meta = document.createElement("div");
    meta.innerHTML = `
      <strong>${escapeHtml(item.seat_slug || "seat")}</strong>
      <p>${escapeHtml(item.preview || "Pending item")}</p>
      <p>${escapeHtml(item.reference_reason || item.reference_status || "review")}</p>
    `;
    row.append(meta);
    if (canUse("admin")) {
      const approve = document.createElement("button");
      approve.className = "secondary-button compact-button";
      approve.type = "button";
      approve.textContent = "Approve";
      approve.addEventListener("click", () => decidePromotionItem(item.id, "approve"));
      row.append(approve);
    } else {
      const chip = document.createElement("span");
      chip.className = "status-chip status-standby";
      chip.textContent = "pending";
      row.append(chip);
    }
    dashboardPromotionList.append(row);
  });
}

async function decidePromotionItem(itemId, action) {
  const label = action === "approve" ? "approve" : "reject";
  if (!window.confirm(`Confirm: ${label} this promotion item?`)) return;
  try {
    await api(`/api/promotion/pending/${encodeURIComponent(itemId)}/${action}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast(`Promotion item ${label}d`);
    loadPromotionQueue();
  } catch (error) {
    showToast(error.message || `Could not ${label} promotion`, true);
  }
}

async function loadGithubSync() {
  try {
    const status = await api("/api/github-sync");
    state.githubSync = status;
    githubSyncStatus.textContent = status.last_checked_at ? "Tracked" : "Ready";
    githubSyncStatus.className = `status-chip ${status.last_checked_at ? "status-enabled" : "status-standby"}`;
    syncLastChecked.textContent = formatDate(status.last_checked_at);
    syncTrackedRepos.textContent = status.tracked_repositories;
    githubSourceLink.href = status.source_url;
    githubSourceLink.textContent = status.source_url.replace("https://", "");
    if (dashboardCronStatus) {
      dashboardCronStatus.textContent = status.last_checked_at ? "Connected" : "Ready";
      dashboardCronMeta.textContent = status.last_checked_at
        ? `last sync ${formatDate(status.last_checked_at)}`
        : "waiting for first sync";
      dashboardIngestStatus.textContent = status.last_checked_at ? "Running" : "Ready";
      dashboardIngestStatus.className = `status-chip ${status.last_checked_at ? "status-enabled" : "status-standby"}`;
      dashboardIngestionList.innerHTML = `
        <div class="entity-item">
          <div>
            <strong>Source inbox</strong>
            <p>${escapeHtml(status.tracked_repositories)} repositories tracked</p>
          </div>
          <span class="status-chip ${status.last_checked_at ? "status-enabled" : "status-standby"}">${escapeHtml(status.last_checked_at ? "tracked" : "ready")}</span>
        </div>
        <div class="entity-item">
          <div>
            <strong>Linked note index</strong>
            <p>New source material flows into graph and vector memory.</p>
          </div>
          <span class="status-chip status-enabled">live</span>
        </div>
        <div class="entity-item">
          <div>
            <strong>Shared learning loop</strong>
            <p>${status.run_improve ? "Runs improvement after sync." : "Manual improvement enabled from Sources."}</p>
          </div>
          <span class="status-chip ${status.run_improve ? "status-enabled" : "status-standby"}">${status.run_improve ? "auto" : "manual"}</span>
        </div>
      `;
    }
    renderKnowledgeDailyUpdate(status);
    renderKnowledgeSources();
    renderHome();
  } catch (error) {
    state.githubSync = null;
    githubSyncStatus.textContent = "Error";
    githubSyncStatus.className = "status-chip status-error";
    syncResult.innerHTML = "";
    syncResult.append(emptyState("Could not load sync status", error.message));
    if (dashboardCronStatus) {
      dashboardCronStatus.textContent = "Error";
      dashboardCronMeta.textContent = "sync status failed";
      dashboardIngestStatus.textContent = "Error";
      dashboardIngestStatus.className = "status-chip status-error";
      dashboardIngestionList.innerHTML = "";
      dashboardIngestionList.append(emptyState("Could not load source inbox", error.message));
    }
    renderKnowledgeSources(error);
  }
}

async function loadObsidianSources() {
  if (!obsidianSourceStatus || !obsidianSourceList) return;
  try {
    const payload = await api("/api/sources?type=obsidian_vault");
    state.obsidianSources = payload;
    const summary = payload.summary || {};
    const sources = payload.sources || [];
    const vaults = Number(summary.obsidian_vaults || 0);
    const documents = Number(summary.obsidian_documents || 0);
    const conflicts = Number(summary.open_conflicts || 0);
    obsidianVaultCount.textContent = String(vaults);
    obsidianDocumentCount.textContent = String(documents);
    obsidianSourceStatus.textContent = conflicts ? "Conflict" : vaults ? "Connected" : "Ready";
    obsidianSourceStatus.className = `status-chip ${
      conflicts ? "status-error" : vaults ? "status-enabled" : "status-standby"
    }`;
    obsidianSourceList.innerHTML = "";

    if (!sources.length) {
      obsidianSourceList.append(emptyState("No Obsidian vaults", "Register a team vault source."));
    } else {
      sources.forEach((source) => {
        const item = document.createElement("div");
        item.className = "entity-item";
        item.innerHTML = `
          <div>
            <strong>${escapeHtml(source.name || "Obsidian vault")}</strong>
            <p>${escapeHtml(source.documents || 0)} notes · ${escapeHtml(formatDate(source.last_push_at))}</p>
          </div>
          <span class="status-chip ${
            source.open_conflicts ? "status-error" : "status-enabled"
          }">${source.open_conflicts ? "conflict" : "synced"}</span>
        `;
        obsidianSourceList.append(item);
      });
    }

    if (dashboardIngestionList && sources.length) {
      sources.slice(0, 2).forEach((source) => {
        const item = document.createElement("div");
        item.className = "entity-item";
        item.innerHTML = `
          <div>
            <strong>${escapeHtml(source.name || "Obsidian vault")}</strong>
            <p>${escapeHtml(source.documents || 0)} synced notes from the team vault.</p>
          </div>
          <span class="status-chip ${
            source.open_conflicts ? "status-error" : "status-enabled"
          }">${source.open_conflicts ? "review" : "ready"}</span>
        `;
        dashboardIngestionList.append(item);
      });
    }
    renderKnowledgeSources();
  } catch (error) {
    state.obsidianSources = null;
    obsidianSourceStatus.textContent = "Error";
    obsidianSourceStatus.className = "status-chip status-error";
    obsidianSourceList.innerHTML = "";
    obsidianSourceList.append(emptyState("Could not load Obsidian sources", error.message));
    renderKnowledgeSources(error);
  }
}

function renderKnowledgeDailyUpdate(status = state.githubSync) {
  if (!knowledgeDailyUpdate || !knowledgeDigestStatus) return;
  knowledgeDailyUpdate.innerHTML = "";
  if (!status) {
    knowledgeDigestStatus.textContent = "Waiting";
    knowledgeDigestStatus.className = "status-chip status-standby";
    knowledgeDailyUpdate.append(emptyState("No repository status", "Run or load source sync status."));
    return;
  }
  const checked = Boolean(status.last_checked_at);
  knowledgeDigestStatus.textContent = checked ? "Tracked" : "Ready";
  knowledgeDigestStatus.className = `status-chip ${checked ? "status-enabled" : "status-standby"}`;
  const rows = [
    ["Organization", status.org || "masumi-network", checked ? "tracked" : "ready"],
    ["Last digest", formatDate(status.last_digest_at || status.last_checked_at), "daily"],
    ["Repositories", `${status.tracked_repositories || 0} tracked`, "source"],
    ["Recent commits", `${status.tracked_commit_repositories || 0} repositories`, "context"],
  ];
  rows.forEach(([label, value, chip]) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(label)}</strong>
        <p>${escapeHtml(value)}</p>
      </div>
      <span class="status-chip ${checked ? "status-enabled" : "status-standby"}">${escapeHtml(chip)}</span>
    `;
    knowledgeDailyUpdate.append(item);
  });
}

function renderKnowledgeSources(error = null) {
  if (!knowledgeSourceList) return;
  knowledgeSourceList.innerHTML = "";
  if (error) {
    knowledgeSourceList.append(emptyState("Could not load sources", error.message));
    if (knowledgeStatus) {
      knowledgeStatus.textContent = "Source error";
      knowledgeStatus.className = "status-chip status-error";
    }
    return;
  }

  const github = state.githubSync;
  const obsidianPayload = state.obsidianSources || {};
  const obsidianSources = obsidianPayload.sources || [];
  const summary = obsidianPayload.summary || {};
  const sourceRows = [];
  if (github) {
    sourceRows.push({
      name: `GitHub: ${github.org || "organization"}`,
      body: `${github.tracked_repositories || 0} repositories - ${formatDate(github.last_checked_at)}`,
      status: github.last_checked_at ? "tracked" : "ready",
      error: false,
    });
  }
  obsidianSources.forEach((source) => {
    sourceRows.push({
      name: source.name || "Obsidian vault",
      body: `${source.documents || 0} notes - ${formatDate(source.last_push_at)}`,
      status: source.open_conflicts ? "review" : "synced",
      error: Boolean(source.open_conflicts),
    });
  });

  const sourceCount = sourceRows.length;
  const snapshotCount = Number(github?.tracked_repositories || 0) + Number(summary.obsidian_documents || 0);
  const conflictCount = Number(summary.open_conflicts || 0);
  if (knowledgeSourceCount) knowledgeSourceCount.textContent = String(sourceCount);
  if (knowledgeSnapshotCount && !state.snapshot) knowledgeSnapshotCount.textContent = String(snapshotCount);
  if (knowledgeConflictCount) knowledgeConflictCount.textContent = String(conflictCount);

  if (!sourceRows.length) {
    knowledgeSourceList.append(emptyState("No connected sources", "GitHub and Obsidian sources will appear here."));
    return;
  }

  sourceRows.forEach((source) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(source.name)}</strong>
        <p>${escapeHtml(source.body)}</p>
      </div>
      <span class="status-chip ${source.error ? "status-error" : "status-enabled"}">${escapeHtml(source.status)}</span>
    `;
    knowledgeSourceList.append(item);
  });
}

// ---------- Home and Review: shared "needs you" material ----------
//
// Both views answer the same question from the same two lists, so the shaping
// lives here once. Home shows a capped version; Review shows all of it and is
// the only place that renders Approve and Reject.

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function pendingPromotions() {
  return Array.isArray(state.promotions) ? state.promotions : [];
}

// "Failing" is inferred, not reported: /api/sources carries no per-source error
// field. What it does carry is open conflicts, and, for the GitHub org source, a
// security scan that blocked the last run. Those are the two real signals.
function failingSources() {
  const rows = [];
  (state.sources || []).forEach((source) => {
    const conflicts = Number(source.open_conflicts || 0);
    const metadata = source.metadata || {};
    const scan = metadata.last_security_scan || {};
    const name = source.name || source.id || "source";
    if (conflicts > 0) {
      rows.push({
        id: `conflict:${source.id}`,
        title: name,
        body: `${conflicts} open conflict${conflicts === 1 ? "" : "s"} to resolve`,
        target: "conflicts",
      });
    }
    if (scan.blocked === true) {
      rows.push({
        id: `scan:${source.id}`,
        title: name,
        body: `Sync blocked by the security scan (${scan.finding_count || 0} finding${
          scan.finding_count === 1 ? "" : "s"
        }, highest ${scan.highest_severity || "unknown"})`,
        target: "sources",
      });
    }
  });
  return rows;
}

function waitingOnYouCount() {
  return pendingPromotions().length + failingSources().length;
}

function promotionMetaRows(item) {
  const rows = [["Seat", item.seat_slug || "unknown"]];
  // No secret-scan runs over a promotion candidate. What the queue does carry is
  // the enrichment sensitivity flag, and it is labelled as exactly that rather
  // than dressed up as a scan result.
  rows.push([
    "Sensitivity",
    item.sensitive === true ? "flagged" : item.sensitive === false ? "clear" : "not assessed",
  ]);
  rows.push(["Reference", item.reference_status || "unreviewed"]);
  const hints = Array.isArray(item.repo_hints) ? item.repo_hints.length : 0;
  rows.push(["Notes", `1 candidate${hints ? ` · ${hints} repo hint${hints === 1 ? "" : "s"}` : ""}`]);
  return rows;
}

function needsRow({ title, body, meta = [], chip = null }) {
  const row = document.createElement("div");
  row.className = "needs-item";
  const main = document.createElement("div");
  main.className = "needs-main";
  main.innerHTML = `
    <strong>${escapeHtml(title)}</strong>
    <p>${escapeHtml(body)}</p>
    ${
      meta.length
        ? `<dl class="needs-meta">${meta
            .map(
              ([label, value]) =>
                `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`,
            )
            .join("")}</dl>`
        : ""
    }
  `;
  row.append(main);
  const actions = document.createElement("div");
  actions.className = "needs-actions";
  if (chip) {
    const span = document.createElement("span");
    span.className = `status-chip ${chip.tone || "status-standby"}`;
    span.textContent = chip.label;
    actions.append(span);
  }
  row.append(actions);
  return { row, actions };
}

// Approve and Reject only for admins: the endpoints behind them require admin +
// sources:sync, so rendering them for a writer would offer a button that always
// 403s. Writers still see the queue, which is the point of showing it.
function promotionNeedsRow(item) {
  const { row, actions } = needsRow({
    title: `Promotion from ${item.seat_slug || "a seat"}`,
    body: item.preview || item.reference_reason || "Pending candidate",
    meta: promotionMetaRows(item),
    chip: canUse("admin") ? null : { label: "waiting on an admin" },
  });
  if (canUse("admin")) {
    const approve = document.createElement("button");
    approve.className = "primary-button compact-button needs-approve";
    approve.type = "button";
    approve.textContent = "Approve";
    approve.addEventListener("click", () => decidePromotionItem(item.id, "approve"));
    const reject = document.createElement("button");
    reject.className = "secondary-button compact-button";
    reject.type = "button";
    reject.textContent = "Reject";
    reject.addEventListener("click", () => decidePromotionItem(item.id, "reject"));
    actions.append(approve, reject);
  }
  return row;
}

function sourceNeedsRow(source) {
  const { row, actions } = needsRow({
    title: source.title,
    body: source.body,
    chip: { label: "failing", tone: "status-error" },
  });
  const open = document.createElement("button");
  open.className = "secondary-button compact-button";
  open.type = "button";
  open.textContent = "Open";
  open.addEventListener("click", () => setPage(source.target));
  actions.append(open);
  return row;
}

function renderNeedsList(container, { limit = null, emptyTitle, emptyBody } = {}) {
  if (!container) return;
  container.innerHTML = "";
  const promotions = pendingPromotions();
  const sources = failingSources();
  const rows = [
    ...promotions.map((item) => promotionNeedsRow(item)),
    ...sources.map((source) => sourceNeedsRow(source)),
  ];
  if (!rows.length) {
    // A real empty state: nothing is waiting, said plainly, with no card
    // pretending there is something to read.
    const empty = document.createElement("p");
    empty.className = "needs-clear";
    empty.textContent = emptyBody || "Nothing is waiting on you.";
    if (emptyTitle) empty.textContent = `${emptyTitle} ${empty.textContent}`;
    container.append(empty);
    return;
  }
  const shown = limit ? rows.slice(0, limit) : rows;
  shown.forEach((row) => container.append(row));
  if (limit && rows.length > shown.length) {
    const more = document.createElement("button");
    more.className = "secondary-button compact-button needs-more";
    more.type = "button";
    more.textContent = `See all ${rows.length} in Review`;
    more.addEventListener("click", () => setPage("review"));
    container.append(more);
  }
}

// ---------- Home ----------

function homeCapturedThisWeek() {
  const events = (state.snapshot && state.snapshot.events) || [];
  const cutoff = Date.now() - WEEK_MS;
  return events.filter((event) => {
    if (event.type !== "ingest") return false;
    const at = Date.parse(event.created_at || "");
    return !Number.isNaN(at) && at >= cutoff;
  }).length;
}

// "Notes you can read" is a readable-corpus count: this caller's Node plus
// every dataset resolve_search_datasets() gives them (Central included).
// /api/me/summary computes exactly that as `readable_document_count`, from the
// durable relational store.
//
// This used to prefer the /api/mesh field now called `tracked_sources`, which
// was never a document count at all — it is the sum of tracked github repos,
// repo-content files and linear issues (ADR-0020). `document_count` is not the
// right substitute either: it is Node-only, so it silently understates an
// org-wide number by however much Central holds.
//
// Returns null, never 0, when the figure is not available. Zero is a claim
// that the caller can read nothing, which is a different (and reassuring)
// statement from "this node did not report a number".
function homeReadableCount() {
  const summary = state.meSummary;
  if (!summary) return null;
  const readable = summary.readable_document_count;
  return typeof readable === "number" ? readable : null;
}

/** A number that is not there yet reads as a dash, never as zero. */
function countOrDash(value) {
  return typeof value === "number" ? String(value) : "—";
}

// Why the number is missing, or "" when it is not missing. Mirrors the Next
// port's Home tile (web/src/pages/app/index.tsx): a failed fetch and a node
// that did not report the field are different states and must not both render
// as a bare dash.
function homeReadableNote() {
  if (state.meSummaryError) return "Unavailable";
  if (homeReadableCount() === null) return "Not reported by this node yet";
  return "";
}

function renderHomeRecent() {
  const list = document.getElementById("homeActivityList");
  if (!list) return;
  list.innerHTML = "";
  const meshEvents = (state.snapshot && state.snapshot.events) || [];
  const events = meshEvents.length
    ? meshEvents
    : (state.meSummary && state.meSummary.recent_activity) || [];
  if (!events.length) {
    list.append(
      emptyState(
        "Nothing yet",
        "Captures, searches, and syncs land here as they happen.",
      ),
    );
    return;
  }
  events.slice(0, 10).forEach((event) => {
    const row = document.createElement("div");
    row.className = "entity-item";
    row.innerHTML = `
      <div>
        <strong>${escapeHtml(humanizeToken(event.type || "event"))}</strong>
        <p>${escapeHtml(event.message || event.dataset || "")}</p>
      </div>
      <span class="status-chip status-standby">${escapeHtml(formatDate(event.created_at))}</span>
    `;
    list.append(row);
  });
}

// Pure render from whatever state has arrived so far. Home is the first page
// most seats open, so it paints immediately and fills in as each fetch lands
// rather than waiting on the slowest one.
function renderHome() {
  const status = document.getElementById("homeStatus");
  if (!status) return;
  const summary = state.meSummary;
  const copy = document.getElementById("homeCopy");
  const title = document.getElementById("homeTitle");

  if (title) {
    title.textContent = state.seatSlug
      ? state.nodeLabel || "Your Node"
      : "Workspace home";
  }
  if (copy) {
    copy.textContent = state.seatSlug
      ? `seat ${state.seatSlug} · private by default, Central still searchable`
      : "No seat on this token. Central is searchable; there is no private Node.";
  }

  const syncedAt = document.getElementById("homeSyncedAt");
  if (syncedAt) {
    const last = state.githubSync && state.githubSync.last_checked_at;
    syncedAt.textContent = last ? `Last sync ${formatDate(last)}` : "Never synced";
  }

  // Activity counters are published only under stats.since_restart (#196).
  const homeStats = (state.snapshot || {}).stats || {};
  const errors = Number((homeStats.since_restart || {}).errors || 0);
  if (!state.snapshot) {
    status.textContent = "Loading";
    status.className = "status-chip status-standby";
  } else if (errors > 0) {
    status.textContent = `${errors} error${errors === 1 ? "" : "s"}`;
    status.className = "status-chip status-error";
  } else {
    status.textContent = "Healthy";
    status.className = "status-chip status-enabled";
  }

  const readable = document.getElementById("homeReadable");
  if (readable) readable.textContent = countOrDash(homeReadableCount());
  const readableNote = document.getElementById("homeReadableNote");
  if (readableNote) {
    const note = homeReadableNote();
    readableNote.textContent = note;
    readableNote.hidden = !note;
  }
  const captured = document.getElementById("homeCapturedWeek");
  if (captured) captured.textContent = String(homeCapturedThisWeek());
  const waiting = document.getElementById("homeWaiting");
  const waitingCard = document.getElementById("homeWaitingCard");
  const waitingCount = waitingOnYouCount();
  if (waiting) waiting.textContent = String(waitingCount);
  // The third number is the only one that is ever coloured.
  if (waitingCard) waitingCard.classList.toggle("is-waiting", waitingCount > 0);

  renderNeedsList(document.getElementById("homeNeedsList"), {
    limit: 5,
    emptyBody: summary && summary.empty
      ? "Nothing is waiting on you. Your Node is still empty, so capture something or search Central."
      : "Nothing is waiting on you.",
  });
  renderHomeRecent();
}

async function loadSeatHome() {
  if (!document.getElementById("homeStatus")) return;
  renderHome();
  try {
    state.meSummary = await api("/api/me/summary");
    state.meSummaryError = false;
  } catch (error) {
    state.meSummary = null;
    state.meSummaryError = true;
    showToast(error.message || "Could not load your Node summary", true);
  }
  renderHome();
}

// ---------- Review ----------

async function loadReview() {
  const status = document.getElementById("reviewStatus");
  if (!status) return;
  status.textContent = "Loading";
  status.className = "status-chip status-standby";
  await Promise.all([loadPromotionQueue(), loadSources()]);
  renderReview();
}

function renderReview() {
  const status = document.getElementById("reviewStatus");
  if (!status) return;
  const pending = pendingPromotions().length;
  const failing = failingSources().length;
  const total = pending + failing;
  status.textContent = total ? `${total} to review` : "All clear";
  status.className = `status-chip ${total ? "status-standby" : "status-enabled"}`;

  const queue = document.getElementById("reviewQueueList");
  if (queue) {
    queue.innerHTML = "";
    const items = pendingPromotions();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "needs-clear";
      empty.textContent = "No promotions are waiting for a decision.";
      queue.append(empty);
    } else {
      items.forEach((item) => queue.append(promotionNeedsRow(item)));
    }
  }

  const sources = document.getElementById("reviewSourceList");
  if (sources) {
    sources.innerHTML = "";
    const rows = failingSources();
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "needs-clear";
      empty.textContent = "Every connected source synced without an error.";
      sources.append(empty);
    } else {
      rows.forEach((source) => sources.append(sourceNeedsRow(source)));
    }
  }
}

// Every source, for the failing-source lists on Home and Review. The Sources
// page keeps its own narrower obsidian_vault fetch.
async function loadSources() {
  try {
    const payload = await api("/api/sources");
    state.sources = payload.sources || [];
  } catch {
    state.sources = [];
  }
  renderHome();
  renderReview();
}

async function loadAccess() {
  if (!canUse("admin")) return;
  accessTokenStatus.textContent = "Loading";
  accessTokenStatus.className = "status-chip status-standby";
  if (agentsStatus) {
    agentsStatus.textContent = "Loading";
    agentsStatus.className = "status-chip status-standby";
  }
  if (auditStatus) {
    auditStatus.textContent = "Loading";
    auditStatus.className = "status-chip status-standby";
  }
  try {
    const snapshot = await api("/api/access");
    state.accessSnapshot = snapshot;
    renderAccess(snapshot);
    accessTokenStatus.textContent = "Ready";
    accessTokenStatus.className = "status-chip status-enabled";
    if (agentsStatus) {
      agentsStatus.textContent = "Ready";
      agentsStatus.className = "status-chip status-enabled";
    }
    if (auditStatus) {
      auditStatus.textContent = "Ready";
      auditStatus.className = "status-chip status-enabled";
    }
  } catch (error) {
    accessTokenStatus.textContent = "Error";
    accessTokenStatus.className = "status-chip status-error";
    if (agentsStatus) {
      agentsStatus.textContent = "Error";
      agentsStatus.className = "status-chip status-error";
    }
    if (auditStatus) {
      auditStatus.textContent = "Error";
      auditStatus.className = "status-chip status-error";
    }
    accessPrincipalList.innerHTML = "";
    accessPrincipalList.append(emptyState("Could not load access", error.message));
    if (accessSeatsList) {
      accessSeatsList.innerHTML = "";
      accessSeatsList.append(emptyState("Could not load seats", error.message));
    }
    if (agentsTokenList) {
      agentsTokenList.innerHTML = "";
      agentsTokenList.append(emptyState("Could not load agents", error.message));
    }
    if (auditAccessList) {
      auditAccessList.innerHTML = "";
      auditAccessList.append(emptyListItem("Could not load audit", error.message));
    }
  }
}

function renderAccess(snapshot) {
  accessPrincipalList.innerHTML = "";
  accessTokenList.innerHTML = "";
  accessAuditList.innerHTML = "";
  renderDashboardMcpAccess(snapshot);
  renderAgents(snapshot);
  loadSeats();
  renderAuditAccessEvents(snapshot.audit_events || []);

  if (!snapshot.principals?.length) {
    accessPrincipalList.append(
      emptyState("No stored principals", "Create a teammate or service-account token."),
    );
  } else {
    snapshot.principals.forEach((principal) => {
      const item = document.createElement("div");
      item.className = "entity-item";
      item.innerHTML = `
        <div>
          <strong>${escapeHtml(principal.name)}</strong>
          <p>${escapeHtml(principal.kind)} - ${escapeHtml(roleLabel(principal.role))}</p>
          <p>${escapeHtml((principal.scopes || []).join(", "))}</p>
          ${principal.seat_slug ? `<p>Seat ${escapeHtml(principal.seat_slug)} - ${escapeHtml(principal.default_dataset || "unset")}</p>` : ""}
        </div>
        <span class="status-chip status-enabled">${escapeHtml(principal.team_id || "default")}</span>
      `;
      accessPrincipalList.append(item);
    });
  }

  if (!snapshot.tokens?.length) {
    accessTokenList.append(emptyState("No stored tokens", "Bootstrap env keys are still available."));
  } else {
    snapshot.tokens.forEach((token) => {
      const item = document.createElement("div");
      item.className = "entity-item";
      const status = token.revoked_at ? "Revoked" : token.expires_at ? "Expires" : "Active";
      item.innerHTML = `
        <div>
          <strong>${escapeHtml(token.name)}</strong>
          <p>${escapeHtml(token.prefix)}... - ${escapeHtml(roleLabel(token.role))}</p>
          <p>Last used ${escapeHtml(formatDate(token.last_used_at))}</p>
          ${token.default_dataset ? `<p>${escapeHtml(token.default_dataset)}</p>` : ""}
        </div>
      `;
      const action = document.createElement("button");
      action.className = "secondary-button compact-button";
      action.type = "button";
      action.textContent = token.revoked_at ? "Revoked" : "Revoke";
      action.disabled = Boolean(token.revoked_at);
      action.title = status;
      action.addEventListener("click", () => revokeAccessToken(token.id));
      item.append(action);
      accessTokenList.append(item);
    });
  }

  const events = snapshot.audit_events || [];
  events.slice(-12).reverse().forEach((event) => {
    accessAuditList.append(eventListItem(event));
  });
  if (!events.length) {
    accessAuditList.append(emptyListItem("No audit events", "Create or revoke a token to start the trail."));
  }
}

function renderAgents(snapshot) {
  if (!agentsTokenList) return;
  agentsTokenList.innerHTML = "";
  const principalsById = new Map((snapshot.principals || []).map((principal) => [principal.id, principal]));
  const agentTokens = (snapshot.tokens || []).filter((token) => {
    const principal = principalsById.get(token.principal_id);
    return principal?.kind === "service_account";
  });
  const activeAgentTokens = agentTokens.filter((token) => !token.revoked_at);
  if (agentsStatus) {
    agentsStatus.textContent = `${activeAgentTokens.length} active`;
    agentsStatus.className = `status-chip ${activeAgentTokens.length ? "status-enabled" : "status-standby"}`;
  }
  if (!agentTokens.length) {
    agentsTokenList.append(emptyState("No service-account tokens", "Create an autonomous agent token from Access."));
    return;
  }
  agentTokens.forEach((token) => {
    const principal = principalsById.get(token.principal_id);
    const revoked = Boolean(token.revoked_at);
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(token.name)}</strong>
        <p>${escapeHtml(roleLabel(token.role))} - ${escapeHtml(principal?.team_id || token.team_id || "default")}</p>
        <p>${escapeHtml((token.scopes || []).join(", "))}</p>
      </div>
      <span class="status-chip ${revoked ? "status-error" : "status-enabled"}">${escapeHtml(revoked ? "revoked" : token.prefix + "...")}</span>
    `;
    agentsTokenList.append(item);
  });
}

async function loadSeats() {
  if (!accessSeatsList) return;
  if (accessSeatsStatus) {
    accessSeatsStatus.textContent = "Loading";
    accessSeatsStatus.className = "status-chip status-standby";
  }
  try {
    const response = await api("/api/access/seats");
    renderSeats(response.seats || []);
    if (accessSeatsStatus) {
      const count = (response.seats || []).length;
      accessSeatsStatus.textContent = `${count} seat${count === 1 ? "" : "s"}`;
      accessSeatsStatus.className = `status-chip ${count ? "status-enabled" : "status-standby"}`;
    }
  } catch (error) {
    accessSeatsList.innerHTML = "";
    accessSeatsList.append(emptyState("Could not load seats", error.message));
    if (accessSeatsStatus) {
      accessSeatsStatus.textContent = "Error";
      accessSeatsStatus.className = "status-chip status-error";
    }
  }
}

async function editSeatCapturePolicy(seatSlug) {
  try {
    const current = await api(`/api/access/seats/${encodeURIComponent(seatSlug)}/capture-policy`);
    const baseline = (current.baseline?.deny_globs || []).join("\n");
    const next = window.prompt(
      `Seat capture policy deny globs for ${seatSlug} (one per line):`,
      baseline,
    );
    if (next === null) return;
    const denyGlobs = next
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    await api(`/api/access/seats/${encodeURIComponent(seatSlug)}/capture-policy`, {
      method: "PUT",
      body: JSON.stringify({ deny_globs: denyGlobs }),
    });
    showToast(`Updated capture policy for ${seatSlug}`);
  } catch (error) {
    showToast(error.message || "Could not update capture policy", true);
  }
}

function renderSeats(seats = []) {
  renderTokenSeatOptions(seats);
  accessSeatsList.innerHTML = "";
  if (!seats.length) {
    accessSeatsList.append(emptyState("No seats yet", "Create a seat to provision a private node."));
    return;
  }
  seats.forEach((seat) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    const meta = document.createElement("div");
    meta.innerHTML = `
      <strong>${escapeHtml(seat.name)}</strong>
      <p>Seat ${escapeHtml(seat.seat_slug)} - ${escapeHtml(seat.node_dataset || "unset")}</p>
      <p>${escapeHtml(roleLabel(seat.role))} - ${seat.active_token_count} active / ${seat.token_count} token${seat.token_count === 1 ? "" : "s"}</p>
    `;
    item.append(meta);
    const tokens = seat.tokens || [];
    if (!tokens.length) {
      const chip = document.createElement("span");
      chip.className = "status-chip status-standby";
      chip.textContent = "no token";
      item.append(chip);
    } else {
      const actions = document.createElement("div");
      actions.className = "seat-token-actions";
      tokens.forEach((token) => {
        const action = document.createElement("button");
        action.className = "secondary-button compact-button";
        action.type = "button";
        const revoked = Boolean(token.revoked);
        action.textContent = revoked ? `${token.prefix}... revoked` : `Revoke ${token.prefix}...`;
        action.disabled = revoked;
        action.title = revoked ? "Revoked" : `Last used ${formatDate(token.last_used_at)}`;
        // Reuse the existing token revoke flow + endpoint.
        action.addEventListener("click", () => revokeAccessToken(token.id));
        actions.append(action);
      });
      item.append(actions);
    }
    const policyButton = document.createElement("button");
    policyButton.className = "secondary-button compact-button";
    policyButton.type = "button";
    policyButton.textContent = "Capture policy";
    policyButton.addEventListener("click", () => editSeatCapturePolicy(seat.seat_slug));
    item.append(policyButton);
    accessSeatsList.append(item);
  });
}

// Keep the token form's seat dropdown in sync with the seats list. Options use
// textContent (no innerHTML) so seat-derived strings need no escaping.
function renderTokenSeatOptions(seats = []) {
  const select = document.getElementById("accessSeat");
  if (!select) return;
  const previous = select.value;
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "No seat — service account";
  select.append(none);
  seats.forEach((seat) => {
    const option = document.createElement("option");
    option.value = seat.seat_slug;
    option.textContent = `${seat.name} — ${seat.seat_slug}`;
    select.append(option);
  });
  // Preserve the prior selection across refreshes when the seat still exists.
  select.value = previous && seats.some((seat) => seat.seat_slug === previous) ? previous : "";
  applyTokenSeatScopeToggle();
}

// When a seat is chosen the seat defines scope, so hide+disable the free-text
// dataset inputs (disabled inputs are excluded from FormData) and show a hint.
// The role select is disabled too, but stays visible with its own hint: the
// seat endpoint always mints the seat's writer token and discards any chosen
// role, so an enabled selector would promise a role the server never grants
// (an operator once picked admin here and walked away holding a writer token).
function applyTokenSeatScopeToggle() {
  const select = document.getElementById("accessSeat");
  if (!select) return;
  const hasSeat = Boolean(select.value);
  const datasetField = document.getElementById("accessDefaultDatasetField");
  const allowedField = document.getElementById("accessAllowedDatasetsField");
  const datasetInput = document.getElementById("accessDefaultDataset");
  const allowedInput = document.getElementById("accessAllowedDatasets");
  const hint = document.getElementById("accessSeatScopeHint");
  const roleSelect = document.getElementById("accessRole");
  const roleHint = document.getElementById("accessRoleSeatHint");
  if (datasetField) datasetField.hidden = hasSeat;
  if (allowedField) allowedField.hidden = hasSeat;
  if (datasetInput) datasetInput.disabled = hasSeat;
  if (allowedInput) allowedInput.disabled = hasSeat;
  if (hint) hint.hidden = !hasSeat;
  if (roleSelect) roleSelect.disabled = hasSeat;
  if (roleHint) roleHint.hidden = !hasSeat;
}

// Describe the credential that was actually minted, read from the server
// response (api_token + principal), never from the form. The seat endpoint
// derives role and datasets from the seat and discards any requested role, so
// echoing the form here would repeat the exact mismatch this line exists to
// expose: a token requested as admin that authenticates as writer.
function mintedTokenSummary(response) {
  const apiToken = (response && response.api_token) || {};
  const principal = (response && response.principal) || {};
  const role = apiToken.role || principal.role || "unknown";
  const seatSlug = principal.seat_slug || null;
  const allowed = Array.isArray(apiToken.allowed_datasets)
    ? apiToken.allowed_datasets.filter(Boolean)
    : [];
  let datasets;
  if (role === "admin") {
    datasets = "every dataset (admin bypasses the allowlist)";
  } else if (allowed.length) {
    datasets = allowed.join(", ");
  } else {
    datasets = "all non-seat datasets (no allowlist on this token)";
  }
  const seat = seatSlug ? `seat ${seatSlug}` : "no seat (service account)";
  return `Role: ${role} · ${seat} · Datasets: ${datasets}`;
}

function renderAuditAccessEvents(events = []) {
  if (!auditAccessList) return;
  auditAccessList.innerHTML = "";
  const filtered = renderAuditFilterState(events);
  if (!filtered.length) {
    auditAccessList.append(emptyListItem("No matching events", "Change the audit filter or run an agent action."));
    return;
  }
  filtered.slice(-30).reverse().forEach((event) => {
    auditAccessList.append(eventListItem(event));
  });
}

async function loadSettings() {
  if (!canUse("admin") || !settingsHealthGrid) return;
  settingsStatus.textContent = "Loading";
  settingsStatus.className = "status-chip status-standby";
  settingsHealthGrid.innerHTML = `
    <div class="skeleton index-skeleton"></div>
    <div class="skeleton index-skeleton"></div>
    <div class="skeleton index-skeleton"></div>
  `;
  try {
    const [ready, learning, mirror, captureBaseline] = await Promise.all([
      api("/readyz"),
      api("/api/learning-agent"),
      api("/api/backup-mirror").catch((error) => ({ ok: false, error: error.message })),
      api("/api/access/capture-baseline").catch((error) => ({ ok: false, error: error.message })),
    ]);
    state.settingsSnapshot = { ready, learning, mirror, captureBaseline };
    renderSettings(state.settingsSnapshot);
    settingsStatus.textContent = "Ready";
    settingsStatus.className = "status-chip status-enabled";
  } catch (error) {
    settingsStatus.textContent = "Error";
    settingsStatus.className = "status-chip status-error";
    settingsHealthGrid.innerHTML = "";
    settingsHealthGrid.append(emptyState("Could not load settings", error.message));
  }
}

function renderSettings(snapshot) {
  if (!settingsHealthGrid) return;
  const ready = snapshot.ready || {};
  const learning = snapshot.learning || {};
  const github = learning.sources?.github || {};
  renderSettingsMirror(snapshot.mirror);
  renderSettingsCaptureBaseline(snapshot.captureBaseline);
  const rows = [
    {
      name: "HTTP service",
      body: `${ready.service || "citadel"} - tenant ${ready.tenant_id || "default"}`,
      status: ready.ok ? "ready" : "error",
      error: !ready.ok,
    },
    {
      name: "Default dataset",
      body: ready.default_dataset || "unset",
      status: "config",
      error: false,
    },
    {
      name: "Auto improvement",
      body: ready.auto_improve ? "Enabled after accepted ingest" : "Manual improvement",
      status: ready.auto_improve ? "enabled" : "manual",
      error: false,
    },
    {
      name: "Global context index",
      body: ready.build_global_context_index ? "Enabled" : "Disabled",
      status: ready.build_global_context_index ? "enabled" : "off",
      error: !ready.build_global_context_index,
    },
    {
      name: "Learning agent",
      body: `${(learning.capabilities || []).join(", ") || "status loaded"}`,
      status: learning.ok ? "ready" : "error",
      error: !learning.ok,
    },
    {
      name: "GitHub source",
      body: `${github.org || "masumi-network"} - ${github.tracked_repositories || 0} repositories`,
      status: github.last_checked_at ? "tracked" : "ready",
      error: false,
    },
  ];

  settingsHealthGrid.innerHTML = "";
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(row.name)}</strong>
        <p>${escapeHtml(row.body)}</p>
      </div>
      <span class="status-chip ${row.error ? "status-error" : "status-enabled"}">${escapeHtml(row.status)}</span>
    `;
    settingsHealthGrid.append(item);
  });
}

function renderSettingsCaptureBaseline(captureBaseline) {
  if (!settingsPolicyList) return;
  settingsPolicyList.innerHTML = "";
  if (!captureBaseline || captureBaseline.ok === false) {
    settingsPolicyList.append(
      emptyState("Could not load capture baseline", captureBaseline?.error || "Admin access required"),
    );
    return;
  }
  const envPatterns = captureBaseline.env_exclude_patterns || [];
  const effective = captureBaseline.effective_deny_globs || [];
  const rows = [
    {
      name: "Env exclude patterns",
      body: envPatterns.length ? envPatterns.join(", ") : "None configured",
      status: envPatterns.length ? "active" : "empty",
      error: false,
    },
    {
      name: "Default org deny globs",
      body: (captureBaseline.default_org_deny_globs || []).slice(0, 6).join(", "),
      status: "template",
      error: false,
    },
    {
      name: "Effective deny globs",
      body: `${effective.length} merged patterns (env + org defaults)`,
      status: "merged",
      error: false,
    },
  ];
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(row.name)}</strong>
        <p>${escapeHtml(row.body)}</p>
      </div>
      <span class="status-chip ${row.error ? "status-error" : "status-enabled"}">${escapeHtml(row.status)}</span>
    `;
    settingsPolicyList.append(item);
  });
}

function renderSettingsMirror(mirror) {
  if (!settingsMirrorList) return;
  settingsMirrorList.innerHTML = "";
  if (!mirror || mirror.ok === false) {
    settingsMirrorList.append(
      emptyState(
        "Mirror status unavailable",
        mirror?.error || "The server did not return mirror status.",
      ),
    );
    return;
  }

  const summary = mirror.summary || {};
  const latest = mirror.latest_export || null;
  const rows = [
    {
      name: "Manifest exporter",
      body: `${mirror.repo || "Vault-Backup-Mirror"} on ${mirror.branch || "main"}`,
      status: mirror.enabled ? "enabled" : "dry-run",
      error: false,
    },
    {
      name: "Tracked state files",
      body: `${summary.available_files || 0}/${summary.tracked_files || 0} available - ${formatBytes(summary.total_bytes)}`,
      status: summary.missing_files ? "partial" : "tracked",
      error: false,
    },
    {
      name: "Latest manifest",
      body: latest
        ? `${latest.snapshot_id || "snapshot"} - ${formatDate(latest.exported_at)}`
        : "No manifest written yet.",
      status: latest ? "ready" : "pending",
      error: false,
    },
  ];

  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(row.name)}</strong>
        <p>${escapeHtml(row.body)}</p>
      </div>
      <span class="status-chip ${row.error ? "status-error" : "status-enabled"}">${escapeHtml(row.status)}</span>
    `;
    settingsMirrorList.append(item);
  });
}

function renderLearningAgentRun(result) {
  const github = result.sources?.github || {};
  const digest = result.organization_digest || {};
  const chat = result.notifications?.google_chat || {};
  const chatLabel = chat.sent
    ? "sent"
    : chat.reason || chat.status_category || (chat.enabled ? "ready" : "disabled");
  const preview = digest.preview
    ? `<pre class="digest-preview">${escapeHtml(digest.preview)}</pre>`
    : "";
  syncResult.innerHTML = `
    <dl class="result-grid">
      <div><dt>Repos scanned</dt><dd>${escapeHtml(github.repos_scanned || 0)}</dd></div>
      <div><dt>Changed</dt><dd>${escapeHtml(github.changed_count || 0)}</dd></div>
      <div><dt>Open PRs</dt><dd>${escapeHtml(github.open_pull_request_count || 0)}</dd></div>
      <div><dt>Merged PRs</dt><dd>${escapeHtml(github.merged_pull_request_count || 0)}</dd></div>
      <div><dt>Digest</dt><dd>${digest.meaningful ? "Meaningful" : "Quiet"}</dd></div>
      <div><dt>Google Chat</dt><dd>${escapeHtml(chatLabel)}</dd></div>
    </dl>
    ${preview}
  `;
}

function renderDashboardMcpAccess(snapshot) {
  if (!dashboardMcpList || !dashboardMcpClients) return;
  const activeTokens = (snapshot.tokens || []).filter((token) => !token.revoked_at);
  dashboardMcpClients.textContent = `${activeTokens.length} active`;
  dashboardMcpMeta.textContent = `${(snapshot.principals || []).length} principals`;
  dashboardMcpStatus.textContent = "Authorized";
  dashboardMcpStatus.className = "status-chip status-enabled";
  dashboardMcpList.innerHTML = "";

  if (!activeTokens.length) {
    dashboardMcpList.append(emptyState("No MCP tokens", "Create a service-account token for agents."));
    return;
  }

  activeTokens.slice(0, 3).forEach((token) => {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(token.name)}</strong>
        <p>${escapeHtml(roleLabel(token.role))} - ${escapeHtml((token.scopes || []).slice(0, 3).join(", "))}</p>
      </div>
      <span class="status-chip status-enabled">${escapeHtml(token.prefix)}...</span>
    `;
    dashboardMcpList.append(item);
  });
}

async function revokeAccessToken(tokenId) {
  accessTokenStatus.textContent = "Revoking";
  accessTokenStatus.className = "status-chip status-standby";
  try {
    await api(`/api/access/tokens/${encodeURIComponent(tokenId)}/revoke`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadAccess();
  } catch (error) {
    accessTokenStatus.textContent = "Error";
    accessTokenStatus.className = "status-chip status-error";
    accessTokenList.prepend(emptyState("Could not revoke token", error.message));
  }
}

function connectEvents() {
  if (!window.EventSource) {
    window.setInterval(() => loadMesh(false), 5000);
    return;
  }

  state.eventSource = new EventSource("/events");
  state.eventSource.addEventListener("open", () => {
    setConnectionState("enabled", "Live");
  });
  state.eventSource.addEventListener("snapshot", (event) => {
    mergeGraph(JSON.parse(event.data));
  });
  state.eventSource.addEventListener("mesh-event", () => {
    loadMesh(false);
  });
  state.eventSource.addEventListener("error", () => {
    setConnectionState("standby", "Reconnecting");
  });
}

document.getElementById("refreshButton").addEventListener("click", () => {
  loadMesh();
  loadGithubSync();
  loadObsidianSources();
  loadConflicts();
  if (state.seatSlug) {
    loadSeatHome();
  }
  if (state.graphMode === "knowledge") {
    loadKnowledgeGraph(true);
  }
  if (canUse("admin")) {
    loadAccess();
    loadSettings();
  }
});
document.getElementById("logoutButton").addEventListener("click", async () => {
  try {
    await api("/admin/logout", { method: "POST" });
  } catch {
    // Cookie may already be gone/expired — the login page is right either way.
  }
  window.location.assign("/login");
});
document.getElementById("meshRetryButton").addEventListener("click", () => loadMesh());
document.getElementById("fitButton").addEventListener("click", () => {
  resetGraphView();
  closeGraphViewMenu();
});
document.getElementById("pauseButton").addEventListener("click", (event) => {
  state.paused = !state.paused;
  event.currentTarget.textContent = state.paused ? "Resume motion" : "Pause motion";
  canvas.classList.toggle("is-paused", state.paused);
  setGraphPaused(state.paused);
  closeGraphViewMenu();
});

auditFilterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.auditFilter = button.dataset.auditFilter || "all";
    renderAuditAccessEvents(state.accessSnapshot?.audit_events || []);
  });
});

conflictFilterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.conflictFilter = button.dataset.conflictFilter || "open";
    loadConflicts();
  });
});

graphModeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setGraphMode(button.dataset.graphMode || "live");
    closeGraphViewMenu();
  });
});

if (graphAggregateButton) {
  graphAggregateButton.addEventListener("click", () => {
    setGraphAggregate(!state.graphAggregate);
    closeGraphViewMenu();
  });
}

if (graphDatasetFilter) {
  graphDatasetFilter.addEventListener("change", (event) => {
    state.graphDataset = String(event.currentTarget.value || "");
    clearGraphNodeSearch();
    // The filter changes what the SERVER returns, so refetch rather than
    // re-shape the cached org-wide payload.
    loadKnowledgeGraph(true);
    closeGraphViewMenu();
  });
}

// Pan, zoom, drag, hover, and click are handled natively by force-graph on the
// canvas it owns; keyboard re-frame via the canvas container.
canvas.addEventListener("keydown", (event) => {
  if (event.key === "Home") {
    resetGraphView();
    event.preventDefault();
  }
});

document.getElementById("githubSyncButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const error = document.getElementById("syncError");
  const force = document.getElementById("syncForce").checked;
  const postToChat = Boolean(syncPostToChat?.checked);
  error.textContent = "";
  syncResult.innerHTML = "";
  syncRunSummary.textContent = "Running";
  syncRunSummary.className = "status-chip status-standby";
  setBusy(button, true, { idle: "Run learning agent", loading: "Running" });
  try {
    const result = await api("/api/learning-agent/run", {
      method: "POST",
      body: JSON.stringify({
        force,
        post_to_chat: postToChat,
        include_digest_preview: true,
      }),
    });
    const chat = result.notifications?.google_chat || {};
    syncRunSummary.textContent = chat.sent ? "Posted" : result.ingested ? "Updated" : "Checked";
    syncRunSummary.className = "status-chip status-enabled";
    renderLearningAgentRun(result);
    await Promise.all([loadMesh(false), loadGithubSync()]);
  } catch (err) {
    error.textContent = err.message;
    syncRunSummary.textContent = "Failed";
    syncRunSummary.className = "status-chip status-error";
  } finally {
    setBusy(button, false, { idle: "Run learning agent", loading: "Running" });
  }
});

if (googleChatTestButton) {
  googleChatTestButton.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const error = document.getElementById("syncError");
    error.textContent = "";
    syncResult.innerHTML = "";
    syncRunSummary.textContent = "Testing";
    syncRunSummary.className = "status-chip status-standby";
    setBusy(button, true, { idle: "Send Google Chat test", loading: "Sending" });
    try {
      const result = await api("/api/learning-agent/google-chat/test", {
        method: "POST",
        body: JSON.stringify({}),
      });
      syncRunSummary.textContent = result.sent ? "Test sent" : "Test skipped";
      syncRunSummary.className = result.sent
        ? "status-chip status-enabled"
        : "status-chip status-error";
      const status = result.reason || result.status_category || (result.sent ? "success" : "not sent");
      syncResult.innerHTML = `
        <dl class="result-grid">
          <div><dt>Google Chat</dt><dd>${escapeHtml(status)}</dd></div>
          <div><dt>Status</dt><dd>${escapeHtml(result.status_code || "n/a")}</dd></div>
          <div><dt>Message</dt><dd>${escapeHtml(result.message_name || "n/a")}</dd></div>
          <div><dt>Thread</dt><dd>${escapeHtml(result.thread_name || "n/a")}</dd></div>
        </dl>
      `;
    } catch (err) {
      error.textContent = err.message;
      syncRunSummary.textContent = "Failed";
      syncRunSummary.className = "status-chip status-error";
    } finally {
      setBusy(button, false, { idle: "Send Google Chat test", loading: "Sending" });
    }
  });
}

document.getElementById("obsidianVaultForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = document.getElementById("obsidianVaultSubmit");
  const error = document.getElementById("obsidianSourceError");
  const vaultName = String(formData.get("vaultName") || "").trim();
  error.textContent = "";
  if (!vaultName) {
    error.textContent = "Add a vault name.";
    form.querySelector("[name='vaultName']").focus();
    return;
  }
  obsidianSourceStatus.textContent = "Registering";
  obsidianSourceStatus.className = "status-chip status-standby";
  setBusy(button, true, { idle: "Register vault", loading: "Registering" });
  try {
    await api("/api/obsidian/vaults", {
      method: "POST",
      body: JSON.stringify({
        vault_name: vaultName,
        team_id: String(formData.get("teamId") || "").trim() || null,
        plugin_version: "web",
      }),
    });
    form.reset();
    await Promise.all([loadObsidianSources(), loadMesh(false)]);
  } catch (err) {
    error.textContent = err.message;
    obsidianSourceStatus.textContent = "Failed";
    obsidianSourceStatus.className = "status-chip status-error";
  } finally {
    setBusy(button, false, { idle: "Register vault", loading: "Registering" });
  }
});

function mcpEndpointUrl() {
  // Derive from the current origin so the snippet always points at this host.
  return `${window.location.origin}/mcp/`;
}

function renderMcpSnippetCard(containerId, label, chip, snippet) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const codeId = `${containerId}Code`;
  container.innerHTML = `
    <article class="mcp-snippet-card">
      <div class="event-row">
        <strong>${escapeHtml(label)}</strong>
        <div class="mcp-snippet-actions">
          <span class="status-chip">${escapeHtml(chip)}</span>
          <button type="button" class="copy-button" data-copy-target="${codeId}">Copy</button>
        </div>
      </div>
      <pre><code id="${codeId}">${escapeHtml(snippet)}</code></pre>
    </article>
  `;
  const button = container.querySelector(".copy-button");
  button.addEventListener("click", () => copyMcpSnippet(codeId));
}

function renderStorageScopeGuide(seatResponse) {
  const container = document.getElementById("storageScopeGuide");
  if (!container) return;
  const slug = seatResponse.principal.seat_slug || "";
  const nodeDataset = seatResponse.principal.default_dataset || `seat:${slug}`;
  container.innerHTML = `
    <p class="storage-scope-explainer">
      This seat's <code>${escapeHtml(nodeDataset)}</code> Node is private agent
      memory. Writes stay on the Node; shared Central copies come from the
      <strong>Promotion Agent</strong> (known org work) or your
      <strong>Promotion Approval</strong> queue (New Org Project).
    </p>
  `;
}

function renderTeammateOnboarding(seatResponse) {
  // The snippet token is the seat-scoped writer token from create_seat
  // (allowed_datasets = seat:{slug} + Central). create_seat forbids the admin
  // role, so this is never an admin token. It is shown once: only the hash is
  // stored, and it is unrecoverable if not copied now.
  const reveal = document.getElementById("newSeatToken");
  const token = seatResponse.token;
  const nodeDataset = seatResponse.principal.default_dataset;
  const endpoint = mcpEndpointUrl();
  reveal.hidden = false;
  reveal.innerHTML = `
    <strong>Seat created</strong>
    <p>Node dataset: ${escapeHtml(nodeDataset)}</p>
    <p>One-time writer token. Citadel stores only the hash; if you do not copy it
      now it cannot be recovered and you will need to re-issue.</p>
    <code>${escapeHtml(token)}</code>
  `;

  const claudeSnippet = `{
  "mcpServers": {
    "citadel": {
      "type": "http",
      "url": "${endpoint}",
      "headers": {
        "Authorization": "Bearer ${token}"
      }
    }
  }
}`;
  const codexSnippet = `[mcp_servers.citadel]
command = "npx"
args = [
  "-y", "mcp-remote",
  "${endpoint}",
  "--header", "Authorization: Bearer ${token}"
]`;
  renderMcpSnippetCard("mcpSnippetClaude", "Claude Code", "http", claudeSnippet);
  renderMcpSnippetCard("mcpSnippetCodex", "Codex", "hosted", codexSnippet);
  renderStorageScopeGuide(seatResponse);
}

async function copyMcpSnippet(elementId) {
  const node = document.getElementById(elementId);
  if (!node) return;
  const text = node.textContent || "";
  const button = document.querySelector(`[data-copy-target="${elementId}"]`);
  try {
    await navigator.clipboard.writeText(text);
    if (button) {
      const original = button.textContent;
      button.textContent = "Copied";
      window.setTimeout(() => {
        button.textContent = original;
      }, 1500);
    }
  } catch (error) {
    if (button) button.textContent = "Copy failed";
  }
}

document.getElementById("accessSeatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = document.getElementById("accessSeatSubmit");
  const error = document.getElementById("accessSeatError");
  const status = document.getElementById("accessSeatStatus");
  const reveal = document.getElementById("newSeatToken");
  const name = String(formData.get("name") || "").trim();
  const slug = String(formData.get("slug") || "").trim();
  error.textContent = "";
  reveal.hidden = true;
  reveal.innerHTML = "";
  ["mcpSnippetClaude", "mcpSnippetCodex", "storageScopeGuide"].forEach((id) => {
    const node = document.getElementById(id);
    if (node) node.innerHTML = "";
  });
  if (!name || !slug) {
    error.textContent = "Name and seat slug are required.";
    return;
  }
  status.textContent = "Creating";
  status.className = "status-chip status-standby";
  setBusy(button, true, { idle: "Create seat and issue writer token", loading: "Creating" });
  try {
    const response = await api("/api/access/seats", {
      method: "POST",
      body: JSON.stringify({
        name,
        slug,
        email: String(formData.get("email") || "").trim() || null,
        role: String(formData.get("role") || "writer"),
        issue_token: true,
      }),
    });
    renderTeammateOnboarding(response);
    form.reset();
    await loadAccess();
  } catch (err) {
    error.textContent = err.message;
    status.textContent = "Failed";
    status.className = "status-chip status-error";
  } finally {
    setBusy(button, false, { idle: "Create seat and issue writer token", loading: "Creating" });
  }
});

document
  .getElementById("accessSeat")
  ?.addEventListener("change", applyTokenSeatScopeToggle);
document.getElementById("accessTokenForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = document.getElementById("accessTokenSubmit");
  const error = document.getElementById("accessTokenError");
  const name = String(formData.get("name") || "").trim();
  error.textContent = "";
  newAccessToken.hidden = true;
  newAccessToken.innerHTML = "";
  if (!name) {
    error.textContent = "Add a teammate or agent name.";
    form.querySelector("[name='name']").focus();
    return;
  }
  accessTokenStatus.textContent = "Creating";
  accessTokenStatus.className = "status-chip status-standby";
  setBusy(button, true, { idle: "Create access token", loading: "Creating" });
  try {
    const seatSlug = String(formData.get("seat") || "").trim();
    let response;
    if (seatSlug) {
      // Seat selected: mint via the seat endpoint. Role/dataset scoping derive
      // from the seat server-side, so only the token name is sent.
      response = await api(`/api/access/seats/${encodeURIComponent(seatSlug)}/tokens`, {
        method: "POST",
        body: JSON.stringify({ token_name: name }),
      });
    } else {
      const allowedRaw = String(formData.get("allowedDatasets") || "").trim();
      const allowedDatasets = allowedRaw
        ? allowedRaw.split(",").map((value) => value.trim()).filter(Boolean)
        : null;
      const defaultDataset = String(formData.get("defaultDataset") || "").trim() || null;
      const defaultSession = String(formData.get("defaultSession") || "").trim() || null;
      response = await api("/api/access/tokens", {
        method: "POST",
        body: JSON.stringify({
          name,
          role: String(formData.get("role") || "reader"),
          kind: String(formData.get("kind") || "service_account"),
          team_id: String(formData.get("teamId") || "").trim() || null,
          default_dataset: defaultDataset,
          default_session: defaultSession,
          allowed_datasets: allowedDatasets,
        }),
      });
    }
    newAccessToken.hidden = false;
    newAccessToken.innerHTML = `
      <strong>Token created</strong>
      <p>${escapeHtml(mintedTokenSummary(response))}</p>
      <p>Copy this now. Citadel stores only the hash and will not show it again.</p>
      <code>${escapeHtml(response.token)}</code>
    `;
    form.reset();
    // reset() clears the seat select but doesn't fire change; restore the
    // dataset inputs so the "No seat" scope fields are visible+enabled again.
    applyTokenSeatScopeToggle();
    await loadAccess();
  } catch (err) {
    error.textContent = err.message;
    accessTokenStatus.textContent = "Failed";
    accessTokenStatus.className = "status-chip status-error";
  } finally {
    setBusy(button, false, { idle: "Create access token", loading: "Creating" });
  }
});

document.getElementById("ingestForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = document.getElementById("ingestSubmit");
  const error = document.getElementById("ingestError");
  const data = String(formData.get("data") || "").trim();
  const tags = String(formData.get("tags") || "")
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
  error.textContent = "";
  form.querySelector("[name='data']").setAttribute("aria-invalid", "false");
  if (!data) {
    error.textContent = "Add note text before saving.";
    form.querySelector("[name='data']").setAttribute("aria-invalid", "true");
    form.querySelector("[name='data']").focus();
    return;
  }
  setBusy(button, true, { idle: "Save to vault", loading: "Indexing" });
  try {
    await api("/ingest", {
      method: "POST",
      body: JSON.stringify({
        data,
        dataset: String(formData.get("dataset") || "").trim() || null,
        tags,
      }),
    });
    form.querySelector("[name='data']").value = "";
    await loadMesh(false);
  } catch (err) {
    error.textContent = err.message;
  } finally {
    setBusy(button, false, { idle: "Save to vault", loading: "Indexing" });
  }
});

// ---------- Search ----------
//
// Scope chips map to the dataset the server queries; type chips map to
// doc_type filters. Both go on the same /search request the app already made,
// so nothing about the API contract changes.
const searchFilters = { scope: "all", types: new Set() };

function searchScopeDataset() {
  if (searchFilters.scope === "central") {
    // Prefer what the session says this caller can search over the hardcoded
    // name, so a deployment whose Central dataset is not "masumi-network" still
    // scopes correctly instead of 403ing on the allowlist.
    const fromSession = (state.searchDatasets || []).find(
      (name) =>
        typeof name === "string" &&
        !name.startsWith(SEAT_DATASET_PREFIX) &&
        name !== "session-traces",
    );
    return fromSession || CENTRAL_DATASET;
  }
  if (searchFilters.scope === "node") {
    return state.seatSlug ? `${SEAT_DATASET_PREFIX}${state.seatSlug}` : null;
  }
  return null;
}

function initializeSearchFilters() {
  const scopeChips = Array.from(document.querySelectorAll("[data-search-scope]"));
  scopeChips.forEach((chip) => {
    chip.addEventListener("click", () => {
      // "My Node only" is meaningless without a seat, so it says so instead of
      // quietly searching everything.
      if (chip.dataset.searchScope === "node" && !state.seatSlug) {
        showToast("This token has no seat, so there is no private Node to search.", true);
        return;
      }
      searchFilters.scope = chip.dataset.searchScope;
      scopeChips.forEach((other) => {
        const on = other === chip;
        other.classList.toggle("is-on", on);
        other.setAttribute("aria-pressed", on ? "true" : "false");
      });
      rerunSearchIfAnswered();
    });
  });

  Array.from(document.querySelectorAll("[data-search-type]")).forEach((chip) => {
    chip.addEventListener("click", () => {
      const value = chip.dataset.searchType;
      const on = !searchFilters.types.has(value);
      if (on) searchFilters.types.add(value);
      else searchFilters.types.delete(value);
      chip.classList.toggle("is-on", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
      rerunSearchIfAnswered();
    });
  });
}

function rerunSearchIfAnswered() {
  const input = document.getElementById("searchQuery");
  const form = document.getElementById("searchForm");
  if (form && input && input.value.trim()) form.requestSubmit();
}

document.getElementById("searchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = document.getElementById("searchSubmit");
  const error = document.getElementById("searchError");
  const results = document.getElementById("searchResults");
  const queryInput = form.querySelector("[name='query']");
  const query = String(formData.get("query") || "").trim();
  error.textContent = "";
  results.innerHTML = "";
  queryInput.setAttribute("aria-invalid", "false");
  if (!query) {
    error.textContent = "Enter a search query.";
    queryInput.setAttribute("aria-invalid", "true");
    queryInput.focus();
    setSearchStatus("Idle");
    return;
  }
  setBusy(button, true, { idle: "Search", loading: "Searching" });
  setSearchStatus("Searching", "status-standby");
  results.append(searchLoadingState());
  try {
    const body = { query, top_k: 10 };
    const dataset = searchScopeDataset();
    if (dataset) body.dataset = dataset;
    if (searchFilters.types.size) body.types = Array.from(searchFilters.types);
    const response = await api("/search", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const returned = response.results || [];
    setSearchStatus(returned.length ? `${returned.length} result${returned.length === 1 ? "" : "s"}` : "No results", returned.length ? "status-enabled" : "status-standby");
    renderSearchResults(returned, response, query);
    await loadMesh(false);
  } catch (err) {

    results.innerHTML = "";
    results.append(searchErrorState(err.message));
    const retry = results.querySelector("[data-search-retry]");
    if (retry) retry.addEventListener("click", () => form.requestSubmit());
    setSearchStatus("Failed", "status-error");
  } finally {
    setBusy(button, false, { idle: "Search", loading: "Searching" });
  }
});

document.getElementById("feedbackForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = document.getElementById("feedbackSubmit");
  const error = document.getElementById("feedbackError");
  const qaInput = form.querySelector("[name='qaId']");
  const qaId = String(formData.get("qaId") || "").trim();
  const scoreValue = String(formData.get("score") || "").trim();
  error.textContent = "";
  feedbackResult.innerHTML = "";
  qaInput.setAttribute("aria-invalid", "false");
  if (!qaId) {
    error.textContent = "Add the QA ID before recording feedback.";
    qaInput.setAttribute("aria-invalid", "true");
    qaInput.focus();
    return;
  }
  feedbackStatus.textContent = "Recording";
  feedbackStatus.className = "status-chip status-standby";
  setBusy(button, true, { idle: "Record feedback", loading: "Recording" });
  try {
    const response = await api("/feedback", {
      method: "POST",
      body: JSON.stringify({
        qa_id: qaId,
        score: scoreValue === "" ? null : Number.parseInt(scoreValue, 10),
        text: String(formData.get("text") || "").trim() || null,
        dataset: String(formData.get("dataset") || "").trim() || null,
        session_id: String(formData.get("sessionId") || "").trim() || null,
      }),
    });
    feedbackStatus.textContent = response.recorded ? "Recorded" : "Skipped";
    feedbackStatus.className = `status-chip ${response.recorded ? "status-enabled" : "status-standby"}`;
    feedbackResult.innerHTML = `
      <dl class="result-grid">
        <div><dt>Recorded</dt><dd>${response.recorded ? "Yes" : "No"}</dd></div>
        <div><dt>Improved</dt><dd>${response.improved ? "Yes" : "No"}</dd></div>
      </dl>
    `;
    await loadMesh(false);
  } catch (err) {
    error.textContent = err.message;
    feedbackStatus.textContent = "Failed";
    feedbackStatus.className = "status-chip status-error";
  } finally {
    setBusy(button, false, { idle: "Record feedback", loading: "Recording" });
  }
});

document.getElementById("upgradeButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const error = document.getElementById("upgradeError");
  error.textContent = "";
  upgradeStatus.textContent = "Running";
  upgradeStatus.className = "status-chip status-standby";
  setBusy(button, true, { idle: "Run improvement", loading: "Improving" });
  try {
    await api("/api/self-upgrade", {
      method: "POST",
      body: JSON.stringify({}),
    });
    upgradeStatus.textContent = "Complete";
    upgradeStatus.className = "status-chip status-enabled";
    await loadMesh(false);
  } catch (err) {
    error.textContent = err.message;
    upgradeStatus.textContent = "Failed";
    upgradeStatus.className = "status-chip status-error";
  } finally {
    setBusy(button, false, { idle: "Run improvement", loading: "Improving" });
  }
});

// Grouped searches use Central, shared traces, then Node, matching the CLI.
// Single-token searches stay flat so the server's cross-dataset ranking remains
// visible. The server does the splitting (`sections` on the /search payload).
const SEARCH_SECTION_ORDER = ["central", "session_traces", "node"];
const SEARCH_SECTION_LABELS = {
  central: "Central",
  session_traces: "Session traces (reference-only, verify before acting)",
  node: "Node",
};

function searchResultCard(result, index) {
  const item = document.createElement("article");
  item.className = "result-item citation-card";
  const feedbackId = findFeedbackId(result);
  const envelope = resultEnvelope(result);
  const provenance = resultProvenance(result);
  const metaRows = resultMetaRows(result);
  const documentEndpoint = safeDocumentEndpoint(result);
  const sourceUrl = safeExternalUrl(provenance.source_url);
  const drilldown = envelope.retrieval?.document_drilldown_available === true;
  const rank = envelope.rank || index + 1;
  const datasetName = envelope.dataset || result?.dataset || "";
  const sourceBadge = datasetSourceBadge(datasetName);
  // Score is whatever the retrieval layer attached; it is frequently absent, so
  // the rank is shown either way rather than printing a hollow "score: null".
  const score =
    typeof result?.score === "number" && Number.isFinite(result.score)
      ? result.score.toFixed(3)
      : null;
  const trust = envelope.trust || envelope.trust_tier || "unattested";
  const sourceLabel =
    provenance.source || provenance.path || datasetName || "unknown source";
  item.innerHTML = `
    <div class="result-header">
      <div>
        <div class="result-meta">#${escapeHtml(rank)} · ${escapeHtml(sourceLabel)}</div>
        <strong>${escapeHtml(resultTitle(result, index))}</strong>
      </div>
      <div class="result-trust-chips" aria-label="Retrieval status">
        <span class="status-chip source-badge source-badge-${escapeHtml(sourceBadge.kind)}">${escapeHtml(sourceBadge.label)}</span>
        <span class="status-chip">${escapeHtml(score ? `score ${score}` : `rank ${rank}`)}</span>
        <span class="status-chip">${escapeHtml(`trust: ${trust}`)}</span>
        <span class="status-chip ${drilldown ? "status-enabled" : ""}">${drilldown ? "Document" : "Chunk"}</span>
      </div>
    </div>
    <p class="result-summary">${escapeHtml(resultBodyText(result))}</p>
    ${
      metaRows.length
        ? `<dl class="result-provenance">${metaRows
            .map(
              ([label, value]) => `
                <div>
                  <dt>${escapeHtml(label)}</dt>
                  <dd>${escapeHtml(value)}</dd>
                </div>
              `
            )
            .join("")}</dl>`
        : ""
    }
    <div class="result-retrieval">
      <span class="citation-required">Citation required before acting</span>
      ${
        sourceUrl
          ? `<a class="result-source-url" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(sourceUrl)}</a>`
          : provenance.source_url
            ? `<span class="result-source-url">${escapeHtml(provenance.source_url)}</span>`
            : ""
      }
    </div>
    <details class="result-raw">
      <summary>Show raw result</summary>
      <pre class="result-body">${escapeHtml(JSON.stringify(result, null, 2))}</pre>
    </details>
  `;
  const actions = document.createElement("div");
  actions.className = "result-actions";
  const previewPanel = document.createElement("div");
  previewPanel.className = "result-document-preview";
  previewPanel.hidden = true;
  if (documentEndpoint) {
    const action = document.createElement("button");
    action.className = "secondary-button result-document-link";
    action.type = "button";
    action.textContent = "Preview source";
    action.addEventListener("click", () => loadDocumentPreview(action, documentEndpoint, previewPanel));
    actions.append(action);
  }
  if (feedbackId) {
    const action = document.createElement("button");
    action.className = "secondary-button result-feedback-button";
    action.type = "button";
    action.textContent = "Use for feedback";
    action.addEventListener("click", () => fillFeedbackForm(feedbackId));
    actions.append(action);
  }
  if (actions.children.length) {
    item.append(actions);
  }
  item.append(previewPanel);
  return item;
}

function isSingleLiteralQuery(query) {
  return String(query || "").trim().split(/\s+/).filter(Boolean).length === 1;
}

function renderSearchResults(results, response = {}, query = "") {
  const container = document.getElementById("searchResults");
  container.innerHTML = "";
  if (!results.length) {
    container.append(searchEmptyState(response));
    return;
  }

  if (isSingleLiteralQuery(query)) {
    results.slice(0, 10).forEach((result, index) => {
      container.append(searchResultCard(result, index));
    });
    return;
  }

  const sections = response.sections;
  const grouped =
    sections && typeof sections === "object"
      ? SEARCH_SECTION_ORDER.filter((key) => (sections[key] || []).length)
      : [];

  if (!grouped.length) {
    // No sections on the payload (older server, or every hit sat outside the
    // three known datasets): fall back to one flat list rather than hiding hits.
    results.slice(0, 10).forEach((result, index) => {
      container.append(searchResultCard(result, index));
    });
    return;
  }

  let index = 0;
  grouped.forEach((key) => {
    const group = document.createElement("section");
    group.className = "result-group";
    const heading = document.createElement("h3");
    heading.className = "result-group-title";
    heading.textContent = `${SEARCH_SECTION_LABELS[key]} · ${sections[key].length}`;
    group.append(heading);
    sections[key].forEach((result) => {
      group.append(searchResultCard(result, index));
      index += 1;
    });
    container.append(group);
  });

  // Anything the server could not place in a section still has to be shown; a
  // silently dropped hit is worse than an extra group.
  const placed = new Set();
  grouped.forEach((key) => sections[key].forEach((hit) => placed.add(hit)));
  const rest = results.filter((hit) => !placed.has(hit));
  if (rest.length) {
    const group = document.createElement("section");
    group.className = "result-group";
    const heading = document.createElement("h3");
    heading.className = "result-group-title";
    heading.textContent = `Other · ${rest.length}`;
    group.append(heading);
    rest.forEach((result) => {
      group.append(searchResultCard(result, index));
      index += 1;
    });
    container.append(group);
  }
}

window.addEventListener("resize", () => {
  // force-graph keeps its own layout; just resize the canvas to the container.
  resizeCanvas();
});

// Home's search bar hands the query to the Search page rather than owning a
// second results list: one search implementation, two entry points.
function initializeHomeSearch() {
  const form = document.getElementById("homeSearchForm");
  const input = document.getElementById("homeSearchQuery");
  if (!form || !input) return;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) {
      input.focus();
      return;
    }
    const target = document.getElementById("searchQuery");
    const searchForm = document.getElementById("searchForm");
    setPage("search");
    if (target) target.value = query;
    if (searchForm) searchForm.requestSubmit();
  });

  document.addEventListener("keydown", (event) => {
    const active = document.activeElement;
    const typing =
      active &&
      (active.tagName === "INPUT" ||
        active.tagName === "TEXTAREA" ||
        active.isContentEditable);
    const shortcut =
      (event.key === "k" && (event.metaKey || event.ctrlKey)) ||
      (event.key === "/" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey);
    if (!shortcut) return;
    event.preventDefault();
    // On Search, focus its own box; anywhere else, go to Home's.
    const onSearch = document.querySelector('[data-page="search"]:not([hidden])');
    if (onSearch) {
      const box = document.getElementById("searchQuery");
      if (box) box.focus();
      return;
    }
    setPage("home");
    input.focus();
    input.select();
  });
}

function initializeReviewControls() {
  const refresh = document.getElementById("reviewRefresh");
  if (refresh) refresh.addEventListener("click", () => loadReview());
}

initializeGraph();
resizeCanvas();
initializeThemeToggle();
initializeGraphNodeSearch();
initializeSearchFilters();
initializeHomeSearch();
initializeReviewControls();
initializeNavigation();
loadSession().then(() => {
  setPage(initialPage());
  loadMesh();
  // The canvas opens on the Knowledge Mesh, so its payload must be fetched at
  // boot — setGraphMode only loads it on a mode switch.
  if (state.graphMode === "knowledge") {
    loadKnowledgeGraph();
  }
  loadGithubSync();
  loadPromotionQueue();
  loadSources();
  loadObsidianSources();
  loadConflicts();
  if (canUse("admin")) {
    loadAccess();
    loadSettings();
  }
  connectEvents();
});
