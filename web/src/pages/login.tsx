import Head from "next/head";
import { useState, type FormEvent } from "react";

import { TopNav } from "@/components/top-nav";
import { CODE, EYEBROW, FIELD_HINT, FIELD_INPUT, FIELD_LABEL, SUBMIT } from "@/components/ui";

const REJECTED = "Seat token or access key was rejected.";

/* Seat access.
 *
 * The page this replaces is generated from a Python string literal in
 * kb/server.py, which is why it has drifted from the other four twice: it is
 * the one public page that no stylesheet, template or test file owns. Being a
 * page like the rest of them is the actual fix. The Python version keeps
 * serving /login until the switch-over.
 *
 * A single centred card, so the glow sits behind it rather than in a corner.
 * The card's own surface keeps the text legible on top of it.
 */
export default function Login() {
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setError("");
    setChecking(true);
    try {
      const response = await fetch("/admin/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_key: new FormData(form).get("accessKey") }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || REJECTED);
      }
      const session = await response.json().catch(() => ({}));
      // Seat readers and writers need the legacy Access panel. Keep env and
      // seat-less readers on the Next read-only dashboard.
      const hasSeat = typeof session.seat_slug === "string" && Boolean(session.seat_slug.trim());
      window.location.assign(hasSeat || session.role !== "reader" ? "/app" : "/next/app");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : REJECTED);
    } finally {
      setChecking(false);
    }
  }

  return (
    <>
      <Head>
        <title>Citadel</title>
        <meta name="description" content="Sign in to your Citadel seat." />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      {/* TopNav is sticky at the page edge. The glow is clipped on a child that
          does not wrap the nav, same split as HeroBand: overflow-hidden on the
          nav's ancestor would pin the bar inside this box. The glow sits in
          the same corner as every other public page. */}
      <TopNav current="/login" />
      <div className="relative overflow-hidden bg-surface p-0">
        <div className="hero-glow" aria-hidden="true" />
        <main className="relative flex min-h-[calc(100vh-var(--topnav-h))] items-center justify-center px-[26px] pb-20 pt-10 max-[620px]:px-4 max-[620px]:pb-[60px] max-[620px]:pt-7">
          <div className="relative z-[1] w-full max-w-[380px]">
            <p className={EYEBROW}>Seat access</p>
            <h1 className="mb-3 text-[clamp(28px,4.4vw,38px)] font-light leading-[1.08] tracking-[-.03em]">
              Open your vault.
            </h1>
            <p className="mb-7 text-[15px] leading-[1.6] text-ink-2">
              Your Node stays private. Central is shared, and Shared Session Traces are reference-only.
              Nobody reads another seat&apos;s notes.
            </p>
            <form onSubmit={onSubmit} className="flex flex-col gap-2">
              <label className={FIELD_LABEL} htmlFor="adminKey">
                Seat token
              </label>
              <input
                id="adminKey"
                className={FIELD_INPUT}
                name="accessKey"
                type="password"
                autoComplete="current-password"
                required
                autoFocus
                placeholder="ctdl_…"
              />
              <p className={FIELD_HINT}>
                Paste the seat token from your admin (or run{" "}
                <code className={CODE}>citadel seat token</code>). Operators can still use the env
                admin key.
              </p>
              {error ? (
                <p className="text-[13px] leading-[1.6] text-warn" role="alert">
                  {error}
                </p>
              ) : null}
              <button
                type="submit"
                disabled={checking}
                aria-busy={checking}
                className={SUBMIT}
              >
                {checking ? "Checking" : "Open workspace"}
              </button>
            </form>
            <p className="mt-[22px] text-[13px] text-ink-3">
              No token yet?{" "}
              <a
                className="border-b border-border-2 text-ink-2 no-underline hover:border-accent hover:text-accent-ink"
                href="/"
              >
                Read what Citadel is
              </a>
              , or{" "}
              <a
                className="border-b border-border-2 text-ink-2 no-underline hover:border-accent hover:text-accent-ink"
                href="/contact"
              >
                ask us for one
              </a>
              .
            </p>
          </div>
        </main>
      </div>
    </>
  );
}
