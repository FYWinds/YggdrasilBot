import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon } from "lucide-react";
import { type ReactNode, useState } from "react";
import type { StickerOut } from "@/client";
import {
  getStickerOptions,
  listStickersOptions,
  setStickerEnabledMutation,
} from "@/client/@tanstack/react-query.gen";
import { Empty, GroupSelect, OptionSelect, PageHeader, Pager, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const LIMIT = 60;

const STATUS_LABELS: Record<StickerOut["status"], string> = {
  fetching: "下载中",
  untagged: "待打标",
  ready: "已打标",
  failed: "失败",
};

// Error codes the collector stores as-is; anything else is a free-text message.
const ERROR_LABELS: Record<string, string> = {
  too_large: "文件超过 5MB，没有保存原图",
};

const UNSAFE_LABELS: Record<string, string> = {
  sexual: "色情",
  gore: "血腥暴力",
  extremism: "极端/政治",
  personal_data: "个人隐私",
};

const hasImage = (s: StickerOut) => s.kind === "image" && !!s.mime;

export function StickersPage() {
  const [kind, setKind] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<string | null>(null);
  const [flagged, setFlagged] = useState<string | null>(null);
  const [groupId, setGroupId] = useState<number | null>(null);
  const [order, setOrder] = useState<string | null>("seen");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
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
  const setStickerEnabled = (id: number, on: boolean) => toggle.mutate({ path: { sticker_id: id }, body: { enabled: on } });
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
        description="群里出现过的表情包：自定义表情在出现时保存原图，由「表情包打标」任务写描述、打标签并做审核标记；商城表情只记录 id。下载失败的默认不显示，在状态里选「失败」查看。点卡片看大图和详细信息。"
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
              className="w-40"
              value={status}
              onChange={filter(setStatus)}
              noneLabel="全部（不含失败）"
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
      {stickers.data?.items.length === 0 && <Empty>没有符合条件的表情包</Empty>}
      {!!stickers.data?.items.length && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-4">
          {stickers.data.items.map((s) => (
            <StickerCard
              key={s.id}
              sticker={s}
              onOpen={() => setSelected(s.id)}
              onToggle={(on) => setStickerEnabled(s.id, on)}
            />
          ))}
        </div>
      )}
      {stickers.data && <Pager total={stickers.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      {selected !== null && (
        <StickerDialog
          id={selected}
          onClose={() => setSelected(null)}
          onToggle={(on) => setStickerEnabled(selected, on)}
        />
      )}
    </div>
  );
}

function StickerBadges({ sticker: s }: { sticker: StickerOut }) {
  return (
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
  );
}

function Placeholder({ sticker: s }: { sticker: StickerOut }) {
  return (
    <span className="px-2 text-center text-xs text-muted-foreground">
      {s.kind === "mface" ? `商城表情「${s.summary || "—"}」` : STATUS_LABELS[s.status]}
    </span>
  );
}

function StickerCard({
  sticker: s,
  onOpen,
  onToggle,
}: {
  sticker: StickerOut;
  onOpen: () => void;
  onToggle: (enabled: boolean) => void;
}) {
  return (
    // A div, not a <button>: the card contains a switch.
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === "Enter" && onOpen()}
      className="flex cursor-pointer flex-col gap-2 rounded-lg border p-3 text-left text-sm transition-colors hover:border-foreground/30"
    >
      <div className="flex h-36 items-center justify-center rounded-md bg-muted/40">
        {hasImage(s) ? (
          <img
            src={`/api/stickers/${s.id}/image`}
            alt={s.description || s.summary}
            loading="lazy"
            className="max-h-36 max-w-full object-contain"
          />
        ) : (
          <Placeholder sticker={s} />
        )}
      </div>
      <StickerBadges sticker={s} />
      {s.description && <p className="line-clamp-4">{s.description}</p>}
      {s.usage && <p className="line-clamp-3 text-xs text-muted-foreground">适合：{s.usage}</p>}
      {s.ocr_text && <p className="line-clamp-2 text-xs text-muted-foreground">文字：{s.ocr_text}</p>}
      {s.error && (
        <p className="line-clamp-2 text-xs break-all text-destructive">{ERROR_LABELS[s.error] ?? s.error}</p>
      )}
      <div className="mt-auto flex items-center justify-between gap-2 pt-1 text-xs text-muted-foreground">
        <span>
          {s.seen_count} 次 · {s.group_count} 个群 · {fmtTime(s.last_seen)}
        </span>
        <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <Switch checked={s.enabled} onCheckedChange={onToggle} />
        </span>
      </div>
    </div>
  );
}

