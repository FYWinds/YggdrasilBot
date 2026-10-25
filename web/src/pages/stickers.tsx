import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { StickerOut } from "@/client";
import { listStickersOptions, setStickerEnabledMutation } from "@/client/@tanstack/react-query.gen";
import { Empty, GroupSelect, OptionSelect, PageHeader, Pager, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { fmtTime } from "@/lib/format";

const LIMIT = 60;

const STATUS_LABELS: Record<StickerOut["status"], string> = {
  fetching: "下载中",
  untagged: "待打标",
  ready: "已打标",
  failed: "失败",
};

const UNSAFE_LABELS: Record<string, string> = {
  sexual: "色情",
  gore: "血腥暴力",
  extremism: "极端/政治",
  personal_data: "个人隐私",
};

export function StickersPage() {
  const [kind, setKind] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<string | null>(null);
  const [flagged, setFlagged] = useState<string | null>(null);
  const [groupId, setGroupId] = useState<number | null>(null);
  const [order, setOrder] = useState<string | null>("seen");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const stickers = useQuery(
    listStickersOptions({
      query: {
        kind: kind as StickerOut["kind"] | null,
        status: status as StickerOut["status"] | null,
        enabled: enabled === null ? null : enabled === "true",
        flagged: flagged === null ? null : flagged === "true",
        group_id: groupId,
        q: q || null,
        order: order === "recent" ? "recent" : "seen",
        limit: LIMIT,
        offset,
      },
    }),
  );
  const toggle = useMutation(setStickerEnabledMutation());
  // Every filter change starts from the first page.
  const filter =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setOffset(0);
    };
  return (
    <div>
      <PageHeader
        title="表情包"
        description="群里出现过的表情包：自定义表情在出现时保存原图，由「表情包打标」任务写描述、打标签并做审核标记；商城表情只记录 id。"
        actions={
          <>
            <Input
              className="w-56"
              placeholder="搜索描述 / 文字 / 标签"
              value={q}
              onChange={(e) => filter(setQ)(e.target.value)}
            />
            <GroupSelect className="w-56" value={groupId} onChange={filter(setGroupId)} />
            <OptionSelect
              className="w-28"
              value={kind}
              onChange={filter(setKind)}
              noneLabel="全部类型"
              options={[
                { value: "image", label: "自定义" },
                { value: "mface", label: "商城" },
              ]}
            />
            <OptionSelect
              className="w-28"
              value={status}
              onChange={filter(setStatus)}
              noneLabel="全部状态"
              options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <OptionSelect
              className="w-28"
              value={flagged}
              onChange={filter(setFlagged)}
              noneLabel="审核：全部"
              options={[
                { value: "true", label: "有风险" },
                { value: "false", label: "无风险" },
              ]}
            />
            <OptionSelect
              className="w-28"
              value={enabled}
              onChange={filter(setEnabled)}
              noneLabel="启用：全部"
              options={[
                { value: "true", label: "已启用" },
                { value: "false", label: "已禁用" },
              ]}
            />
            <OptionSelect
              className="w-28"
              value={order}
              onChange={filter(setOrder)}
              options={[
                { value: "seen", label: "最常用" },
                { value: "recent", label: "最近出现" },
              ]}
            />
          </>
        }
      />
      <QueryState isLoading={stickers.isLoading} error={stickers.error} />
      {stickers.data?.items.length === 0 && <Empty>还没有收集到表情包</Empty>}
      {!!stickers.data?.items.length && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-4">
          {stickers.data.items.map((s) => (
            <StickerCard
              key={s.id}
              sticker={s}
              onToggle={(c) => toggle.mutate({ path: { sticker_id: s.id }, body: { enabled: c } })}
            />
          ))}
        </div>
      )}
      {stickers.data && <Pager total={stickers.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}

function StickerCard({ sticker: s, onToggle }: { sticker: StickerOut; onToggle: (enabled: boolean) => void }) {
  const hasImage = s.kind === "image" && s.status !== "fetching" && !(s.status === "failed" && !s.mime);
  return (
    <div className="flex flex-col gap-2 rounded-lg border p-3 text-sm">
      <div className="flex h-36 items-center justify-center rounded-md bg-muted/40">
        {hasImage ? (
          <img
            src={`/api/stickers/${s.id}/image`}
            alt={s.description || s.summary}
            loading="lazy"
            className="max-h-36 max-w-full object-contain"
          />
        ) : (
          <span className="px-2 text-center text-xs text-muted-foreground">
            {s.kind === "mface" ? `商城表情「${s.summary || "—"}」` : STATUS_LABELS[s.status]}
          </span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <Badge variant={s.status === "failed" ? "destructive" : "secondary"}>{STATUS_LABELS[s.status]}</Badge>
        {s.unsafe_category && (
          <Badge variant="destructive">审核：{UNSAFE_LABELS[s.unsafe_category] ?? s.unsafe_category}</Badge>
        )}
        {!!s.frames && s.frames > 1 && <Badge variant="outline">动图</Badge>}
        {s.emotions.map((e) => (
          <Badge key={e} variant="outline">
            {e}
          </Badge>
        ))}
      </div>
      {s.description && <p>{s.description}</p>}
      {s.usage && <p className="text-xs text-muted-foreground">适合：{s.usage}</p>}
      {s.ocr_text && <p className="text-xs text-muted-foreground">文字：{s.ocr_text}</p>}
      {s.error && <p className="text-xs break-all text-destructive">{s.error}</p>}
      <div className="mt-auto flex items-center justify-between gap-2 pt-1 text-xs text-muted-foreground">
        <span title={`首次 ${fmtTime(s.first_seen)}`}>
          {s.seen_count} 次 · {s.group_count} 个群 · {fmtTime(s.last_seen)}
        </span>
        <Switch checked={s.enabled} onCheckedChange={onToggle} />
      </div>
    </div>
  );
}
