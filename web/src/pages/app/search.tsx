import { useEffect, useState } from "react";

import {
  AppShell,
  Empty,
  MUTED,
  PANEL,
  PANEL_HEAD,
  PANEL_NOTE,
  PANEL_TITLE,
  VIEW_H1,
} from "@/components/app/app-shell";
import { FIELD_INPUT } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";
import {
  SEARCH_SOURCE_OPTIONS,
  searchScopeDatasets,
  useSession,
  type SearchScope,
  type SearchSource,
} from "@/lib/dashboard";

type SectionKey = "central" | "session_traces" | "node";

type SearchProvenance = {
  title?: string | null;
  source?: string | null;
  path?: string | null;
  session_id?: string | null;
  source_url?: string | null;
};

type SearchEnvelope = {
  rank?: number | null;
  dataset?: string | null;
  trust?: string | null;
  trust_tier?: string | null;
  provenance?: SearchProvenance | null;
  content_sha256?: string | null;
  retrieval?: { document_drilldown_available?: boolean } | null;
  document_endpoint?: string | null;
  result_id?: string | null;
};

type SearchHit = {
  id?: string | number | null;
  document_id?: string | number | null;
  title?: unknown;
  name?: unknown;
  body?: unknown;
  content?: unknown;
  text?: unknown;
  summary?: unknown;
  answer?: unknown;
  result?: unknown;
  source?: unknown;
  dataset?: unknown;
  score?: unknown;
  _citadel?: SearchEnvelope | null;
  [key: string]: unknown;
};

type SearchResponse = {
  results?: SearchHit[];
  sections?: Partial<Record<SectionKey, SearchHit[]>>;
  note?: string;
  warnings?: string[];
  known_datasets?: string[];
  timed_out?: boolean;
  truncated?: boolean;
};

type SourceDocument = {
  title?: unknown;
  path?: unknown;
  id?: unknown;
  body?: unknown;
  content?: unknown;
  source?: unknown;
  source_type?: unknown;
  dataset?: unknown;
  normalized_path?: unknown;
  current_rev?: unknown;
  rev?: unknown;
  updated_at?: unknown;
  metadata?: { checked_at?: unknown; digest_at?: unknown } | null;
};

type DocumentResponse = { document?: SourceDocument };

type PreviewState = {
  loading: boolean;
  error: string | null;
  document: SourceDocument | null;
};

type SourceFilter = "all" | SearchSource;

type SearchRequest = {
  query: string;
  top_k: number;
  dataset?: string;
  source?: SearchSource;
};

const FILTER_BUTTON =
  "cursor-pointer border px-3 py-1.5 text-[12.5px] font-medium transition-[background-color,border-color,color] duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

function filterButtonClass(active: boolean): string {
  return active
    ? `${FILTER_BUTTON} border-accent bg-accent-soft text-accent-ink`
    : `${FILTER_BUTTON} border-border-2 bg-surface text-ink-2 hover:border-accent hover:text-accent-ink`;
}

function buildSearchRequest(
  query: string,
  dataset: string | null,
  source: SourceFilter,
): SearchRequest {
  const request: SearchRequest = { query, top_k: 10 };
  if (dataset) request.dataset = dataset;
  if (source !== "all") request.source = source;
  return request;
}

const SECTION_ORDER: Array<{ key: SectionKey; label: string }> = [
  { key: "central", label: "Central" },
  { key: "session_traces", label: "Session traces (reference-only, verify before acting)" },
  { key: "node", label: "Node" },
];

function valueText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return "";
}

function compactText(value: string, maxLength = 1400): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length <= maxLength
    ? normalized
    : `${normalized.slice(0, maxLength - 3)}...`;
}

function resultKey(hit: SearchHit): string {
  const envelope = hit._citadel;
  return (
    valueText(hit.id) ||
    valueText(hit.document_id) ||
    valueText(envelope?.result_id) ||
    valueText(hit)
  );
}

