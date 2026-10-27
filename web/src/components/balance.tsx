import type { UseQueryResult } from "@tanstack/react-query";
import type { BalanceAmount, BalanceOut } from "@/client/types.gen";
import { Hint } from "@/components/common";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A balance lookup: the provider's balance, or why it has none (the endpoint's 422). */
export type BalanceResult = { balance: BalanceOut } | { unsupported: string };

/** Balance lookups are slow upstream calls: cache for five minutes and show a failure as-is. */
export const BALANCE_QUERY = { staleTime: 5 * 60_000, retry: false } as const;

/** queryFn body for the balance endpoints: a 422 (no balance API) is a result, other failures throw. */
export async function balanceResult(
  call: Promise<{ data?: BalanceOut; error?: unknown; response?: Response }>,
): Promise<BalanceResult> {
  const { data, error, response } = await call;
  if (data !== undefined) return { balance: data };
  if (response?.status === 422) return { unsupported: errorMessage(error) };
  throw error;
}

const SYMBOLS: Record<string, string> = { CNY: "¥", USD: "$" };

function fmtAmount(a: BalanceAmount): string {
  if (a.value == null) return "不限";
  const n = a.value.toFixed(2);
  if (!a.currency) return n;
  const symbol = SYMBOLS[a.currency];
  return symbol ? `${symbol}${n}` : `${n} ${a.currency}`;
}

/** Table cell content for a provider's balance: the first amount, the rest on hover. */
export function BalanceView({ query, enabled }: { query: UseQueryResult<BalanceResult>; enabled: boolean }) {
  if (!enabled) return <span className="text-muted-foreground">—</span>;
  if (query.isPending) return <Skeleton className="h-4 w-16" />;
  if (query.isError) {
    return (
      <Hint content={errorMessage(query.error)}>
        <span className="text-xs text-destructive">查询失败</span>
      </Hint>
    );
  }
  const result = query.data;
  if ("unsupported" in result) {
    return (
      <Hint content={result.unsupported}>
        <span className="text-muted-foreground">—</span>
      </Hint>
    );
  }
  const { amounts, note, available, fetched_at } = result.balance;
  const [primary] = amounts;
  if (!primary) {
    return (
      <Hint content={note}>
        <span className="text-muted-foreground">—</span>
      </Hint>
    );
  }
  const details = [
    ...amounts.map((a) => `${a.label}：${fmtAmount(a)}`),
    ...(note ? ["", note] : []),
    "",
    `更新于 ${fmtTime(fetched_at)}`,
  ].join("\n");
  return (
    <Hint content={details}>
      <span className={cn("tabular-nums", available === false && "text-destructive")}>
        {primary.label !== "余额" && <span className="text-xs text-muted-foreground">{primary.label} </span>}
        {fmtAmount(primary)}
      </span>
    </Hint>
  );
}
