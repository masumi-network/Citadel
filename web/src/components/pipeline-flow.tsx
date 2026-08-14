/* The interactive architecture diagram.
 *
 * Topology is the layered stack from docs/diagrams/citadel-architecture,
 * hand-placed from pipeline-data.ts. React Flow measures nodes through
 * onNodesChange; without that handler no edge is drawn.
 *
 * CSS: @xyflow/react/dist/style.css is loaded in globals.css (Tailwind 4
 * base layer, https://reactflow.dev/learn/customization/theming) and again
 * here so the lazy chunk is self-contained. Node chrome lives under the
 * React Flow banner in globals.css.
 */
import {
  Background,
  Handle,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useState, type WheelEvent } from "react";

import { FLOW_BANDS, FLOW_LINKS, FLOW_STAGES, LINKS } from "@/components/pipeline-data";

import "@xyflow/react/dist/style.css";

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

const BAND_CHILDREN = (() => {
  const map = new Map<string, string[]>();
  for (const stage of FLOW_STAGES) {
    const list = map.get(stage.parent) ?? [];
    list.push(stage.id);
    map.set(stage.parent, list);
  }
  return map;
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
type BandData = { label: string; caption?: string; dim: boolean; on: boolean };
type FlowNode = Node<StageData, "stage"> | Node<BandData, "band">;

function Port({ type, position, id }: { type: "source" | "target"; position: Position; id: string }) {
  return <Handle type={type} position={position} id={id} isConnectable={false} />;
}

function StageNode({ data }: NodeProps<Node<StageData, "stage">>) {
  return (
    <div className={`fnode fnode-${data.kind}${data.dim ? " is-dim" : ""}`}>
      <Port type="target" position={Position.Top} id="tin" />
      <Port type="source" position={Position.Top} id="tout" />
      <Port type="target" position={Position.Right} id="rin" />
      <Port type="source" position={Position.Right} id="rout" />
      <Port type="target" position={Position.Bottom} id="bin" />
      <Port type="source" position={Position.Bottom} id="bout" />
      <Port type="target" position={Position.Left} id="lin" />
      <Port type="source" position={Position.Left} id="lout" />
      <span className="fnode-l">{data.label}</span>
      {data.sub ? <span className="fnode-s">{data.sub}</span> : null}
    </div>
  );
}

function BandNode({ data }: NodeProps<Node<BandData, "band">>) {
  return (
    <div className={`fband${data.dim ? " is-dim" : ""}${data.on ? " is-on" : ""}`}>
      <span className="fband-k">{data.label}</span>
      {data.caption ? <span className="fband-s">{data.caption}</span> : null}
    </div>
  );
}

const nodeTypes = {
  stage: StageNode,
  band: BandNode,
} as unknown as Record<string, React.ComponentType<NodeProps>>;

const DETAIL = new Map(
  FLOW_STAGES.map((stage) => [
    stage.id,
    {
      kind: stage.kind,
      label: stage.label,
      sub: stage.sub,
      detail: stage.detail,
      layer: stage.layer,
    },
  ])
);

const LABEL = new Map(FLOW_STAGES.map((stage) => [stage.id, stage.label]));

const INITIAL_NODES: FlowNode[] = [
  ...FLOW_BANDS.map((band) => ({
    id: band.id,
    type: "band" as const,
    position: { x: band.x, y: band.y },
    data: { label: band.label, caption: band.caption, dim: false, on: false },
    style: { width: band.w, height: band.h },
    width: band.w,
    height: band.h,
    draggable: false,
    selectable: false,
    focusable: false,
    connectable: false,
    zIndex: -1,
  })),
  ...FLOW_STAGES.map((stage) => {
    const band = FLOW_BANDS.find((item) => item.id === stage.parent);
    return {
      id: stage.id,
      type: "stage" as const,
      position: {
        x: (band?.x ?? 0) + stage.x,
        y: (band?.y ?? 0) + stage.y,
      },
      data: { kind: stage.kind, label: stage.label, sub: stage.sub, dim: false },
      draggable: false,
      zIndex: 1,
    };
  }),
];

const INITIAL_EDGES: Edge[] = FLOW_LINKS.map((link) => ({
  id: `${link.source}-${link.target}`,
  source: link.source,
  target: link.target,
  sourceHandle: link.sourceHandle,
  targetHandle: link.targetHandle,
  label: link.label || undefined,
  type: "smoothstep",
  pathOptions: { borderRadius: 0 },
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

function blurFlowPane() {
  const el = document.activeElement;
  if (el instanceof HTMLElement && el.closest(".react-flow")) {
    el.blur();
  }
}

function hopLabels(id: string, side: "up" | "down"): string[] {
  const links =
    side === "down" ? LINKS.filter(([from]) => from === id) : LINKS.filter(([, to]) => to === id);
  const names: string[] = [];
  const seen = new Set<string>();
  for (const link of links) {
    const other = side === "down" ? link[1] : link[0];
    if (seen.has(other)) continue;
    seen.add(other);
    const name = LABEL.get(other);
    if (name) names.push(name);
  }
  return names;
}

export default function PipelineFlow({ onReady }: { onReady?: () => void }) {
  const [active, setActive] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const calm = useMemo(() => matchesMedia("(prefers-reduced-motion: reduce)"), []);
  /* Phone / coarse: pan inside the canvas. Fine pointers already pan; this
     used to be false on coarse so a one-finger swipe scrolled the page. */
  const coarse = useMemo(
    () => matchesMedia("(pointer: coarse)") || matchesMedia("(max-width: 620px)"),
    []
  );
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(INITIAL_NODES);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(INITIAL_EDGES);

  useEffect(() => onReady?.(), [onReady]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setSelected(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const lit = active ? pathThrough(active) : null;
    setNodes((current) =>
      current.map((node) => {
        if (node.type === "band") {
          const childIds = BAND_CHILDREN.get(node.id) ?? [];
          const on = lit ? childIds.some((id) => lit.has(id)) : false;
          return { ...node, data: { ...node.data, dim: false, on } };
        }
        return { ...node, data: { ...node.data, dim: lit ? !lit.has(node.id) : false } };
      })
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
    blurFlowPane();
  }, []);
  const onPaneClick = useCallback(() => {
    setSelected(null);
    blurFlowPane();
  }, []);
  const onWheelCapture = useCallback((event: WheelEvent<HTMLDivElement>) => {
    /* Pinch (ctrl+wheel) may zoom. Plain wheel must reach the page. */
    if (event.ctrlKey) return;
    event.stopPropagation();
  }, []);

  const detail = selected ? DETAIL.get(selected) : null;
  const incoming = selected ? hopLabels(selected, "up") : [];
  const outgoing = selected ? hopLabels(selected, "down") : [];

  return (
    <div className="flowcanvas" onWheelCapture={onWheelCapture}>
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
        fitViewOptions={{ padding: 0.18 }}
        minZoom={0.12}
        maxZoom={2}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        connectOnClick={false}
        zoomOnScroll={false}
        panOnScroll={false}
        preventScrolling={!coarse}
        zoomOnPinch={true}
        panOnDrag={true}
        proOptions={{ hideAttribution: false }}
      >
        <Background gap={22} size={1} />
      </ReactFlow>
      {detail ? (
        <aside
          className="absolute bottom-3 right-3 z-[5] w-[340px] max-w-[calc(100%-24px)] border border-accent bg-surface px-[18px] py-4 max-[620px]:left-3 max-[620px]:w-auto"
          aria-live="polite"
        >
          <p className="mb-1.5 font-mono text-[9.5px] font-medium uppercase tracking-[.1em] text-accent-ink">
            {detail.layer}
            {detail.sub ? ` · ${detail.sub}` : ""}
          </p>
          <h4 className="mb-2 text-[15px] font-semibold tracking-[-.01em]">{detail.label}</h4>
          <p className="text-[13px] leading-[1.55] text-ink-2">{detail.detail}</p>
          {incoming.length ? (
            <p className="mt-3 font-mono text-[10.5px] leading-[1.45] text-ink-3">
              In: {incoming.join(" · ")}
            </p>
          ) : (
            <p className="mt-3 font-mono text-[10.5px] leading-[1.45] text-ink-3">In: entry</p>
          )}
          {outgoing.length ? (
            <p className="font-mono text-[10.5px] leading-[1.45] text-ink-3">
              Out: {outgoing.join(" · ")}
            </p>
          ) : (
            <p className="font-mono text-[10.5px] leading-[1.45] text-ink-3">Out: terminal</p>
          )}
          <button
            type="button"
            onClick={() => setSelected(null)}
            className="mt-[13px] cursor-pointer border border-border-2 px-[11px] py-[5px] text-[11.5px] font-medium text-ink-2 transition-[color,border-color] duration-150 hover:border-accent hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Close
          </button>
        </aside>
      ) : (
        <p className="pointer-events-none absolute bottom-3 left-3 z-[5] m-0 text-[11.5px] text-ink-3">
          Click a box to follow a request. Hover lights its path.
        </p>
      )}
    </div>
  );
}