function resultTitle(hit: SearchHit, index: number): string {
  const provenance = hit._citadel?.provenance;
  return (
    valueText(provenance?.title) ||
    valueText(hit.title) ||
    valueText(hit.name) ||
    valueText(hit.id) ||
    `Result ${index + 1}`
  );
}

function resultBody(hit: SearchHit): string {
  for (const candidate of [
    hit.content,
    hit.body,
    hit.text,
    hit.summary,
    hit.answer,
    hit.result,
  ]) {
    const text = valueText(candidate);
    if (text) return compactText(text);
  }
  return "No passage text returned.";
}

function safeDocumentEndpoint(hit: SearchHit): string | null {
  const envelope = hit._citadel;
  const endpoint = envelope?.document_endpoint;
  if (
    envelope?.retrieval?.document_drilldown_available === true &&
    typeof endpoint === "string" &&
    /^\/api\/documents\/[A-Za-z0-9][A-Za-z0-9:._-]*$/.test(endpoint)
  ) {
    return endpoint;
  }
  return null;
}

function safeExternalUrl(value: string | null | undefined): string | null {
  if (!value?.trim()) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

function resultGroups(response: SearchResponse): Array<{
  key: string;
  label: string;
  hits: SearchHit[];
}> {
  const results = Array.isArray(response.results) ? response.results : [];
  const groups: Array<{ key: string; label: string; hits: SearchHit[] }> = [];
  const placed = new Set<string>();

  for (const section of SECTION_ORDER) {
    const hits = Array.isArray(response.sections?.[section.key])
      ? response.sections[section.key] ?? []
      : [];
    if (!hits.length) continue;
    hits.forEach((hit) => placed.add(resultKey(hit)));
    groups.push({ key: section.key, label: section.label, hits });
  }

  const rest = results.filter((hit) => !placed.has(resultKey(hit)));
  if (rest.length || !groups.length) {
    groups.push({ key: "other", label: "Results", hits: rest.length ? rest : results });
  }
  return groups;
}

function documentRows(document: SourceDocument): Array<[string, string]> {
  const metadata = document.metadata;
  const rows: Array<[string, string]> = [
    ["Source", valueText(document.source) || valueText(document.source_type)],
    ["Dataset", valueText(document.dataset)],
    ["Path", valueText(document.path) || valueText(document.normalized_path)],
    ["Revision", valueText(document.current_rev) || valueText(document.rev)],
    [
      "Checked",
      valueText(metadata?.checked_at) || valueText(metadata?.digest_at) || valueText(document.updated_at),
    ],
  ];
  return rows.filter(([, value]) => Boolean(value));
}

function ResultCard({
  hit,
  index,
  preview,
  onPreview,
}: {
  hit: SearchHit;
  index: number;
  preview?: PreviewState;
  onPreview: (endpoint: string, hide: boolean) => void;
}) {
  const envelope = hit._citadel;
  const provenance = envelope?.provenance;
  const dataset = valueText(envelope?.dataset) || valueText(hit.dataset);
  const source = valueText(provenance?.source) || valueText(provenance?.path) || dataset;
  const trust = valueText(envelope?.trust) || valueText(envelope?.trust_tier) || "unattested";
  const rank = typeof envelope?.rank === "number" ? envelope.rank : index + 1;
  const score = typeof hit.score === "number" && Number.isFinite(hit.score)
    ? hit.score.toFixed(3)
    : null;
  const sourceUrl = safeExternalUrl(provenance?.source_url);
  const endpoint = safeDocumentEndpoint(hit);

  return (
    <article className="border-t border-border py-5 first:border-t-0 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          <p className="m-0 font-mono text-[11.5px] uppercase tracking-[.06em] text-ink-3">
            #{rank} {source ? `- ${source}` : ""}
          </p>
          <h3 className="mt-1 break-words text-[15px] font-semibold text-ink">
            {resultTitle(hit, index)}
          </h3>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[.04em]">
          <span className="bg-accent-soft px-[11px] py-[5px] text-accent-ink">
            {dataset || "unknown dataset"}
          </span>
          <span className="bg-surface-2 px-[11px] py-[5px] text-ink-2">
            {score ? `score ${score}` : `rank ${rank}`}
          </span>
          <span className="bg-surface-2 px-[11px] py-[5px] text-ink-2">trust: {trust}</span>
        </div>
      </div>

      <p className="m-0 mt-3 whitespace-pre-wrap break-words text-[14.5px] leading-[1.6] text-ink">
        {resultBody(hit)}
      </p>

      <dl className="mt-4 grid grid-cols-[max-content_1fr] gap-x-5 gap-y-1 text-[12.5px] leading-[1.5] text-ink-2 max-[620px]:grid-cols-1 max-[620px]:gap-x-0 max-[620px]:gap-y-0">
        {(
          [
            ["Source", valueText(provenance?.source)],
            ["Path", valueText(provenance?.path)],
            ["Session", valueText(provenance?.session_id)],
            ["Hash", valueText(envelope?.content_sha256).slice(0, 12)],
          ] as Array<[string, string]>
        )
          .filter(([, value]) => Boolean(value))
          .map(([label, value]) => (
            <div key={label} className="contents max-[620px]:block">
              <dt className="font-medium text-ink-3">{label}</dt>
              <dd className="min-w-0 break-words max-[620px]:mb-1">{value}</dd>
            </div>
          ))}
      </dl>

      <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px]">
        <span className="text-ink-3">Citation required before acting.</span>
        {sourceUrl ? (
          <a href={sourceUrl} target="_blank" rel="noreferrer">
            Open source link
          </a>
        ) : provenance?.source_url ? (
          <span className="break-all text-ink-3">{provenance.source_url}</span>
        ) : null}
        {endpoint ? (
          <button
            type="button"
            onClick={() => onPreview(endpoint, Boolean(preview?.document))}
            disabled={preview?.loading}
            className="cursor-pointer border border-border-2 bg-surface px-3 py-1.5 font-medium text-ink transition-[border-color,color,opacity] duration-150 hover:border-accent hover:text-accent-ink disabled:cursor-default disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            {preview?.loading
              ? "Loading source"
              : preview?.document
                ? "Hide source"
                : "Preview source"}
          </button>
        ) : null}
      </div>

      {preview?.error ? (
        <p role="alert" className="mt-3 border border-warn-bg bg-warn-bg px-3 py-2 text-[13px] text-warn">
          Could not load source. {preview.error}
        </p>
      ) : null}
      {preview?.document ? (
        <div className="mt-4 border-t border-border pt-4">
          <div className="flex flex-wrap items-baseline justify-between gap-x-5 gap-y-1">
            <h4 className="m-0 text-[14px] font-semibold text-ink">
              {valueText(preview.document.title) || valueText(preview.document.path) || "Source document"}
            </h4>
            <span className="font-mono text-[11.5px] text-ink-3">Source preview</span>
          </div>
          {documentRows(preview.document).length ? (
            <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-5 gap-y-1 text-[12.5px] leading-[1.5] text-ink-2 max-[620px]:grid-cols-1 max-[620px]:gap-x-0">
              {documentRows(preview.document).map(([label, value]) => (
                <div key={label} className="contents max-[620px]:block">
                  <dt className="font-medium text-ink-3">{label}</dt>
                  <dd className="min-w-0 break-words max-[620px]:mb-1">{value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          <pre className="mt-3 max-h-[420px] overflow-auto whitespace-pre-wrap break-words bg-surface-2 p-3 font-mono text-[12px] leading-[1.55] text-ink-2">
            {compactText(
              valueText(preview.document.body) ||
                valueText(preview.document.content) ||
                "No document body returned.",
              2400
            )}
          </pre>
        </div>
      ) : null}

      <details className="mt-4 border-t border-border pt-3 text-[12.5px] text-ink-2">
        <summary className="cursor-pointer font-medium text-ink">Show raw result</summary>
        <pre className="mt-3 max-h-[360px] overflow-auto whitespace-pre-wrap break-words bg-surface-2 p-3 font-mono text-[11.5px] leading-[1.55]">
          {JSON.stringify(hit, null, 2)}
        </pre>
      </details>
    </article>
  );
}

function LoadingResults() {
  return (
    <div aria-label="Loading search results" className="flex flex-col gap-4">
      {[0, 1, 2].map((item) => (
        <div key={item} className="border-t border-border py-5 first:border-t-0 first:pt-0">
          <div className="h-3 w-32 animate-pulse bg-surface-2" />
          <div className="mt-3 h-4 w-2/3 animate-pulse bg-surface-2" />
          <div className="mt-4 h-16 animate-pulse bg-surface-2" />
        </div>
      ))}
    </div>
  );
}

function SearchError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="border border-warn-bg bg-warn-bg px-4 py-3 text-[13.5px] leading-[1.6] text-warn">
      <p className="m-0">Could not search. {message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 cursor-pointer border border-warn bg-surface px-3 py-1.5 text-[13px] font-medium text-warn transition-[filter] duration-150 hover:brightness-[.98] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        Retry
      </button>
    </div>
  );
}

export default function Search() {
  const session = useSession();
  const [input, setInput] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [scope, setScope] = useState<SearchScope>("all");
  const [source, setSource] = useState<SourceFilter>("all");
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});

  const scopeDatasets = searchScopeDatasets(session.data);
  const scopeOptions: Array<{ value: SearchScope; label: string; dataset: string | null }> = [
    { value: "all", label: "Everything", dataset: null },
    ...(scopeDatasets.central
      ? [{ value: "central" as const, label: "Central only", dataset: scopeDatasets.central }]
      : []),
    ...(scopeDatasets.node
      ? [{ value: "node" as const, label: "My Node only", dataset: scopeDatasets.node }]
      : []),
  ];
  const activeScope = scopeOptions.some((option) => option.value === scope) ? scope : "all";
  const selectedDataset = scopeOptions.find((option) => option.value === activeScope)?.dataset ?? null;

  useEffect(() => {
    const nextQuery = new URLSearchParams(window.location.search).get("q")?.trim() ?? "";
    setInput(nextQuery);
    setQuery(nextQuery);
  }, []);

  useEffect(() => {
    if (!query) {
      setResponse(null);
      setError(null);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setResponse(null);
    setError(null);

    api<SearchResponse>("/search", {
      method: "POST",
      body: JSON.stringify(buildSearchRequest(query, selectedDataset, source)),
      signal: controller.signal,
    })
      .then((payload) => {
        if (active) setResponse(payload);
      })
      .catch((failure) => {
        if (active) setError(errorMessage(failure));
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [query, retry, selectedDataset, source]);

  async function togglePreview(endpoint: string, hide: boolean) {
    if (hide) {
      setPreviews((current) => ({
        ...current,
        [endpoint]: { loading: false, error: null, document: null },
      }));
      return;
    }

    setPreviews((current) => ({
      ...current,
      [endpoint]: { loading: true, error: null, document: null },
    }));
    try {
      const payload = await api<DocumentResponse>(endpoint);
      setPreviews((current) => ({
        ...current,
        [endpoint]: { loading: false, error: null, document: payload.document ?? null },
      }));
    } catch (failure) {
      setPreviews((current) => ({
        ...current,
        [endpoint]: { loading: false, error: errorMessage(failure), document: null },
      }));
    }
  }

  const role = session.data?.role ?? null;
  const hits = response && Array.isArray(response.results) ? response.results : [];
  const groups = response ? resultGroups(response) : [];
  const incomplete = response?.timed_out === true || response?.truncated === true;
  const notices = response
    ? [response.note, ...(response.warnings ?? [])].filter((item): item is string => Boolean(item))
    : [];

  return (
    <AppShell
      title="Search"
      current="/next/app/search"
      role={role}
      seat={session.data?.seat_slug}
    >
      <header className="mb-8 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <h1 className={VIEW_H1}>Search</h1>
        {query ? (
          <span className="max-w-full break-words text-right font-mono text-[12px] text-ink-3">
            {loading ? "Searching" : error ? "Search failed" : `${hits.length} result${hits.length === 1 ? "" : "s"}`}
          </span>
        ) : null}
      </header>

      <form method="get" action="/next/app/search" className="mb-7 flex gap-2.5">
        <label className="sr-only" htmlFor="next-search-query">
          Search the vault
        </label>
        <input
          id="next-search-query"
          className={`${FIELD_INPUT} font-sans text-[15px]`}
          type="search"
          name="q"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Search your Node and Central"
          autoComplete="off"
        />
        <button
          type="submit"
          className="shrink-0 cursor-pointer border border-accent bg-accent px-5 text-sm font-medium text-on-accent transition-[filter] duration-150 hover:brightness-[1.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Search
        </button>
      </form>

      <div className="mb-7 flex flex-wrap gap-x-8 gap-y-5">
        <fieldset className="m-0 min-w-0 border-0 p-0">
          <legend className="mb-2 text-[12px] font-semibold uppercase tracking-[.08em] text-ink-3">
            Scope
          </legend>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Search scope">
            {scopeOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                aria-pressed={activeScope === option.value}
                className={filterButtonClass(activeScope === option.value)}
                onClick={() => setScope(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </fieldset>

        <fieldset className="m-0 min-w-0 border-0 p-0">
          <legend className="mb-2 text-[12px] font-semibold uppercase tracking-[.08em] text-ink-3">
            Source
          </legend>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Search source">
            <button
              type="button"
              aria-pressed={source === "all"}
              className={filterButtonClass(source === "all")}
              onClick={() => setSource("all")}
            >
              All sources
            </button>
            {SEARCH_SOURCE_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                aria-pressed={source === option.value}
                className={filterButtonClass(source === option.value)}
                onClick={() => setSource(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </fieldset>
      </div>

      <section className={PANEL} aria-live="polite" aria-busy={loading}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Results</h2>
          <span className={PANEL_NOTE}>
            {query ? `For "${query}"` : "Central first, then session traces and your Node"}
          </span>
        </div>

        {!query ? <Empty>Enter a query to search the sources available to your seat.</Empty> : null}
        {query && loading ? <LoadingResults /> : null}
        {query && !loading && error ? (
          <SearchError message={error} onRetry={() => setRetry((value) => value + 1)} />
        ) : null}
        {query && !loading && !error && response && incomplete && !hits.length ? (
          <SearchError
            message="Search ended before complete results arrived. Retry or narrow the query."
            onRetry={() => setRetry((value) => value + 1)}
          />
        ) : null}
        {query && !loading && !error && response && !incomplete && !hits.length ? (
          <div>
            <Empty>
              No results for <b className="text-ink">{query}</b>.
            </Empty>
            {notices.map((notice) => (
              <p key={notice} className={`mt-3 ${MUTED}`}>
                {notice}
              </p>
            ))}
            {response.known_datasets?.length ? (
              <p className={`mt-3 ${MUTED}`}>
                Known datasets: {response.known_datasets.join(", ")}
              </p>
            ) : null}
          </div>
        ) : null}
        {query && !loading && !error && response && hits.length ? (
          <div>
            {notices.map((notice) => (
              <p key={notice} className="mb-4 border border-warn-bg bg-warn-bg px-3 py-2 text-[13px] leading-[1.5] text-warn">
                {notice}
              </p>
            ))}
            {groups.map((group) => (
              <section key={group.key} aria-labelledby={`search-group-${group.key}`}>
                <h3
                  id={`search-group-${group.key}`}
                  className="mb-1 border-t border-border pt-4 text-[12px] font-semibold uppercase tracking-[.08em] text-ink-3 first:border-t-0 first:pt-0"
                >
                  {group.label} - {group.hits.length}
                </h3>
                {group.hits.map((hit, index) => {
                  const endpoint = safeDocumentEndpoint(hit);
                  return (
                    <ResultCard
                      key={`${group.key}-${resultKey(hit)}`}
                      hit={hit}
                      index={index}
                      preview={endpoint ? previews[endpoint] : undefined}
                      onPreview={togglePreview}
                    />
                  );
                })}
              </section>
            ))}
          </div>
        ) : null}
      </section>
    </AppShell>
  );
}
