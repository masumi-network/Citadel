/* Architecture on the landing page.
 *
 * Layers follow docs/diagrams/citadel-architecture.* (clients at the top,
 * storage then sync at the bottom). Store names are what kb/lite_runtime.py
 * configures at head: SQLite, Qdrant, Ladybug. Cadence figures stay out: the
 * evolve interval is operator configuration, not code.
 */

export type ArchItem = {
  id: string;
  label: string;
  sub: string;
  detail: string;
  highlight?: boolean;
};

export type ArchLayer = {
  id: string;
  label: string;
  items: ArchItem[];
};

export const PATH: Array<{ title: string; sub: string; highlight?: boolean }> = [
  { title: "Capture", sub: "hooks, no filing" },
  { title: "Your Node", sub: "seat scoped", highlight: true },
  { title: "Promotion", sub: "scan · review · approve" },
  { title: "Central", sub: "shared" },
  { title: "Read", sub: "MCP, CLI, web" },
];

export const LAYERS: ArchLayer[] = [
  {
    id: "clients",
    label: "Clients",
    items: [
      {
        id: "agents",
        label: "AI agents / IDEs",
        sub: "MCP",
        detail:
          "Claude Code, Cursor, Codex, or an agent you wrote. The client speaks hosted MCP with a seat-bound bearer token. It gets the same tools and the same read scope as the seat that minted the token: your Node plus Central, never another seat. A search, a share-session, or a mesh read all start here, then enter Hosted MCP.",
      },
      {
        id: "cli",
        label: "citadel CLI",
        sub: "search · onboard · promote",
        detail:
          "The terminal route. You onboard a seat token, search, ingest, and promote from the command line. citadel capture summarises an Approved Capture Root (git metadata and README, not raw files) and POSTs it to your Node. Same token, same scope as MCP. Next hop is HTTPS REST.",
      },
      {
        id: "web",
        label: "Web dashboard",
        sub: "signed in",
        detail:
          "The signed-in workspace: search, the knowledge graph, connected sources, and the promotion queue. Public pages such as /info hydrate aggregates from /api/state and never see vault content. Signed-in calls go through HTTPS REST with the seat token.",
      },
      {
        id: "hooks",
        label: "Capture hooks",
        sub: "SessionEnd · git pre-push",
        detail:
          "A git pre-push hook and a SessionEnd hook write into your Node while you work. They fail silently, so a vault problem never blocks a push or a session close. Both refuse a non-HTTPS Node URL (loopback is the exception), refuse redirects so the bearer token is not resent, and omit the dataset field so the write lands on your Node. Next hop is HTTPS.",
      },
    ],
  },
  {
    id: "app",
    label: "FastAPI",
    items: [
      {
        id: "mcp",
        label: "Hosted MCP",
        sub: "/mcp/",
        detail:
          "Streamable HTTP at /mcp/. The same tools the CLI speaks, behind a bearer token, so an agent never gets a wider read than the seat that minted it. Search, mesh, share-session, and promotion tools mount here and call the FastAPI routes in-process. Next hop is the matching FastAPI route, with the seat token already attached.",
      },
      {
        id: "https",
        label: "HTTPS",
        sub: "no plaintext tokens",
        detail:
          "Anything that carries a seat token refuses plaintext. Capture hooks, citadel capture, and the promotion client require HTTPS except loopback or a Railway private service, and they do not follow redirects. The hosted Node is served on HTTPS at the edge. This is the constraint on the wire, not a separate process.",
      },
      {
        id: "rest",
        label: "REST + public pages",
        sub: "/ingest · /search · /api/*",
        detail:
          "HTTP ingest and search, plus the rest of /api/*. Health endpoints and /api/state stay unauthenticated. Everything that reads memory needs a seat token. CLI, web, and hooks enter here. Authenticated routes go to search, ingest, mesh, share-session, or promotion. /api/state is public and skips the seat gate.",
      },
      {
        id: "auth",
        label: "Seat-bound token",
        sub: "reader · writer · admin",
        detail:
          "Tokens are minted per seat (ctdl_ prefix) and SHA-256 hashed at rest. A reader can search. A writer can ingest, volunteer a share-session, and promote. Admin is operations. The token is the seat: search is scoped to that Node plus Central, and another seat's content returns 404 rather than 403, so there is no existence oracle.",
      },
      {
        id: "read",
        label: "search",
        sub: "/search · citadel_search",
        detail:
          "One query reads your Node and Central together and tells you which one answered. Seat presence is universal; content is caller-scoped. Another seat's content is never in the result, and drilling into it returns 404 rather than 403. The recall path hits Qdrant for embeddings, Ladybug for graph expansion, and SQLite for rows, then applies the visibility filter. Agents reach this through Hosted MCP. CLI and web reach it through REST.",
      },
      {
        id: "write",
        label: "ingest",
        sub: "/ingest · citadel_ingest",
        detail:
          "Seat-scoped writes land on the owning Node. Untagged writes to Central are rejected. Every write path (HTTP, MCP, hooks, capture) runs the seat write-policy guard and a secret scan. Accepted bytes go to the learning process, which commits them to the lifecycle ledger. This is the Capture step on the PATH: hooks and citadel capture enter here.",
      },
      {
        id: "mesh",
        label: "mesh",
        sub: "/api/mesh · citadel_get_mesh",
        detail:
          "The graph API and the dashboard mesh read the same stores the search path uses. Each dataset has its own Ladybug graph; an org-wide read merges those stores. Seat presence is universal; content is caller-scoped. Next hop is the memory engine, which talks to Ladybug and Qdrant.",
      },
      {
        id: "share",
        label: "share-session",
        sub: "/api/share-session",
        detail:
          "A volunteered Shared Session Trace. The MCP tool citadel_share_session posts here after explicit user approval. Writer seat only. The payload is secret-scanned, then dual-written: light into your Node, shared into the session-traces dataset. Traces stay reference-only. They do not promote to Central and they do not feed the improve loop. Next hop is the learning process.",
      },
      {
        id: "state",
        label: "/api/state",
        sub: "public · no secrets",
        detail:
          "Public snapshot for the /info page. Safe aggregates only: no vault content, no per-seat data, no tokens, no graph dumps. A sync hiccup degrades to empty, never a 500. Unauthenticated on purpose. The signed-in dashboard does not use this for memory reads.",
      },
    ],
  },
  {
    id: "core",
    label: "Core",
    items: [
      {
        id: "learn",
        label: "Learning process",
        sub: "filter · chunk · embed",
        detail:
          "Accepted content is filtered, chunked, embedded, and projected. A document counts as searchable in a backend only when that backend's receipt says so. Ingest, share-session, and the evolve pass all enter here, then the lifecycle ledger records the acceptance. The memory engine runs the embed and graph work. This is not a separate service: it runs inside the FastAPI process.",
      },
      {
        id: "promote",
        label: "Promotion",
        sub: "scan · review · approve",
        detail:
          "The only seat-to-Central path. A candidate is secret-scanned and reviewed, then a person approves it from the dashboard, MCP, or the CLI. The evolve pass can enqueue candidates; it does not copy your Node on its own. Promotion is opt-in on the server. Approved bytes dual-write into Central. This is the Promotion step on the PATH.",
      },
      {
        id: "evolve",
        label: "Evolve scheduler",
        sub: "evolve pass",
        detail:
          "A scheduled pass inside the web service, off by default, interval set by the operator. Stages run in order: GitHub sync, repo content, self-improve, promotion, Linear sync, then a verified cognify on the same process. This is not a cron phrase on the diagram: the default lives in config and operators change it. Outputs land in Central (org sources) or your Node (seat-scoped Linear mirrors).",
      },
      {
        id: "cognee",
        label: "Memory engine",
        sub: "memory engine",
        detail:
          "Citadel handles embeddings and graph operations through this engine. Storage, access, sync, and the UI stay Citadel's. add is the fast write (no graph). cognify projects into Qdrant and Ladybug. Search and mesh both read through this client. The lifecycle ledger is what Citadel believes; the engine is how a backend is filled.",
      },
      {
        id: "access",
        label: "Access store",
        sub: "seats · tokens · audit",
        detail:
          "Seats, hashed tokens, roles, capture policy, and the promotion queue live in a JSON store outside the memory engine, so a graph rebuild cannot rewrite who is allowed to read. The seat-bound token at the edge is minted and checked against this store.",
      },
    ],
  },
  {
    id: "storage",
    label: "Storage",
    items: [
      {
        id: "node",
        label: "Your Node",
        sub: "personal by default",
        highlight: true,
        detail:
          "Your own memory, one dataset per seat. Capture hooks, citadel capture, ingest, and the Node copy of a share-session land here first. No other seat can read it. Linear issues assigned to you can mirror here. Promotion is the only way a note leaves this dataset for Central. Physically the bytes live in SQLite, Qdrant, and Ladybug, keyed by this dataset.",
      },
      {
        id: "central",
        label: "Central",
        sub: "org, curated",
        detail:
          "The organization's shared memory. It only ever holds what was promoted into it, plus what the evolve pass ingested from GitHub, repo content, and org-visible Linear. That is what keeps it small enough to trust. MCP tokens stay read-only on Central. Physically the same three stores, different dataset.",
      },
      {
        id: "ledger",
        label: "Lifecycle ledger",
        sub: "durable receipts",
        detail:
          "Ingest is an acceptance: source bytes, the revision head, projection jobs, and per-backend receipts commit in one SQLite transaction. A document is searchable in a backend only when that backend's receipt says so. Relational, vector, and graph are the three required backends (SQLite · Qdrant · Ladybug). Recovery replays failed jobs from this ledger, not from the internal engine.",
      },
      {
        id: "sqlite",
        label: "SQLite",
        sub: "relational · receipts",
        detail:
          "The live relational store at head (DB_PROVIDER=sqlite in kb/lite_runtime.py). Rows, dataset membership, and the lifecycle ledger live here. Not Postgres. The three live stores together are SQLite · Qdrant · Ladybug. Search uses these rows after the vector and graph recall, under the same seat scope.",
      },
      {
        id: "qdrant",
        label: "Qdrant",
        sub: "BAAI/bge-small-en-v1.5",
        detail:
          "The live vector store: a separate Qdrant service (VECTOR_DB_PROVIDER=qdrant). Embeddings come from BAAI/bge-small-en-v1.5, baked into the image, so nothing is downloaded at boot. citadel_search starts here, then expands through Ladybug and filters through SQLite.",
      },
      {
        id: "ladybug",
        label: "Ladybug",
        sub: "knowledge graph",
        detail:
          "The live graph store (GRAPH_DATABASE_PROVIDER=ladybug). One Ladybug store per dataset; an org-wide mesh read merges the per-dataset stores. Mesh and graph expansion on search read here. Not Kuzu, not pgvector.",
      },
    ],
  },
  {
    id: "sync",
    label: "Sync sources",
    items: [
      {
        id: "github",
        label: "GitHub org",
        sub: "evolve pass",
        detail:
          "The organization's repositories sync as one stage of the evolve pass. Commits, pull requests, and issues land as org knowledge in Central, not as yours. This is a scheduled pull from the web service, not a GitHub App and not a webhook (the webhook flag is off by default).",
      },
      {
        id: "repo",
        label: "Repo content",
        sub: "evolve pass",
        detail:
          "Readmes, architecture decision records, and docs from org repositories are ingested as content, not just as filenames. Same evolve pass as GitHub metadata, landing in Central.",
      },
      {
        id: "linear",
        label: "Linear",
        sub: "seat mirror · Central",
        detail:
          "Linear issues assigned to you mirror into your Node. Org-visible issues go to Central during the evolve pass. Your own work stays yours; the org sees what was already shared.",
      },
      {
        id: "obsidian",
        label: "Obsidian",
        sub: "push · pull",
        detail:
          "A linked Obsidian vault mirrors into your Node. Push and pull are both implemented, so markdown notes you already keep are searchable alongside capture. Vaults are seat-owned: another seat addressing your vault by id gets 404, not 403.",
      },
    ],
  },
];

