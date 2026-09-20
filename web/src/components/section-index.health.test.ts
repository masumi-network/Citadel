import { describe, expect, it } from "vitest";

import { healthPill } from "./section-index";
import type { VaultState } from "@/lib/vault-state";

const healthy = { version: "0.5.2.0", healthy: true } as unknown as VaultState;
const degraded = { version: "0.5.2.0", healthy: false } as unknown as VaultState;

describe("healthPill", () => {
  it("shows Loading while the read is unsettled, never a false-green Live", () => {
    const pill = healthPill(null, false);
    expect(pill.text).toBe("Loading");
    expect(pill.tone).toBe("muted");
    expect(pill.text).not.toContain("Live");
  });

  it("shows Unavailable when a settled read is null (fetch failed)", () => {
    const pill = healthPill(null, true);
    expect(pill.text).toBe("Unavailable · reload page");
    expect(pill.tone).toBe("warn");
    expect(pill.text).not.toContain("Live");
  });

  it("shows Degraded when the settled state reports healthy=false", () => {
    expect(healthPill(degraded, true)).toEqual({
      text: "Degraded · v0.5.2.0",
      tone: "warn",
    });
  });

  it("shows Live with version when the settled state is healthy", () => {
    expect(healthPill(healthy, true)).toEqual({
      text: "Live · v0.5.2.0",
      tone: "good",
    });
  });
});
