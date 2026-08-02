import dynamic from "next/dynamic";
import {
  Component,
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

/* React, React DOM and React Flow are a few hundred kilobytes, on a page that
   otherwise ships almost nothing. So the four-step spine below is the real
   diagram: plain markup, in the exported HTML, no download. This upgrades it in
   place the first time it comes near the viewport.

   `ssr: false` is not an optimisation here, it is a requirement. React Flow
   measures the DOM to place its nodes, and the export is pre-rendered in Node
   where there is nothing to measure. It also keeps the component out of the
   pre-rendered HTML, which is what keeps a `transform` style attribute out of
   the exported markup and the strict style-src policy intact. */
const PipelineFlow = dynamic(() => import("@/components/pipeline-flow"), { ssr: false });

const STEPS: Array<{ title: string; sub: string; highlight?: boolean }> = [
  { title: "Capture", sub: "hooks, no filing" },
  { title: "Your Node", sub: "seat scoped", highlight: true },
  { title: "Promotion", sub: "scan · approve" },
  { title: "Central", sub: "shared" },
];

/* A chunk that fails to load, or a diagram that throws while rendering, must
   not take the section with it. The spine is already on screen and already
   correct, so the fallback is to keep it and say nothing. */
class FlowBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

export function PipelineDiagram() {
  const spine = useRef<HTMLDivElement>(null);
  const [wanted, setWanted] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!("IntersectionObserver" in window)) return;
    const target = spine.current;
    if (!target) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        // Below 620px React Flow cannot fit the ~1680px-wide topology:
        // fitView clamps at minZoom, both sides are clipped, and with
        // panning off for coarse pointers the clipped columns are
        // unreachable. The static spine is the honest rendering there, so
        // phones keep it. Not disconnecting means a viewport that later
        // widens past the gate (rotation, window resize) upgrades on the
        // next intersection instead of being locked out.
        if (!window.matchMedia("(min-width: 620px)").matches) return;
        observer.disconnect();
        setWanted(true);
      },
      // 300px of lead time, so the chunk is usually already parsed by the time
      // the section is actually read and the swap is not something you watch
      // happen.
      { rootMargin: "300px 0px" }
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  const onReady = useCallback(() => setReady(true), []);

  return (
    <>
      {/* Unmounted rather than marked `hidden` once the real diagram is up. The
          `hidden` attribute is only a UA-stylesheet `display: none`, so any
          author rule setting a display -- `flex`, here -- outranks it and the
          element stays on screen. Removing it says what is meant and cannot be
          quietly undone by a styling change. */}
      {ready ? null : (
        <div
          ref={spine}
          aria-label="How work reaches the vault"
          className="mt-[26px] flex items-center justify-between gap-2.5 max-[620px]:flex-col max-[620px]:gap-1.5"
        >
          {STEPS.map((step, i) => (
            <Fragment key={step.title}>
              {i > 0 ? (
                <span
                  aria-hidden="true"
                  className="flex-none text-[18px] text-border-2 max-[620px]:rotate-90"
                >
                  →
                </span>
              ) : null}
              <div className="min-w-0 flex-1 text-center">
                <span
                  className={`block text-[15.5px] font-semibold tracking-[-.01em] ${
                    step.highlight ? "text-accent-ink" : ""
                  }`}
                >
                  {step.title}
                </span>
                <span className="mt-0.5 block font-mono text-xs text-ink-3">{step.sub}</span>
              </div>
            </Fragment>
          ))}
        </div>
      )}

      {/* The read line is the guarantee, and the interactive diagram does not
          restate it, so it stays on screen either way. */}
      <p className="mb-[30px] mt-[18px] border-t border-border pt-3.5 text-center text-[14.5px] text-ink-2">
        You and your agents read via <b className="text-ink">MCP, CLI or web</b>: your Node plus
        Central, never another seat's.
      </p>

      {wanted ? (
        <FlowBoundary>
          {/* Rendered before it reports ready, because React Flow fits the graph
              to a container it can measure and a hidden container measures
              zero. The 300px of lead time above is what keeps that from being
              something the reader sees. */}
          <div className="mb-3.5 h-[520px] border border-border bg-surface max-[620px]:h-[400px]">
            <PipelineFlow onReady={onReady} />
          </div>
          {ready ? (
            <p className="mb-[30px] text-[12.5px] leading-[1.6] text-ink-3">
              Capture to promotion to read, end to end. Hover a step to follow its path, and select
              one to read what it does.
            </p>
          ) : null}
        </FlowBoundary>
      ) : null}
    </>
  );
}