const KIND: Record<string, string> = {
  agents: "source",
  cli: "source",
  hooks: "source",
  web: "source",
  mcp: "source",
  https: "gate",
  rest: "source",
  auth: "gate",
  read: "read",
  write: "source",
  mesh: "source",
  share: "source",
  state: "source",
  learn: "store",
  promote: "gate",
  cognee: "store",
  evolve: "store",
  access: "store",
  node: "store-node",
  central: "store-central",
  ledger: "store",
  sqlite: "store",
  qdrant: "store",
  ladybug: "store",
  github: "source",
  repo: "source",
  linear: "source",
  obsidian: "source",
};

const ITEM = new Map(
  LAYERS.flatMap((layer) => layer.items.map((item) => [item.id, { item, layer }]))
);

/* Hand-placed. Layer bands are parents; x/y on stages are relative to the band.
   Five columns, 188px nodes, 44px gutters, 136px left gutter for the band label. */
const X = [136, 368, 600, 832, 1064] as const;
const Y = 44;
const Y2 = 162;

export type FlowBand = {
  id: string;
  label: string;
  caption?: string;
  x: number;
  y: number;
  w: number;
  h: number;
};

export const FLOW_BANDS: FlowBand[] = [
  { id: "band-clients", label: "Clients", x: 0, y: 0, w: 1284, h: 168 },
  { id: "band-app", label: "FastAPI", x: 0, y: 220, w: 1284, h: 292 },
  { id: "band-core", label: "Core", x: 0, y: 564, w: 1284, h: 168 },
  {
    id: "band-storage",
    label: "Storage",
    caption: "SQLite · Qdrant · Ladybug",
    x: 0,
    y: 784,
    w: 1284,
    h: 292,
  },
  { id: "band-sync", label: "Sync sources", x: 0, y: 1128, w: 1284, h: 168 },
];

