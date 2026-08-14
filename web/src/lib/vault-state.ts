/* The public node snapshot, read from `/api/state`.
 *
 * Every public page shows something from it: the health pill in the section
 * index on all four, plus the live tiles and the health strip on /info. The
 * endpoint and its shape are owned by kb/server.py and are not changed here.
 *
 * The fetch is deduplicated at module scope rather than per component, because
 * a page can mount two readers (the pill and the tiles) and the hand-written
 * version this replaces made exactly one request. The cache is per document
 * load, which is the same lifetime the old script had.
 */
import { useEffect, useState } from "react";

export type RepoWeek = { start: string; commits: number };

export type RepoBlock = {
  adrs?: number | null;
  mcp_tools?: number | null;
  weeks?: RepoWeek[];
  commits_total?: number | null;
  commits_window_weeks?: number;
  refreshed_at?: string | null;
  stale?: boolean;
  source?: string;
};

export type VaultSource = {
  name?: string;
  type?: string;
  documents?: number;
  status?: string;
  last_synced_at?: string | null;
};

export type LifecycleState = {
  enabled?: boolean;
  ok?: boolean;
};

export type VaultState = {
  version?: string;
  healthy?: boolean;
  updated_at?: string | null;
  sources?: VaultSource[];
  totals?: {
    github_repositories?: number;
    documents?: number;
    linear_issues?: number;
  };
  lifecycle?: LifecycleState;
  repo?: RepoBlock;
};

/** null means the fetch failed, which the pages render as their own fallback. */
let inFlight: Promise<VaultState | null> | null = null;

export function fetchVaultState(): Promise<VaultState | null> {
  if (!inFlight) {
    inFlight = fetch("/api/state", { headers: { Accept: "application/json" } })
      .then((response) => {
        if (!response.ok) throw new Error(`state ${response.status}`);
        return response.json() as Promise<VaultState>;
      })
      .catch(() => null);
  }
  return inFlight;
}

export type VaultStateResult = { state: VaultState | null; settled: boolean };

export function useVaultState(): VaultStateResult {
  const [result, setResult] = useState<VaultStateResult>({ state: null, settled: false });

  useEffect(() => {
    let live = true;
    fetchVaultState().then((state) => {
      if (live) setResult({ state, settled: true });
    });
    return () => {
      live = false;
    };
  }, []);

  return result;
}

/** "3 hours ago", "on 2026-07-12". Empty string when there is no usable date. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const minutes = Math.round((Date.now() - at) / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}${hours === 1 ? " hour ago" : " hours ago"}`;
  const days = Math.round(hours / 24);
  if (days < 8) return `${days}${days === 1 ? " day ago" : " days ago"}`;
  return `on ${new Date(at).toISOString().slice(0, 10)}`;
}

/** Versions are stamped bare ("0.4.0") but read as versions ("v0.4.0"). */
export function versionLabel(version: string | undefined): string {
  if (!version) return "";
  return /^[0-9]/.test(version) ? `v${version}` : version;
}
