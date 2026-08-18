import { BAND, BAND_IN, CODE, FOOT_NOTE } from "@/components/ui";

const LICENSE_HREF =
  "https://github.com/masumi-network/Citadel/blob/main/LICENSE";
const SOURCE_HREF = "https://github.com/masumi-network/Citadel";
const OWNER_HREF = "https://utxo.ag/";
const WINDOW = "window v0.2.0 → v0.5.1.";

const COL_K =
  "mb-2 text-[11px] font-semibold uppercase tracking-[.16em] text-ink-3";
const COL_V = "m-0 text-[14.5px] text-ink-2";
const COLS =
  "grid grid-cols-5 gap-8 border-t border-border pt-8 max-[900px]:grid-cols-3 max-[620px]:grid-cols-1 max-[620px]:gap-5";

/* A closing footer that is itself a band is full-bleed, so it carries no top
   margin: the gap an in-column footer wants would show as a stripe of --ground
   between two bands. */
export function SiteFooter({ note }: { note?: string | null }) {
  return (
    <footer className={`${BAND} bg-surface`}>
      <div className={BAND_IN}>
        <div className={COLS}>
          <div>
            <p className={COL_K}>Check</p>
            <p className={COL_V}>
              <code className={CODE}>citadel status</code>
            </p>
          </div>
          <div>
            <p className={COL_K}>Source</p>
            <p className="m-0 text-[14.5px]">
              <a href={SOURCE_HREF}>github.com/masumi-network/Citadel</a>
            </p>
          </div>
          <div>
            <p className={COL_K}>License</p>
            <p className="m-0 text-[14.5px]">
              <a href={LICENSE_HREF}>Apache-2.0</a>
            </p>
          </div>
          <div>
            <p className={COL_K}>Owner</p>
            <p className={COL_V}>
              <a href={OWNER_HREF}>utxo AG</a>
            </p>
          </div>
          <div>
            <p className={COL_K}>Contact</p>
            <p className="m-0 text-[14.5px]">
              <a href="/contact">Contact</a>
            </p>
          </div>
        </div>
        <p className={FOOT_NOTE}>
          Live node: <code className={CODE}>citadel.utxo.ag</code>
          {` · ${note || WINDOW}`}
        </p>
      </div>
    </footer>
  );
}
