import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { listStickersOptions, setStickerEnabledMutation } from "@/client/@tanstack/react-query.gen";
import { Empty, OptionSelect, PageHeader, Pager, QueryState } from "@/components/common";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const LIMIT = 100;

export function StickersPage() {
  const [enabled, setEnabled] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const stickers = useQuery(
    listStickersOptions({
      query: { enabled: enabled === null ? null : enabled === "true", q: q || null, limit: LIMIT, offset },
    }),
  );
  const toggle = useMutation(setStickerEnabledMutation());
  return (
    <div>
      <PageHeader
        title="表情包"
        description="群里出现过的商城表情；快速响应模型会按摘要从启用的表情里挑选。"
        actions={
          <>
            <Input className="w-56" placeholder="搜索摘要" value={q} onChange={(e) => setQ(e.target.value)} />
            <OptionSelect
              className="w-28"
              value={enabled}
              onChange={(v) => {
                setEnabled(v);
                setOffset(0);
              }}
              noneLabel="全部"
              options={[
                { value: "true", label: "已启用" },
                { value: "false", label: "已禁用" },
              ]}
            />
          </>
        }
      />
      <QueryState isLoading={stickers.isLoading} error={stickers.error} />
      {stickers.data?.items.length === 0 && <Empty>还没有收集到表情包</Empty>}
      {!!stickers.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>摘要</TableHead>
              <TableHead>表情包 / 表情 id</TableHead>
              <TableHead>出现次数</TableHead>
              <TableHead>最近出现</TableHead>
              <TableHead>启用</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {stickers.data.items.map((s) => (
              <TableRow key={s.id}>
                <TableCell>{s.summary || "—"}</TableCell>
                <TableCell className="font-mono text-xs">
                  {s.package_id} / {s.emoji_id}
                </TableCell>
                <TableCell>{s.seen_count}</TableCell>
                <TableCell className="text-xs">{fmtTime(s.last_seen)}</TableCell>
                <TableCell>
                  <Switch
                    checked={s.enabled}
                    onCheckedChange={(c) => toggle.mutate({ path: { sticker_id: s.id }, body: { enabled: c } })}
                  />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {stickers.data && <Pager total={stickers.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
