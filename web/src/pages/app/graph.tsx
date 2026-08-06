import dynamic from "next/dynamic";

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
import { useEndpoint, useSession } from "@/lib/dashboard";
import type { GraphPayload } from "@/components/app/knowledge-graph";

const KnowledgeGraph = dynamic(() => import("@/components/app/knowledge-graph"), { ssr: false });

export default function Graph() {
  const session = useSession();
  const graph = useEndpoint<GraphPayload>("/api/mesh/graph?limit=200");
  const nodeCount = graph.data?.nodes?.length ?? 0;
  const edgeCount = graph.data?.edges?.length ?? 0;

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
            {graph.data?.truncated ? `Showing up to ${graph.data.limit ?? 200} nodes` : "Current view"}
          </span>
        </div>
        {graph.error ? <LoadError what="the knowledge graph" message={graph.error} /> : null}
        {graph.loading ? <Empty>Loading graph data.</Empty> : null}
        {!graph.loading && !graph.error && graph.data?.fallback ? (
          <p className="border border-warn-bg bg-warn-bg px-4 py-3 text-[13.5px] leading-[1.6] text-warn">
            Graph data is temporarily unavailable. {graph.data.fallback_reason ?? "Try again later."}
          </p>
        ) : null}
        {!graph.loading && !graph.error && !nodeCount ? <Empty>No graph nodes are visible yet.</Empty> : null}
        {graph.data?.note ? <p className="mt-3 text-[12.5px] text-ink-3">{graph.data.note}</p> : null}
        {nodeCount ? (
          <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-[12.5px] text-ink-2">
            <div>
              <dt className="font-medium text-ink-3">Visible nodes</dt>
              <dd className="font-mono text-ink">{nodeCount}</dd>
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

      {graph.data && !graph.error && nodeCount ? <KnowledgeGraph payload={graph.data} /> : null}
    </AppShell>
  );
}
