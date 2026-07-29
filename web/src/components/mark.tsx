/* The Pixel Bastion: a 7x7 crenellated castle, one <i> per cell.
 *
 * The hand-written pages paint this from JavaScript because they had no way to
 * express 49 elements without writing 49 tags. Here it is just markup, so it is
 * in the exported HTML and never depends on a script running.
 */
const GRID = [
  "1010101",
  "1111111",
  "1111111",
  "1111111",
  "1101011",
  "1101011",
  "1101011",
].join("");

const CELL_ON = "bg-[linear-gradient(135deg,var(--accent),var(--accent-ink))]";

export function Mark({ className = "size-[22px] gap-px" }: { className?: string }) {
  return (
    <span className={`grid shrink-0 grid-cols-7 grid-rows-7 ${className}`} aria-hidden="true">
      {GRID.split("").map((cell, i) => (
        <i key={i} className={cell === "1" ? CELL_ON : undefined} />
      ))}
    </span>
  );
}
