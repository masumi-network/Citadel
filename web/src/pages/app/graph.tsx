import dynamic from "next/dynamic";
import { useEffect } from "react";

import {
  AppShell,
  Empty,
  LoadError,
  PANEL,
  PANEL_HEAD,
  PANEL_NOTE,
  PANEL_TITLE,
  VIEW_H1,
} from "@/components/app/app-shell";
import { relativeTime, useEndpoint, useSession } from "@/lib/dashboard";
import type { GraphPayload } from "@/components/app/knowledge-graph";

const KnowledgeGraph = dynamic(() => import("@/components/app/knowledge-graph"), { ssr: false });

type ScopedGraphPayload = GraphPayload & {
  visible_nodes?: number;
};

type ProjectionOperation = {
  projection_job_id?: string;
  state?: string;
  job?: { updated_at?: string | null; last_error_code?: string | null };
  receipts?: Array<{ backend?: string; state?: string }>;
};

type ProjectionStatus = {
  enabled?: boolean;
  dataset?: string | null;
  operations?: ProjectionOperation[];
};

export default function Graph() {
  const session = useSession();
  const graph = useEndpoint<ScopedGraphPayload>("/api/mesh/graph?limit=1000");
  const projection = useEndpoint<ProjectionStatus>("/api/mesh/projection-status");
  useEffect(() => {
    const timer = window.setInterval(projection.reload, 5000);
    return () => window.clearInterval(timer);
  }, [projection.reload]);
  const renderedNodeCount = graph.data?.nodes?.length ?? 0;
  const hasScopedCount = typeof graph.data?.visible_nodes === "number";
  const visibleNodeCount = hasScopedCount
    ? graph.data?.visible_nodes ?? 0
    : renderedNodeCount;
  const edgeCount = graph.data?.edges?.length ?? 0;
  const hasPresenceHubs =
    graph.data?.nodes?.some((node) =>
      typeof node.type === "string" && node.type.toLowerCase().includes("dataset"),
    ) ?? false;
  const presenceOnly = hasScopedCount && visibleNodeCount === 0 && hasPresenceHubs;
  const emptyScope = hasScopedCount && visibleNodeCount === 0 && !hasPresenceHubs;
  const emptyGraph = !hasScopedCount && renderedNodeCount === 0;

  return (
    <AppShell
      title="Graph"
      current="/next/app/graph"
      role={session.data?.role ?? null}
      seat={session.data?.seat_slug}
    >
      <header className="mb-8 flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className={VIEW_H1}>Knowledge graph</h1>
          <p className="mt-2 text-[14.5px] text-ink-2">
            Caller-scoped graph projection. Dataset hubs are presence signals, not content access.
          </p>
        </div>
        <button
          type="button"
          onClick={graph.reload}
          className="cursor-pointer border border-border-2 bg-surface px-4 py-2 text-[13px] font-medium text-ink transition-[border-color,color] duration-150 hover:border-accent hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Refresh
        </button>
      </header>

      <section className={`${PANEL} mb-3`}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Mesh projection</h2>
          <span className={PANEL_NOTE}>
            {graph.data?.truncated ? `Showing up to ${graph.data.limit ?? 1000} nodes` : "Current view"}
          </span>
        </div>
        {graph.error ? <LoadError what="the knowledge graph" message={graph.error} /> : null}
        {graph.loading ? <Empty>Loading graph data.</Empty> : null}
        {!graph.loading && !graph.error && graph.data?.fallback ? (
          <p className="border border-warn-bg bg-warn-bg px-4 py-3 text-[13.5px] leading-[1.6] text-warn">
            Graph data is temporarily unavailable. {graph.data.fallback_reason ?? "Try again later."}
          </p>
        ) : null}
        {!graph.loading && !graph.error && presenceOnly ? (
          <p className="border border-border-2 bg-surface-2 px-4 py-3 text-[13.5px] leading-[1.6] text-ink-2">
            Presence-only view. No content nodes are visible for this scope.
          </p>
        ) : null}
        {!graph.loading && !graph.error && (emptyScope || emptyGraph) ? (
          <Empty>
            {emptyScope ? "No content nodes are visible for this scope." : "No graph nodes are visible yet."}
          </Empty>
        ) : null}
        {graph.data?.note ? <p className="mt-3 text-[12.5px] text-ink-3">{graph.data.note}</p> : null}
        {!graph.loading && !graph.error && (renderedNodeCount || hasScopedCount) ? (
          <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-[12.5px] text-ink-2">
            <div>
              <dt className="font-medium text-ink-3">Visible nodes</dt>
              <dd className="font-mono text-ink">{visibleNodeCount}</dd>
            </div>
            <div>
              <dt className="font-medium text-ink-3">Visible edges</dt>
              <dd className="font-mono text-ink">{edgeCount}</dd>
            </div>
            {typeof graph.data?.total_nodes === "number" ? (
              <div>
                <dt className="font-medium text-ink-3">Graph nodes</dt>
                <dd className="font-mono text-ink">{graph.data.total_nodes}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </section>

      <section className={`${PANEL} mb-3`}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Cognee projection</h2>
          <span className={PANEL_NOTE}>{projection.data?.dataset ?? "Current seat"}</span>
        </div>
        {projection.error ? <LoadError what="projection status" message={projection.error} /> : null}
        {!projection.error && projection.data?.enabled === false ? (
          <Empty>Projection status is unavailable for this session.</Empty>
        ) : null}
        {!projection.error && projection.data?.enabled !== false && !projection.data?.operations?.length ? (
          <Empty>No seat projection writes recorded yet.</Empty>
        ) : null}
        {projection.data?.operations?.length ? (
          <ul className="space-y-2">
            {projection.data.operations.map((operation) => (
              <li key={operation.projection_job_id} className="border border-border-2 bg-surface-2 px-3 py-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-ink-2">
                    {operation.projection_job_id}
                  </span>
                  <span className="text-[12px] font-medium text-ink">{operation.state ?? "unknown"}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-3">
                  {operation.job?.updated_at ? <span>{relativeTime(operation.job.updated_at)}</span> : null}
                  {operation.job?.last_error_code ? <span>{operation.job.last_error_code}</span> : null}
                  {operation.receipts?.map((receipt) => (
                    <span key={`${operation.projection_job_id}-${receipt.backend}`}>
                      {receipt.backend ?? "backend"}: {receipt.state ?? "unknown"}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      {graph.data && !graph.error && renderedNodeCount ? <KnowledgeGraph payload={graph.data} /> : null}
    </AppShell>
  );
}
