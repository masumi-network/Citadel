/* The interactive pipeline diagram on /.
 *
 * Source for kb/static/vendor/flow.js. Nothing imports this at runtime: it is
 * bundled by `npm run build:flow` and the output is committed. See
 * docs/web-bundle.md.
 *
 * The topology is fixed and hand placed. There is no layout engine, because
 * the diagram says one specific thing about how capture, promotion and reading
 * relate, and an auto-layout would keep re-deciding that.
 *
 * Node presentation lives in info.css under the `/* --- / flow --- *\/` banner,
 * not here, so the diagram picks up the page's light and dark tokens. The only
 * stylesheet this bundle carries is React Flow's own.
 */
import { StrictMode, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

const COL = { src: 0, node: 300, gate: 600, central: 900, read: 1200, out: 1500 };

/* Column captions. Rendered as nodes so they pan and zoom with the diagram
   rather than floating over it.

   They deliberately do not repeat the label of the node beneath them. "Your
   Node" and "Central" are the two nodes that carry the argument; a caption
   saying the same words directly above one reads as a duplicate, not a lane. */
const HEADERS = [
  ["h-src", COL.src, "Sources"],
  ["h-node", COL.node, "Personal"],
  ["h-gate", COL.gate, "Promotion gate"],
  ["h-central", COL.central, "Organization"],
  ["h-read", COL.read, "Read scope"],
  ["h-out", COL.out, "Where you read it"],
];

const NODES = [
  ["src-prepush", COL.src, 0, "source", "git pre-push", "capture",
    "A pre-push hook sends the commits you are about to push into your Node. It fails silently, so a vault problem never blocks a push."],
  ["src-session", COL.src, 66, "source", "SessionEnd hook", "capture",
    "Your coding agent writes each finished session to your Node when it ends. Nothing to file, nothing to remember to save."],
  ["src-capture", COL.src, 132, "source", "citadel capture", "manual",
    "The deliberate route. One command puts a note, a file, or a decision into your Node when you want it there on purpose."],
  ["src-obsidian", COL.src, 198, "source", "Obsidian sync", "mirror",
    "A linked Obsidian vault mirrors into your Node, so markdown notes you already keep are searchable alongside everything else."],

  ["src-github", COL.src, 320, "source", "GitHub org", "hourly",
    "The organization's repositories sync on a schedule: commits, pull requests, and issues, held as org knowledge rather than as yours."],
  ["src-linear", COL.src, 386, "source", "Linear workspace", "hourly",
    "Linear issues mirror in seat scoped, so your own work stays yours and the org sees what was already shared."],
  ["src-repo", COL.src, 452, "source", "Repo content", "hourly",
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

  ["read", COL.read, 210, "read", "Caller-scoped read", "your Node plus Central",
    "One query reads your Node and Central together and tells you which one answered. Another seat's content is never in the result, and drilling into it returns 404 rather than 403, so there is no existence oracle."],

  ["out-cli", COL.out, 110, "out", "citadel search", "CLI",
    "The terminal route. You search from the command line under your own seat token."],
  ["out-mcp", COL.out, 176, "out", "MCP clients", "agents",
    "Claude Code, Cursor, or an agent you wrote yourself, over MCP, under the same seat and the same read scope you have."],
  ["out-web", COL.out, 242, "out", "Web workspace", "signed in",
    "The signed-in dashboard: search, the knowledge graph, connected sources, and the promotion queue."],

  ["audit", COL.out, 360, "audit", "Audit log", "every call",
    "Every read and every write is recorded, including which tool asked and under which seat."],
];

const EDGES = [
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

  ["node", "read", ""],
  ["central", "read", ""],

  ["read", "out-cli", ""],
  ["read", "out-mcp", ""],
  ["read", "out-web", ""],

  ["out-cli", "audit", ""],
  ["out-mcp", "audit", ""],
  ["out-web", "audit", ""],
];

/* Adjacency, both directions, so hovering a node can light the whole path it
   sits on rather than only its immediate neighbours. */
const NEIGHBOURS = (() => {
  const up = new Map();
  const down = new Map();
  for (const [from, to] of EDGES) {
    if (!down.has(from)) down.set(from, []);
    if (!up.has(to)) up.set(to, []);
    down.get(from).push(to);
    up.get(to).push(from);
  }
  return { up, down };
})();

function reach(id, direction) {
  const seen = new Set([id]);
  const queue = [id];
  while (queue.length) {
    for (const next of direction.get(queue.shift()) || []) {
      if (!seen.has(next)) {
        seen.add(next);
        queue.push(next);
      }
    }
  }
  return seen;
}

function pathThrough(id) {
  const both = reach(id, NEIGHBOURS.up);
  for (const node of reach(id, NEIGHBOURS.down)) both.add(node);
  return both;
}

function StageNode({ data }) {
  return (
    <div className={`fnode fnode-${data.kind}${data.dim ? " is-dim" : ""}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="fnode-l">{data.label}</span>
      {data.sub ? <span className="fnode-s">{data.sub}</span> : null}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

function HeaderNode({ data }) {
  return <div className="fhead">{data.label}</div>;
}

const nodeTypes = { stage: StageNode, header: HeaderNode };

const DETAIL = new Map(NODES.map(([id, , , kind, label, sub, detail]) => [id, { kind, label, sub, detail }]));

function Flow() {
  const [active, setActive] = useState(null);
  const [selected, setSelected] = useState(null);

  // The two animations here are the dashed edge flow and the dim transition.
  // Both are off when the visitor asked for less motion.
  const media = useCallback((query) => {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia(query).matches
    );
  }, []);

  const calm = useMemo(() => media("(prefers-reduced-motion: reduce)"), [media]);

  // Drag-to-pan on a touch screen would swallow the vertical swipe that scrolls
  // the page, and this diagram sits in the middle of a long one. React Flow's
  // filter only screens mouse buttons, never touchstart, so the array form of
  // panOnDrag does not help: panning has to be off outright for coarse
  // pointers. fitView shows the whole graph anyway, and the zoom controls stay.
  const canPan = useMemo(() => media("(pointer: fine)"), [media]);

  // React Flow 12 records each node's measured size by dispatching a change
  // through onNodesChange. Passing `nodes` as a plain prop without that handler
  // leaves every node unmeasured, and an unmeasured node has no handle position,
  // so not one edge is ever drawn. Hence the state hooks rather than a useMemo.
  const [nodes, setNodes, onNodesChange] = useNodesState(() => [
    ...HEADERS.map(([id, x, label]) => ({
      id,
      type: "header",
      position: { x, y: -64 },
      data: { label },
      draggable: false,
      selectable: false,
      focusable: false,
    })),
    ...NODES.map(([id, x, y, kind, label, sub]) => ({
      id,
      type: "stage",
      position: { x, y },
      data: { kind, label, sub, dim: false },
      draggable: false,
    })),
  ]);

  const [edges, setEdges, onEdgesChange] = useEdgesState(() =>
    EDGES.map(([source, target, label]) => ({
      id: `${source}-${target}`,
      source,
      target,
      label: label || undefined,
      type: "smoothstep",
      animated: false,
      className: "fedge",
    }))
  );

  // Hovering a stage lights the whole path it sits on and dims everything else.
  useEffect(() => {
    const lit = active ? pathThrough(active) : null;
    setNodes((current) =>
      current.map((node) =>
        node.type === "stage"
          ? { ...node, data: { ...node.data, dim: lit ? !lit.has(node.id) : false } }
          : node
      )
    );
    setEdges((current) =>
      current.map((edge) => {
        const on = lit ? lit.has(edge.source) && lit.has(edge.target) : false;
        return {
          ...edge,
          animated: !calm && on,
          className: lit ? (on ? "fedge is-on" : "fedge is-dim") : "fedge",
        };
      })
    );
  }, [active, calm, setNodes, setEdges]);

  const onNodeMouseEnter = useCallback((_event, node) => {
    if (DETAIL.has(node.id)) setActive(node.id);
  }, []);
  const onNodeMouseLeave = useCallback(() => setActive(null), []);
  const onNodeClick = useCallback((_event, node) => {
    if (DETAIL.has(node.id)) setSelected((current) => (current === node.id ? null : node.id));
  }, []);
  const onPaneClick = useCallback(() => setSelected(null), []);

  const detail = selected ? DETAIL.get(selected) : null;

  return (
    <div className="flowcanvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        onNodeMouseEnter={onNodeMouseEnter}
        onNodeMouseLeave={onNodeMouseLeave}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        minZoom={0.25}
        maxZoom={1.4}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        // The diagram sits mid-page, so the wheel must keep scrolling the page.
        // Zoom is on the controls and on pinch.
        zoomOnScroll={false}
        preventScrolling={false}
        panOnDrag={canPan}
        proOptions={{ hideAttribution: false }}
      >
        <Background gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
      {detail ? (
        <aside className="flowpanel" aria-live="polite">
          <p className="flowpanel-k">{detail.sub}</p>
          <h4>{detail.label}</h4>
          <p>{detail.detail}</p>
          <button type="button" className="flowpanel-x" onClick={() => setSelected(null)}>
            Close
          </button>
        </aside>
      ) : (
        <p className="flowhint">Hover a step to follow its path. Select one to read what it does.</p>
      )}
    </div>
  );
}

/* Returns whether the diagram is actually on screen, which is the question
   landing.js is really asking: it hides the static spine on the strength of
   this answer, and a wrong `true` replaces a correct diagram with an empty box.
   `root.render()` alone cannot answer it. It schedules the render rather than
   performing it, so it returns before the container holds anything and before
   a render that throws has thrown, which puts the failure outside the caller's
   try/catch. flushSync forces the first render to completion here, so a throw
   lands in this try and the child count is a real observation. */
export function mount(element) {
  if (!element) return false;
  try {
    const root = createRoot(element);
    flushSync(() => {
      root.render(
        <StrictMode>
          <Flow />
        </StrictMode>
      );
    });
    return element.childElementCount > 0;
  } catch (error) {
    return false;
  }
}
