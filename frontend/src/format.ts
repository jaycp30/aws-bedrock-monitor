export const REGION_LABELS: Record<string, string> = {
  "us-east-1": "N. Virginia",
  "us-west-2": "Oregon",
  "eu-west-1": "Ireland",
  "ca-central-1": "Canada Central",
  "ap-southeast-2": "Sydney",
  "ap-northeast-1": "Tokyo",
};

export function regionLabel(r: string): string {
  return REGION_LABELS[r] ? `${REGION_LABELS[r]} (${r})` : r;
}

export function fmtInt(n: number): string {
  return n.toLocaleString("en-US");
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(n);
}

export function fmtUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return "$" + n.toFixed(4);
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function shortModel(id: string): string {
  // Strip cross-region prefix (jp. / apac. / us. / eu. / global.) and provider.
  return id.replace(/^(global|jp|apac|ap|us|eu|ca)\./, "").replace(/^anthropic\.|^amazon\./, "");
}

// The data-viz series colors, read from the theme tokens.
export const SERIES_COLORS = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
];
