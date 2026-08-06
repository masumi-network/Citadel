/* The authenticated Knowledge Mesh view.
 *
 * The API already applies the caller's dataset visibility and adds presence
 * hubs. This component only lays out the returned projection; it does not
 * infer relationships or fetch a wider graph from the browser.
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
import { useEffect } from "react";

import "@xyflow/react/dist/style.css";

export type GraphNodePayload = {
  id?: unknown;
  label?: unknown;
  name?: unknown;
  type?: unknown;
  dataset?: unknown;
  trust_tier?: unknown;
  presence?: { documents?: unknown } | null;
};

export type GraphEdgePayload = {
  source?: unknown;
  target?: unknown;
  relationship?: unknown;
};

export type GraphPayload = {
  nodes?: GraphNodePayload[];
  edges?: GraphEdgePayload[];
  fallback?: boolean;
  fallback_reason?: string;
  note?: string;
  total_nodes?: number;
  total_edges?: number;
  truncated?: boolean;
  limit?: number;
};

type KnowledgeNodeData = {
  label: string;
  kind: string;
  dataset: string;
  trust: string;
  presence: string | null;
};

type KnowledgeNode = Node<KnowledgeNodeData, "knowledge">;

function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function kindFor(node: GraphNodePayload): string {
  const type = text(node.type).toLowerCase();
  return type.includes("dataset") ? "dataset" : type.includes("document") ? "document" : "entity";
}

function KnowledgeNodeView({ data }: NodeProps<KnowledgeNode>) {
  return (
    <div className={`knowledge-node knowledge-node-${data.kind}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="knowledge-node-label">{data.label}</span>
      <span className="knowledge-node-kind">{data.kind}</span>
      {data.dataset && data.kind !== "dataset" ? (
        <span className="knowledge-node-meta">{data.dataset}</span>
      ) : null}
      {data.presence ? <span className="knowledge-node-meta">{data.presence}</span> : null}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { knowledge: KnowledgeNodeView };

function graphNodes(payload: GraphPayload): KnowledgeNode[] {
  const source = Array.isArray(payload.nodes) ? payload.nodes : [];
  const columns = Math.max(1, Math.ceil(Math.sqrt(source.length)));
  const seen = new Set<string>();

  return source.flatMap((item, index) => {
    const id = text(item.id);
    if (!id || seen.has(id)) return [];
    seen.add(id);
    const kind = kindFor(item);
    const label = text(item.label) || text(item.name) || id;
    const presenceCount = text(item.presence?.documents);
    const dataset = text(item.dataset);
    const trust = text(item.trust_tier);
    const column = kind === "dataset" ? 0 : 1 + (index % columns);
    const row = kind === "dataset" ? index : Math.floor(index / columns);
    return [
      {
        id,
        type: "knowledge",
        position: { x: column * 270, y: row * 112 },
        data: {
          label,
          kind,
          dataset,
          trust,
          presence: presenceCount ? `${presenceCount} documents` : trust ? trust : null,
        },
        draggable: false,
      },
    ];
  });
}

function graphEdges(payload: GraphPayload, nodes: KnowledgeNode[]): Edge[] {
  const ids = new Set(nodes.map((node) => node.id));
  const seen = new Set<string>();
  const source = Array.isArray(payload.edges) ? payload.edges : [];

  return source.flatMap((item, index) => {
    const from = text(item.source);
    const to = text(item.target);
    if (!from || !to || !ids.has(from) || !ids.has(to) || from === to) return [];
    const relationship = text(item.relationship) || "related";
    const id = `${from}:${to}:${relationship}:${index}`;
    if (seen.has(id)) return [];
    seen.add(id);
    return [
      {
        id,
        source: from,
        target: to,
        label: relationship,
        type: "smoothstep",
      },
    ];
  });
}

export default function KnowledgeGraph({ payload }: { payload: GraphPayload }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<KnowledgeNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    const nextNodes = graphNodes(payload);
    setNodes(nextNodes);
    setEdges(graphEdges(payload, nextNodes));
  }, [payload, setEdges, setNodes]);

  return (
    <div className="knowledgecanvas" aria-label="Knowledge graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        fitView
        fitViewOptions={{ padding: 0.2, minZoom: 0.35, maxZoom: 1.25 }}
        minZoom={0.25}
        maxZoom={1.5}
        attributionPosition="bottom-left"
      >
        <Background gap={24} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