function StickerDialog({ id, onClose, onToggle }: { id: number; onClose: () => void; onToggle: (enabled: boolean) => void }) {
  const detail = useQuery(getStickerOptions({ path: { sticker_id: id } }));
  const d = detail.data;
  const size =
    d?.size_bytes == null
      ? "—"
      : d.size_bytes >= 1024 * 1024
        ? `${(d.size_bytes / 1024 / 1024).toFixed(1)} MB`
        : `${(d.size_bytes / 1024).toFixed(0)} KB`;
  const facts: [string, ReactNode][] = d
    ? [
        ["类型", d.kind === "mface" ? "商城表情" : "自定义表情"],
        [d.kind === "mface" ? "包 / 表情 id" : "md5", <span className="font-mono break-all select-all">{d.ref}</span>],
        ["格式", d.mime || "—"],
        ["大小", size],
        ["尺寸", d.width && d.height ? `${d.width} × ${d.height}` : "—"],
        ["帧数", d.frames ?? "—"],
        ["出现", `${d.seen_count} 次 · ${d.group_count} 个群`],
        ["首次出现", fmtTime(d.first_seen)],
        ["最近出现", fmtTime(d.last_seen)],
        ["打标时间", fmtTime(d.tagged_at)],
        ["打标尝试", d.tag_attempts],
        ["审核", d.unsafe_category ? (UNSAFE_LABELS[d.unsafe_category] ?? d.unsafe_category) : "无风险"],
      ]
    : [];
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>
            表情包 #{id} <span className="text-sm font-normal text-muted-foreground">{d?.summary}</span>
          </DialogTitle>
        </DialogHeader>
        <QueryState isLoading={detail.isLoading} error={detail.error} />
        {d && (
          <div className="grid gap-4 text-sm md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div className="flex flex-col gap-2">
              <div className="flex min-h-48 items-center justify-center rounded-md bg-muted/40 p-2">
                {hasImage(d) ? (
                  <img
                    src={`/api/stickers/${d.id}/image`}
                    alt={d.description || d.summary}
                    className="max-h-[60svh] max-w-full object-contain"
                  />
                ) : (
                  <Placeholder sticker={d} />
                )}
              </div>
              <div className="flex items-center justify-between gap-2">
                {hasImage(d) ? (
                  <Button size="sm" variant="outline" asChild>
                    <a href={`/api/stickers/${d.id}/image`} target="_blank" rel="noreferrer">
                      打开原图 <ExternalLinkIcon />
                    </a>
                  </Button>
                ) : (
                  <span />
                )}
                <label className="flex items-center gap-2 text-xs">
                  {d.enabled ? "已启用" : "已禁用"}
                  <Switch checked={d.enabled} onCheckedChange={onToggle} />
                </label>
              </div>
            </div>
            <div className="flex flex-col gap-3">
              <StickerBadges sticker={d} />
              {d.description && <p>{d.description}</p>}
              {d.usage && (
                <p>
                  <span className="text-muted-foreground">适合：</span>
                  {d.usage}
                </p>
              )}
              {d.ocr_text && (
                <div>
                  <span className="text-muted-foreground">图中文字：</span>
                  <p className="whitespace-pre-wrap">{d.ocr_text}</p>
                </div>
              )}
              {d.error && <p className="break-all text-destructive">错误：{ERROR_LABELS[d.error] ?? d.error}</p>}
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
                {facts.map(([label, value]) => (
                  <div key={label} className="contents">
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </div>
            {d.groups.length > 0 && (
              <div className="md:col-span-2">
                <div className="mb-1 font-medium">各群出现次数</div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>群</TableHead>
                      <TableHead className="w-24 text-right">次数</TableHead>
                      <TableHead className="w-48">最近</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {d.groups.map((g) => (
                      <TableRow key={g.group_id}>
                        <TableCell>
                          {g.group_name || "—"} <span className="font-mono text-xs text-muted-foreground">{g.group_id}</span>
                        </TableCell>
                        <TableCell className="text-right">{g.count}</TableCell>
                        <TableCell>{fmtTime(g.last_seen)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