const PLACE: Array<[string, string, number, number]> = [
  ["agents", "band-clients", X[0], Y],
  ["cli", "band-clients", X[1], Y],
  ["web", "band-clients", X[2], Y],
  ["hooks", "band-clients", X[3], Y],

  ["mcp", "band-app", X[0], Y],
  ["https", "band-app", X[1], Y],
  ["rest", "band-app", X[2], Y],
  ["auth", "band-app", X[3], Y],
  ["read", "band-app", X[0], Y2],
  ["write", "band-app", X[1], Y2],
  ["mesh", "band-app", X[2], Y2],
  ["share", "band-app", X[3], Y2],
  ["state", "band-app", X[4], Y2],

  ["learn", "band-core", X[0], Y],
  ["promote", "band-core", X[1], Y],
  ["evolve", "band-core", X[2], Y],
  ["cognee", "band-core", X[3], Y],
  ["access", "band-core", X[4], Y],

  ["node", "band-storage", X[0], Y],
  ["central", "band-storage", X[2], Y],
  ["ledger", "band-storage", X[4], Y],
  ["sqlite", "band-storage", X[0], Y2],
  ["qdrant", "band-storage", X[2], Y2],
  ["ladybug", "band-storage", X[4], Y2],

  ["github", "band-sync", X[0], Y],
  ["repo", "band-sync", X[1], Y],
  ["linear", "band-sync", X[2], Y],
  ["obsidian", "band-sync", X[3], Y],
];

