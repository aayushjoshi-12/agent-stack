// Cost and token formatting, matching the Python gateways (gateways/base.py) so every surface
// shows the same figures the same way.

export function formatCost(usd: number): string {
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return usd >= 1 ? `$${usd.toFixed(2)}` : `$${usd.toFixed(3)}`;
}

export function formatTokens(count: number): string {
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`.replace(".0k", "k");
  return `${(count / 1_000_000).toFixed(1)}M`.replace(".0M", "M");
}

/**
 * A timestamp for people, in the reader's local time: "today 14:05", "yesterday 09:30",
 * "14 Nov 07:21" (with the year when it is not this year). Matches the terminal's chat list.
 */
export function formatWhen(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const clock = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(when)) / 86_400_000);
  if (days === 0) return `today ${clock}`;
  if (days === 1) return `yesterday ${clock}`;
  const day = when.toLocaleDateString([], { day: "numeric", month: "short", ...(when.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }) });
  return `${day} ${clock}`;
}
