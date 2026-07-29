/* The dashboard's data layer: the endpoint shapes this slice reads, and the
 * hooks that read them.
 *
 * The field lists below are deliberately narrow. `docs/dashboard-api-contract.md`
 * records that `/api/me/summary` returns eleven fields of which Home rendered
 * three, and that a promotion item delivers seventeen of which Review rendered
 * seven. Typing only what is rendered keeps that honest: a field that appears
 * here is a field something draws.
 */
import { useCallback, useEffect, useState } from "react";

import { api, errorMessage } from "@/lib/api";

/* --- session ------------------------------------------------------------ */

export type Role = "reader" | "writer" | "admin";

export const ROLE_ORDER: Record<Role, number> = { reader: 1, writer: 2, admin: 3 };

export type Session = {
  role: Role;
  seat_slug?: string | null;
  node_label?: string | null;
  actor?: { name?: string | null } | null;
};

export function canUse(role: Role | null, minimum: Role): boolean {
  return role !== null && ROLE_ORDER[role] >= ROLE_ORDER[minimum];
}

/* --- me/summary --------------------------------------------------------- */

export type MeSummary = {
  node_label?: string | null;
  document_count?: number;
  pending_promotions?: number;
  recent_activity?: ActivityEvent[];
  empty?: boolean;
  /* Not shipped yet. Gaps 1 and 2 in the contract map: both are being added to
     this endpoint. Optional here on purpose, so this reads a dash rather than a
     wrong number until they land, and starts working the moment they do. */
  readable_document_count?: number | null;
  captured_last_7d?: number | null;
};

export type ActivityEvent = {
  id?: string;
  type?: string;
  message?: string;
  created_at?: string | null;
  dataset?: string | null;
};

/* --- mesh --------------------------------------------------------------- */

export type Mesh = {
  stats?: { documents?: number; errors?: number };
  events?: Array<{
    id?: string;
    type?: string;
    message?: string;
    created_at?: string | null;
    details?: { dataset?: string | null };
  }>;
};

/* --- sources ------------------------------------------------------------ */

export type Source = {
  id?: string;
  name?: string | null;
  source_type?: string;
  open_conflicts?: number;
  metadata?: { last_security_scan?: { blocked?: boolean; finding_count?: number; highest_severity?: string } };
};

/* A source in trouble, as far as the API can currently say.
 *
 * There is no `last_error` on `/api/sources` (contract map gap 8), so "failing"
 * is inferred from open conflicts and, for the GitHub source only, a blocked
 * security scan. A source that failed for any other reason is invisible here,
 * and no amount of client code fixes that. */
export function failingSources(sources: Source[]): Source[] {
  return sources.filter(
    (source) =>
      (source.open_conflicts ?? 0) > 0 || source.metadata?.last_security_scan?.blocked === true
  );
}

export function failureReason(source: Source): string {
  const conflicts = source.open_conflicts ?? 0;
  if (source.metadata?.last_security_scan?.blocked) {
    const findings = source.metadata.last_security_scan.finding_count;
    return `Security scan blocked this source${findings ? `, ${findings} findings` : ""}`;
  }
  return `${conflicts} open ${conflicts === 1 ? "conflict" : "conflicts"}`;
}

/* --- promotion queue ---------------------------------------------------- */

/* Seven of the seventeen fields that arrive. Two the design spec asked for are
   deliberately absent:

   - a per-item **document count**. A pending item is one candidate note; there
     is nothing to count (gap 6).
   - a per-item **secret-scan result**. No secret scan runs over promotion
     candidates today (gap 7). `sensitive` arrives, from LLM enrichment, and is
     a weaker and different claim, so it is not read here and must not be
     substituted. An absent assurance is fine; a false one is not. */
export type PromotionItem = {
  id: string;
  seat_slug?: string;
  preview?: string;
  reference_status?: string;
  reference_reason?: string;
  repo_hints?: string[];
};

/* --- github sync -------------------------------------------------------- */

export type GithubSync = { last_checked_at?: string | null; org?: string | null };

/* --- loading ------------------------------------------------------------ */

export type Loaded<T> = { data: T | null; error: string | null; loading: boolean };

const IDLE = { data: null, error: null, loading: true } as const;

/** One endpoint, one state. `reload` is stable and safe to put in a dependency
    list or hand to a Refresh button. */
export function useEndpoint<T>(path: string, enabled = true): Loaded<T> & { reload: () => void } {
  const [state, setState] = useState<Loaded<T>>(IDLE);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let live = true;
    setState((current) => ({ ...current, loading: true }));
    api<T>(path)
      .then((data) => live && setState({ data, error: null, loading: false }))
      // An error here must not read as an empty result. The dashboard this
      // replaces resets the promotion list to [] on failure and renders "No
      // promotions are waiting for a decision", which is indistinguishable
      // from success.
      .catch((failure) => live && setState({ data: null, error: errorMessage(failure), loading: false }));
    return () => {
      live = false;
    };
  }, [path, enabled, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}

export function useSession(): Loaded<Session> {
  return useEndpoint<Session>("/api/session");
}

/* --- formatting --------------------------------------------------------- */

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

/** A number that is not there yet reads as a dash, never as zero. */
export function countOrDash(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "—";
}