export type FlowStage = {
  id: string;
  parent: string;
  x: number;
  y: number;
  kind: string;
  label: string;
  sub: string;
  detail: string;
  layer: string;
  highlight?: boolean;
};

export const FLOW_STAGES: FlowStage[] = PLACE.map(([id, parent, x, y]) => {
  const found = ITEM.get(id);
  if (!found) {
    throw new Error(`pipeline-data: unknown stage id ${id}`);
  }
  return {
    id,
    parent,
    x,
    y,
    kind: KIND[id] ?? "source",
    label: found.item.label,
    sub: found.item.sub,
    detail: found.item.detail,
    layer: found.layer.label,
    highlight: found.item.highlight,
  };
});

function absPos(id: string): { x: number; y: number } {
  const stage = FLOW_STAGES.find((item) => item.id === id);
  const band = FLOW_BANDS.find((item) => item.id === stage?.parent);
  if (!stage || !band) return { x: 0, y: 0 };
  return { x: band.x + stage.x, y: band.y + stage.y };
}

function handlesFor(source: string, target: string): { sourceHandle: string; targetHandle: string } {
  const from = absPos(source);
  const to = absPos(target);
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  if (Math.abs(dy) < 50) {
    return dx >= 0
      ? { sourceHandle: "rout", targetHandle: "lin" }
      : { sourceHandle: "lout", targetHandle: "rin" };
  }
  if (dy > 0) {
    return { sourceHandle: "bout", targetHandle: "tin" };
  }
  return { sourceHandle: "tout", targetHandle: "bin" };
}

