/* The architecture diagram's content: columns, stages, and links.
 *
 * One module, two renderings. pipeline-flow.tsx draws it with React Flow on
 * viewports wide enough to fit the topology; pipeline-diagram.tsx renders the
 * same rows as stacked lanes in the exported markup, which is what phones
 * read. Keeping the rows here keeps the two from drifting apart.
 *
 * Every stage says something the repo can prove at head: the stores are the
 * ones kb/lite_runtime.py configures, the evolve stage order is the one
 * scripts/run_railway.py runs, the lifecycle wording is kb/lifecycle.py's.
 * No stage carries a cadence figure, because the evolve interval is operator
 * configuration rather than code.
 */

export const COL = {
  src: 0,
  node: 300,
  gate: 600,
  central: 900,
  store: 1200,
  read: 1500,
  out: 1800,
};

/* Column captions. Rendered as nodes so they pan and zoom with the diagram
   rather than floating over it.

   They deliberately do not repeat the label of the node beneath them. "Your
   Node" and "Central" are the two nodes that carry the argument; a caption
   saying the same words directly above one reads as a duplicate, not a lane. */
export const HEADERS: Array<[string, number, string]> = [
  ["h-src", COL.src, "Sources"],
  ["h-node", COL.node, "Personal"],
  ["h-gate", COL.gate, "Promotion gate"],
  ["h-central", COL.central, "Organization"],
  ["h-store", COL.store, "Storage"],
  ["h-read", COL.read, "Read scope"],
  ["h-out", COL.out, "Where you read it"],
];

export type StageRow = [string, number, number, string, string, string, string];

export const STAGES: StageRow[] = [
  ["src-prepush", COL.src, 0, "source", "git pre-push", "capture",
    "A pre-push hook sends the commits you are about to push into your Node. It fails silently, so a vault problem never blocks a push."],
  ["src-session", COL.src, 66, "source", "SessionEnd hook", "capture",
    "Your coding agent writes each finished session to your Node when it ends. Nothing to file, nothing to remember to save."],
  ["src-capture", COL.src, 132, "source", "citadel capture", "manual",
    "The deliberate route. One command puts a note, a file, or a decision into your Node when you want it there on purpose."],
  ["src-obsidian", COL.src, 198, "source", "Obsidian sync", "mirror",
    "A linked Obsidian vault mirrors into your Node, so markdown notes you already keep are searchable alongside everything else."],

  ["src-github", COL.src, 320, "source", "GitHub org", "evolve pass",
    "The organization's repositories sync as one stage of the evolve pass, which runs inside the web service: GitHub sync, repo content, self-improve, promotion, Linear sync, then a verified cognify. Commits, pull requests, and issues land as org knowledge rather than as yours."],
  ["src-linear", COL.src, 386, "source", "Linear workspace", "evolve pass",
    "Linear issues mirror in seat scoped, so your own work stays yours and the org sees what was already shared."],
  ["src-repo", COL.src, 452, "source", "Repo content", "evolve pass",
    "Readmes, architecture decision records, and docs from org repositories are ingested as content, not just as filenames."],

  ["node", COL.node, 99, "store-node", "Your Node", "personal by default",
    "Your own memory, one per seat. Everything you capture lands here first, and no other seat can read it."],

  ["gate-scan", COL.gate, 33, "gate", "Secret scan", "1",
    "Every promotion is scanned first. A candidate carrying a key or a token is rejected before a person ever sees it."],
  ["gate-review", COL.gate, 99, "gate", "Automated review", "2",
    "A review pass summarises the candidate and checks it against what Central already holds, so promotion does not duplicate or contradict."],
  ["gate-approve", COL.gate, 165, "gate", "Human approval", "3",
    "Known work promotes automatically once it passes. Anything new waits for a person to approve it, from the dashboard, MCP, or the CLI."],

  ["central", COL.central, 320, "store-central", "Central", "org, curated",
    "The organization's shared memory. It only ever holds what was promoted into it, which is what keeps it small enough to trust."],

  ["store-lifecycle", COL.store, 150, "store", "Lifecycle ledger", "durable receipts",
    "Ingest is an acceptance: source bytes, the revision head, projection jobs, and per-backend receipts commit in one transaction. A document counts as searchable in a backend only when that backend's receipt says so."],
  ["store-backends", COL.store, 260, "store", "Three stores", "SQLite · Qdrant · Ladybug",
    "Rows and receipts live in SQLite, embeddings in a separate Qdrant service, and the knowledge graph in one Ladybug store per dataset; an org-wide read merges the per-dataset stores. Embeddings come from BAAI/bge-small-en-v1.5, baked into the image, so nothing is downloaded at boot."],

  ["read", COL.read, 210, "read", "Caller-scoped read", "your Node plus Central",
    "One query reads your Node and Central together and tells you which one answered. Another seat's content is never in the result, and drilling into it returns 404 rather than 403, so there is no existence oracle."],

  ["out-cli", COL.out, 110, "out", "citadel search", "CLI",
    "The terminal route. You search from the command line under your own seat token."],
  ["out-mcp", COL.out, 176, "out", "MCP clients", "agents",
    "Claude Code, Cursor, or an agent you wrote yourself, over stateless MCP with a bearer token, under the same seat and the same read scope you have."],
  ["out-web", COL.out, 242, "out", "Web workspace", "signed in",
    "The signed-in dashboard: search, the knowledge graph, connected sources, and the promotion queue."],

  ["audit", COL.out, 360, "audit", "Audit log", "every call",
    "Every read and every write is recorded, including which tool asked and under which seat."],
];

export const LINKS: Array<[string, string, string]> = [
  ["src-prepush", "node", "capture"],
  ["src-session", "node", "capture"],
  ["src-capture", "node", "capture"],
  ["src-obsidian", "node", "capture"],

  ["node", "gate-scan", "promote"],
  ["gate-scan", "gate-review", ""],
  ["gate-review", "gate-approve", ""],
  ["gate-approve", "central", "approved"],

  ["src-github", "central", "evolve"],
  ["src-linear", "central", "evolve"],
  ["src-repo", "central", "evolve"],

  ["node", "store-lifecycle", "ingest"],
  ["central", "store-lifecycle", "ingest"],
  ["store-lifecycle", "store-backends", "project"],
  ["store-backends", "read", ""],

  ["read", "out-cli", ""],
  ["read", "out-mcp", ""],
  ["read", "out-web", ""],

  ["out-cli", "audit", ""],
  ["out-mcp", "audit", ""],
  ["out-web", "audit", ""],
];
