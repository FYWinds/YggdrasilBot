import type { ModelCaps, ProviderOut } from "@/client/types.gen";
import { Hint } from "@/components/common";
import { Badge } from "@/components/ui/badge";

function fmtTokens(n: number): string {
  return n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);
}

export function capsOf(provider: ProviderOut | undefined, model: string | null | undefined): ModelCaps | undefined {
  return model ? provider?.model_caps[model.trim()] : undefined;
}

/** Capability badges from models.dev; "能力未知" (or nothing when `quiet`) if the model isn't matched. */
export function CapBadges({ caps, showId = false, quiet = false }: { caps: ModelCaps | undefined; showId?: boolean; quiet?: boolean }) {
  if (!caps) return quiet ? null : <span className="text-xs text-muted-foreground">能力未知</span>;
  const flags: [boolean, string][] = [
    [caps.vision, "看图"],
    [caps.pdf, "PDF"],
    [caps.tools, "工具"],
    [caps.reasoning, "推理"],
  ];
  const badges = (
    <span className="inline-flex flex-wrap items-center gap-1">
      {flags.map(([on, label]) => (
        <Badge key={label} variant={on ? "secondary" : "outline"} className={on ? "" : "text-muted-foreground line-through"}>
          {label}
        </Badge>
      ))}
      {caps.context != null && <Badge variant="outline">{fmtTokens(caps.context)} 上下文</Badge>}
      {showId && <span className="basis-full font-mono text-xs text-muted-foreground">{caps.catalog_id}</span>}
    </span>
  );
  return showId ? badges : <Hint content={`models.dev: ${caps.catalog_id}`}>{badges}</Hint>;
}