/* Data-flow direction: producer to consumer. Hover walks both ways, so a
   search node lights the stores that feed it and the clients that call it. */
export const LINKS: Array<[string, string, string]> = [
  ["agents", "mcp", "MCP"],
  ["cli", "https", ""],
  ["web", "rest", ""],
  ["web", "state", "public"],
  ["hooks", "https", "capture"],
  ["https", "rest", ""],

  ["mcp", "read", "search"],
  ["mcp", "share", ""],
  ["mcp", "mesh", ""],
  ["mcp", "promote", ""],
  ["rest", "read", ""],
  ["rest", "write", "ingest"],
  ["rest", "mesh", ""],
  ["rest", "share", ""],
  ["rest", "promote", ""],
  ["auth", "read", "scope"],
  ["auth", "write", ""],
  ["access", "auth", "seats"],

  ["write", "learn", ""],
  ["write", "node", ""],
  ["share", "learn", "dual-write"],
  ["share", "node", ""],
  ["mesh", "cognee", ""],
  ["promote", "central", "approved"],

  ["learn", "ledger", "accept"],
  ["learn", "cognee", ""],
  ["evolve", "learn", "cognify"],
  ["evolve", "central", ""],
  ["evolve", "promote", ""],
  ["cognee", "qdrant", "embed"],
  ["cognee", "ladybug", "graph"],
  ["cognee", "sqlite", ""],

  ["node", "promote", ""],
  ["node", "ledger", ""],
  ["central", "ledger", ""],
  ["ledger", "sqlite", "project"],
  ["ledger", "qdrant", ""],
  ["ledger", "ladybug", ""],
  ["sqlite", "read", ""],
  ["qdrant", "read", ""],
  ["ladybug", "read", ""],

  ["github", "evolve", "evolve"],
  ["repo", "evolve", ""],
  ["linear", "evolve", ""],
  ["linear", "node", "mirror"],
  ["obsidian", "node", ""],
];

export type FlowLink = {
  source: string;
  target: string;
  label: string;
  sourceHandle: string;
  targetHandle: string;
};

export const FLOW_LINKS: FlowLink[] = LINKS.map(([source, target, label]) => ({
  source,
  target,
  label,
  ...handlesFor(source, target),
}));
