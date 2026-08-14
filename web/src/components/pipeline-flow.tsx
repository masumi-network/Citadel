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

import { HEADERS, LINKS, STAGES } from "@/components/pipeline-data";

import "@xyflow/react/dist/style.css";

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
