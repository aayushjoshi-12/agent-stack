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
