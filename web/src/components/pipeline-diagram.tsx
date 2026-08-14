import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState, type ReactNode, Component } from "react";

import { LAYERS, PATH } from "@/components/pipeline-data";

/* React Flow has to be client-only: it measures the DOM, and a static export
 * cannot carry a `style=""` from a Node render. See web/README.md and
 * https://reactflow.dev/learn */
const PipelineFlow = dynamic(() => import("@/components/pipeline-flow"), { ssr: false });

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
  const fallback = useRef<HTMLDivElement>(null);
  const [wanted, setWanted] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!("IntersectionObserver" in window)) {
      setWanted(true);
      return;
    }
    const target = fallback.current;
    if (!target) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        observer.disconnect();
        setWanted(true);
      },
      { rootMargin: "300px 0px" }
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  const onReady = useCallback(() => setReady(true), []);

  return (
    <div className="mb-8">
      {ready ? null : (
        <div ref={fallback}>
          <ol
            aria-label="How work reaches the vault"
            className="mb-4 grid list-none grid-cols-5 gap-2 p-0 max-[620px]:grid-cols-1"
          >
            {PATH.map((step, i) => (
              <li key={step.title} className="flex items-center gap-3 border border-border bg-surface px-3 py-3">
                <span className="font-mono text-[11px] text-ink-3">{i + 1}</span>
                <span>
                  <span
                    className={`block text-[15px] font-semibold tracking-[-.01em] ${
                      step.highlight ? "text-accent-ink" : ""
                    }`}
                  >
                    {step.title}
                  </span>
                  <span className="mt-0.5 block font-mono text-xs text-ink-3">{step.sub}</span>
                </span>
              </li>
            ))}
          </ol>
          <div aria-label="The architecture, layer by layer" className="border border-border">
            {LAYERS.map((layer) => (
              <div
                key={layer.id}
                className="grid grid-cols-[9rem_minmax(0,1fr)] border-t border-border first:border-t-0 max-[620px]:grid-cols-1"
              >
                <p className="bg-surface px-3 py-3 font-mono text-[10.5px] font-semibold uppercase tracking-[.14em] text-ink-3">
                  {layer.label}
                </p>
                <div className="grid grid-cols-3 gap-2 bg-surface p-2 max-[620px]:grid-cols-1">
                  {layer.items.map((item) => (
                    <details key={item.id} className="border border-border bg-surface">
                      <summary className="cursor-pointer px-3 py-2.5 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent">
                        <span
                          className={`text-[13.5px] font-semibold tracking-[-.01em] ${
                            item.highlight ? "text-accent-ink" : ""
                          }`}
                        >
                          {item.label}
                        </span>{" "}
                        <span className="font-mono text-[9.5px] tracking-[.02em] text-ink-3">
                          {item.sub}
                        </span>
                      </summary>
                      <p className="border-t border-border px-3 py-2 text-[12.5px] leading-[1.55] text-ink-2">
                        {item.detail}
                      </p>
                    </details>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {wanted ? (
        <FlowBoundary>
          <div className="mb-3.5 h-[1100px] border border-border bg-surface max-[620px]:h-[700px]">
            <PipelineFlow onReady={onReady} />
          </div>
        </FlowBoundary>
      ) : null}

      <p className="mb-0 mt-4 border-t border-border pt-3.5 text-[14.5px] text-ink-2">
        You and your agents read via <b className="text-ink">MCP, CLI or web</b>: your Node plus
        Central, never another seat&apos;s.
      </p>
    </div>
  );
}
