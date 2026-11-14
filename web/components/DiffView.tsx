import type { DiffRow } from "@/lib/diff";

const SIGN: Record<DiffRow["kind"], string> = { meta: "", hunk: "", add: "+", del: "−", ctx: " " };

/**
 * A diff as a table: old and new line numbers, a +/− sign (so the meaning never depends on
 * colour alone) and the line. Whitespace is preserved; long lines scroll instead of wrapping.
 */
export function DiffView({ rows, label }: { rows: DiffRow[]; label: string }) {
  if (rows.length === 0) return <p className="code p-3 text-soft">No differences.</p>;
  return (
    <div className="overflow-x-auto" role="region" aria-label={label} tabIndex={0}>
      <table className="code w-full border-collapse">
        <tbody>
          {rows.map((row, i) => {
            if (row.kind === "meta") {
              return (
                <tr key={i} className="text-soft">
                  <td colSpan={4} className="px-3 py-0.5 whitespace-pre">
                    {row.text}
                  </td>
                </tr>
              );
            }
            if (row.kind === "hunk") {
              return (
                <tr key={i} className="bg-sunk text-soft">
                  <td colSpan={4} className="px-3 py-0.5 whitespace-pre">
                    {row.text}
                  </td>
                </tr>
              );
            }
            const oldNo = row.kind === "add" ? "" : row.oldNo;
            const newNo = row.kind === "del" ? "" : row.newNo;
            return (
              <tr key={i} className={row.kind === "add" ? "diff-add" : row.kind === "del" ? "diff-del" : ""}>
                <td className="w-10 select-none px-2 text-right text-soft tabular-nums">{oldNo}</td>
                <td className="w-10 select-none px-2 text-right text-soft tabular-nums">{newNo}</td>
                <td className="w-5 select-none text-center font-bold" aria-label={row.kind === "add" ? "added" : row.kind === "del" ? "removed" : undefined}>
                  {SIGN[row.kind]}
                </td>
                <td className="pr-3 whitespace-pre">{row.text}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
