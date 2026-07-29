/* The interactive pipeline diagram.
 *
 * Ported from web/flow/index.jsx, which is the esbuild entry point for the
 * committed bundle the hand-written / page still loads. That file mounts itself
 * into a container; this one is a component, lazily imported by
 * pipeline-diagram.tsx, which is the whole point of the migration.
 *
 * The topology is fixed and hand placed. There is no layout engine, because the
 * diagram says one specific thing about how capture, promotion and reading
 * relate, and an auto-layout would keep re-deciding that.
 *
 * Node presentation lives in src/styles/globals.css under the React Flow
 * banner, not here, so the diagram picks up the page's light and dark tokens.
 * The only stylesheet this component carries is React Flow's own.
 */
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useState } from "react";

import "@xyflow/react/dist/style.css";

const COL = { src: 0, node: 300, gate: 600, central: 900, read: 1200, out: 1500 };

/* Column captions. Rendered as nodes so they pan and zoom with the diagram
   rather than floating over it.

   They deliberately do not repeat the label of the node beneath them. "Your
   Node" and "Central" are the two nodes that carry the argument; a caption
   saying the same words directly above one reads as a duplicate, not a lane. */
const HEADERS: Array<[string, number, string]> = [
  ["h-src", COL.src, "Sources"],
  ["h-node", COL.node, "Personal"],
  ["h-gate", COL.gate, "Promotion gate"],
  ["h-central", COL.central, "Organization"],
  ["h-read", COL.read, "Read scope"],
  ["h-out", COL.out, "Where you read it"],
];

type StageRow = [string, number, number, string, string, string, string];

const STAGES: StageRow[] = [
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

const LINKS: Array<[string, string, string]> = [
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
  const up = new Map<string, string[]>();
  const down = new Map<string, string[]>();
  for (const [from, to] of LINKS) {
    if (!down.has(from)) down.set(from, []);
    if (!up.has(to)) up.set(to, []);
    down.get(from)!.push(to);
    up.get(to)!.push(from);
  }
  return { up, down };
})();

function reach(id: string, direction: Map<string, string[]>): Set<string> {
  const seen = new Set([id]);
  const queue = [id];
  while (queue.length) {
    for (const next of direction.get(queue.shift()!) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        queue.push(next);
      }
    }
  }
  return seen;
}

function pathThrough(id: string): Set<string> {
  const both = reach(id, NEIGHBOURS.up);
  for (const node of reach(id, NEIGHBOURS.down)) both.add(node);
  return both;
}

type StageData = { kind: string; label: string; sub: string; dim: boolean };
type HeaderData = { label: string };
type FlowNode = Node<StageData, "stage"> | Node<HeaderData, "header">;

function StageNode({ data }: NodeProps<Node<StageData, "stage">>) {
  return (
    <div className={`fnode fnode-${data.kind}${data.dim ? " is-dim" : ""}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="fnode-l">{data.label}</span>
      {data.sub ? <span className="fnode-s">{data.sub}</span> : null}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

function HeaderNode({ data }: NodeProps<Node<HeaderData, "header">>) {
  return <div className="fhead">{data.label}</div>;
}

/* React Flow types `nodeTypes` as a map of components over one node type, and
   this diagram has two. The cast is at the registry, so the components
   themselves stay properly typed against their own data. */
const nodeTypes = {
  stage: StageNode,
  header: HeaderNode,
} as unknown as Record<string, React.ComponentType<NodeProps>>;

const DETAIL = new Map(
  STAGES.map(([id, , , kind, label, sub, detail]) => [id, { kind, label, sub, detail }])
);

const INITIAL_NODES: FlowNode[] = [
  ...HEADERS.map(([id, x, label]) => ({
    id,
    type: "header" as const,
    position: { x, y: -64 },
    data: { label },
    draggable: false,
    selectable: false,
    focusable: false,
  })),
  ...STAGES.map(([id, x, y, kind, label, sub]) => ({
    id,
    type: "stage" as const,
    position: { x, y },
    data: { kind, label, sub, dim: false },
    draggable: false,
  })),
];

const INITIAL_EDGES: Edge[] = LINKS.map(([source, target, label]) => ({
  id: `${source}-${target}`,
  source,
  target,
  label: label || undefined,
  type: "smoothstep",
  animated: false,
  className: "fedge",
}));

function matchesMedia(query: string): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(query).matches
  );
}

export default function PipelineFlow({ onReady }: { onReady?: () => void }) {
  const [active, setActive] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // The two animations here are the dashed edge flow and the dim transition.
  // Both are off when the visitor asked for less motion.
  const calm = useMemo(() => matchesMedia("(prefers-reduced-motion: reduce)"), []);

  // Drag-to-pan on a touch screen would swallow the vertical swipe that scrolls
  // the page, and this diagram sits in the middle of a long one. React Flow's
  // filter only screens mouse buttons, never touchstart, so the array form of
  // panOnDrag does not help: panning has to be off outright for coarse
  // pointers. fitView shows the whole graph anyway, and the zoom controls stay.
  const canPan = useMemo(() => matchesMedia("(pointer: fine)"), []);

  // React Flow 12 records each node's measured size by dispatching a change
  // through onNodesChange. Passing `nodes` as a plain prop without that handler
  // leaves every node unmeasured, and an unmeasured node has no handle position,
  // so not one edge is ever drawn. Hence the state hooks rather than a useMemo.
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(INITIAL_NODES);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(INITIAL_EDGES);

  // Tells the page the diagram is really on screen, which is what lets it hide
  // the static spine. A chunk that never loads, or a render that throws, never
  // reaches this, so the spine stays and the reader still gets a correct
  // picture.
  useEffect(() => onReady?.(), [onReady]);

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

  const onNodeMouseEnter = useCallback((_event: unknown, node: FlowNode) => {
    if (DETAIL.has(node.id)) setActive(node.id);
  }, []);
  const onNodeMouseLeave = useCallback(() => setActive(null), []);
  const onNodeClick = useCallback((_event: unknown, node: FlowNode) => {
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
        <aside
          className="absolute bottom-3 right-3 z-[5] w-[310px] max-w-[calc(100%-24px)] border border-accent bg-surface px-[18px] py-4 max-[620px]:left-3 max-[620px]:w-auto"
          aria-live="polite"
        >
          <p className="mb-1.5 font-mono text-[9.5px] font-medium uppercase tracking-[.1em] text-accent-ink">
            {detail.sub}
          </p>
          <h4 className="mb-2 text-[15px] font-semibold tracking-[-.01em]">{detail.label}</h4>
          <p className="text-[13px] leading-[1.55] text-ink-2">{detail.detail}</p>
          <button
            type="button"
            onClick={() => setSelected(null)}
            className="mt-[13px] cursor-pointer border border-border-2 px-[11px] py-[5px] text-[11.5px] font-medium text-ink-2 transition-[color,border-color] duration-150 hover:border-accent hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Close
          </button>
        </aside>
      ) : (
        <p className="pointer-events-none absolute bottom-3 left-3 z-[5] m-0 text-[11.5px] text-ink-3 max-[620px]:hidden">
          Hover a step to follow its path. Select one to read what it does.
        </p>
      )}
    </div>
  );
}
