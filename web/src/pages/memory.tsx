import { useMutation, useQuery } from "@tanstack/react-query";
import { endOfDay, startOfDay } from "date-fns";
import { ArchiveIcon, ArchiveRestoreIcon, LockIcon, LockOpenIcon, PencilIcon, PlusIcon } from "lucide-react";
import { useState } from "react";
import type { DateRange } from "react-day-picker";
import { toast } from "sonner";
import {
  archiveSmallTopicsMutation,
  archiveTopicMutation,
  createGlossaryTermMutation,
  deleteGlossaryTermMutation,
  deleteProfileFactMutation,
  deleteProfileMutation,
  getProfileOptions,
  listDigestsOptions,
  listGlossaryOptions,
  listPipelinesOptions,
  listProfilesOptions,
  listTopicsOptions,
  lockGlossaryTermMutation,
  updateGlossaryTermMutation,
} from "@/client/@tanstack/react-query.gen";
import type { GlossaryIn, GlossaryOut } from "@/client/types.gen";
import {
  ConfirmButton,
  DateRangePicker,
  Empty,
  Field,
  GroupSelect,
  Hint,
  NumberInput,
  OptionSelect,
  PageHeader,
  Pager,
  Pre,
  QueryState,
  TagInput,
  useGroupName,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export function MemoryPage() {
  const [groupId, setGroupId] = useState<number | null>(null);
  return (
    <div>
      <PageHeader
        title="记忆"
        description="话题时间线、每日摘要、黑话词典、用户画像和整理流水线的水位。"
        actions={<GroupSelect className="w-64" value={groupId} onChange={setGroupId} />}
      />
      <Tabs defaultValue="topics">
        <TabsList>
          <TabsTrigger value="topics">话题</TabsTrigger>
          <TabsTrigger value="digests">每日摘要</TabsTrigger>
          <TabsTrigger value="glossary">黑话词典</TabsTrigger>
          <TabsTrigger value="profiles">画像</TabsTrigger>
          <TabsTrigger value="pipelines">流水线</TabsTrigger>
        </TabsList>
        <TabsContent value="topics">
          <Topics groupId={groupId} />
        </TabsContent>
        <TabsContent value="digests">
          <Digests groupId={groupId} />
        </TabsContent>
        <TabsContent value="glossary">
          <Glossary groupId={groupId} />
        </TabsContent>
        <TabsContent value="profiles">
          <Profiles groupId={groupId} />
        </TabsContent>
        <TabsContent value="pipelines">
          <Pipelines />
        </TabsContent>
      </Tabs>
    </div>
  );
}

const TOPIC_STATUS = [
  { value: "open", label: "进行中" },
  { value: "closed", label: "已结束" },
  { value: "archived", label: "已归档" },
];

type TopicFilters = {
  status: string | null;
  q: string;
  user_id: number | null;
  min_messages: number | null;
  max_messages: number | null;
  min_participants: number | null;
  max_participants: number | null;
};

const NO_FILTERS: TopicFilters = {
  status: null,
  q: "",
  user_id: null,
  min_messages: null,
  max_messages: null,
  min_participants: null,
  max_participants: null,
};

function Topics({ groupId }: { groupId: number | null }) {
  const groupName = useGroupName();
  const [range, setRange] = useState<DateRange | undefined>();
  const [filters, setFilters] = useState<TopicFilters>(NO_FILTERS);
  const [offset, setOffset] = useState(0);
  const limit = 30;
  const topics = useQuery(
    listTopicsOptions({
      query: {
        ...filters,
        status: filters.status as "open" | "closed" | "archived" | null,
        q: filters.q.trim() || null,
        group_id: groupId,
        since: range?.from ? startOfDay(range.from).toISOString() : null,
        until: range?.from ? endOfDay(range.to ?? range.from).toISOString() : null,
        limit,
        offset,
      },
    }),
  );
  const archive = useMutation(archiveTopicMutation());
  const archiveSmall = useMutation({
    ...archiveSmallTopicsMutation(),
    onSuccess: (r) => toast.success(`归档了 ${r.archived} 个话题`),
  });
  const set = (patch: Partial<TopicFilters>) => {
    setFilters({ ...filters, ...patch });
    setOffset(0);
  };
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="时间范围">
          <DateRangePicker
            value={range}
            onChange={(v) => {
              setRange(v);
              setOffset(0);
            }}
          />
        </Field>
        <Field label="状态">
          <OptionSelect
            className="w-28"
            value={filters.status}
            onChange={(status) => set({ status })}
            options={TOPIC_STATUS}
            noneLabel="全部"
          />
        </Field>
        <Field label="关键词">
          <Input
            className="w-48"
            placeholder="标题、摘要、关键词"
            value={filters.q}
            onChange={(e) => set({ q: e.target.value })}
          />
        </Field>
        <Field label="参与人 QQ">
          <NumberInput className="w-32" value={filters.user_id} onChange={(user_id) => set({ user_id })} />
        </Field>
        <Field label="消息数">
          <div className="flex items-center gap-1">
            <NumberInput
              className="w-20"
              placeholder="最少"
              value={filters.min_messages}
              onChange={(v) => set({ min_messages: v })}
            />
            –
            <NumberInput
              className="w-20"
              placeholder="最多"
              value={filters.max_messages}
              onChange={(v) => set({ max_messages: v })}
            />
          </div>
        </Field>
        <Field label="人数">
          <div className="flex items-center gap-1">
            <NumberInput
              className="w-20"
              placeholder="最少"
              value={filters.min_participants}
              onChange={(v) => set({ min_participants: v })}
            />
            –
            <NumberInput
              className="w-20"
              placeholder="最多"
              value={filters.max_participants}
              onChange={(v) => set({ max_participants: v })}
            />
          </div>
        </Field>
        <Button variant="ghost" onClick={() => set(NO_FILTERS)}>
          清空筛选
        </Button>
        <div className="ml-auto">
          <Hint content={groupId === null ? "先在右上角选择群" : null}>
            <span>
              {groupId === null ? (
                <Button variant="outline" disabled>
                  <ArchiveIcon /> 按阈值归档
                </Button>
              ) : (
                <ConfirmButton
                  variant="outline"
                  size="default"
                  title={`按当前阈值整理群 ${groupId} 的已结束话题？`}
                  description="消息数和参与人数都低于阈值（设置 → memory.topic_archive_below_*）的已结束话题会被归档，agent 搜不到，这里仍可查看。手动恢复过的话题也会再次归档。"
                  onConfirm={() => archiveSmall.mutate({ body: { group_id: groupId } })}
                >
                  <ArchiveIcon /> 按阈值归档
                </ConfirmButton>
              )}
            </span>
          </Hint>
        </div>
      </div>
      <QueryState isLoading={topics.isLoading} error={topics.error} />
      {topics.data?.items.length === 0 && <Empty />}
      <ol className="relative grid gap-4 border-l pl-6">
        {topics.data?.items.map((t) => (
          <li key={t.id} className="relative flex items-start gap-4">
            <span
              className={cn(
                "absolute top-1.5 -left-[1.85rem] size-3 rounded-full border-2 border-background",
                t.status === "archived" ? "bg-muted-foreground/40" : "bg-primary",
              )}
            />
            <div className={cn("min-w-0 flex-1", t.status === "archived" && "text-muted-foreground")}>
              <div className="text-xs text-muted-foreground">
                {fmtTime(t.start_at)} — {fmtTime(t.end_at)}
                {groupId === null && ` · ${groupName(t.group_id)}`} · {t.msg_count} 条 · {t.participants.length} 人
              </div>
              <div className="flex items-center gap-2 font-medium">
                {t.title}
                {t.status === "open" && <Badge variant="outline">进行中</Badge>}
                {t.status === "archived" && (
                  <Badge variant="outline" className="border-dashed text-muted-foreground">
                    <ArchiveIcon /> 已归档 · agent 不可见
                  </Badge>
                )}
                {t.dirty && <Badge variant="destructive">待重算</Badge>}
              </div>
              <p className="text-sm whitespace-pre-wrap">{t.summary}</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {t.keywords.map((k) => (
                  <Badge key={k} variant="secondary">
                    {k}
                  </Badge>
                ))}
              </div>
            </div>
            {t.status !== "open" && (
              <Button
                size="sm"
                variant={t.status === "archived" ? "default" : "outline"}
                className="shrink-0"
                disabled={archive.isPending}
                onClick={() =>
                  archive.mutate({ path: { topic_id: t.id }, body: { archived: t.status !== "archived" } })
                }
              >
                {t.status === "archived" ? <ArchiveRestoreIcon /> : <ArchiveIcon />}
                {t.status === "archived" ? "恢复" : "归档"}
              </Button>
            )}
          </li>
        ))}
      </ol>
      {topics.data && <Pager total={topics.data.total} limit={limit} offset={offset} onChange={setOffset} />}
    </div>
  );
}

function Digests({ groupId }: { groupId: number | null }) {
  const groupName = useGroupName();
  const [offset, setOffset] = useState(0);
  const limit = 14;
  const digests = useQuery(listDigestsOptions({ query: { group_id: groupId, limit, offset } }));
  return (
    <div className="grid gap-4">
      <QueryState isLoading={digests.isLoading} error={digests.error} />
      {digests.data?.items.length === 0 && <Empty />}
      {digests.data?.items.map((d) => (
        <Card key={d.id}>
          <CardHeader>
            <CardTitle className="text-base">
              {d.day}
              {groupId === null && <span className="ml-2 text-sm font-normal text-muted-foreground">{groupName(d.group_id)}</span>}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm whitespace-pre-wrap">{d.summary}</CardContent>
        </Card>
      ))}
      {digests.data && <Pager total={digests.data.total} limit={limit} offset={offset} onChange={setOffset} />}
    </div>
  );
}

function Glossary({ groupId }: { groupId: number | null }) {
  const groupName = useGroupName();
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 50;
  const terms = useQuery(
    listGlossaryOptions({
      query: { group_id: groupId, q: q || null, limit, offset },
    }),
  );
  const lock = useMutation(lockGlossaryTermMutation());
  const remove = useMutation({
    ...deleteGlossaryTermMutation(),
    onSuccess: () => toast.success("已删除"),
  });
  const [editing, setEditing] = useState<GlossaryOut | "new" | null>(null);
  return (
    <div className="grid gap-4">
      <div className="flex items-center gap-3">
        <Input className="w-72" placeholder="搜索词条或释义" value={q} onChange={(e) => setQ(e.target.value)} />
        {/* span: a disabled button gets no pointer events, so the hint needs a wrapper */}
        <Hint content={groupId === null ? "先在右上角选择群" : null}>
          <span>
            <Button onClick={() => setEditing("new")} disabled={groupId === null}>
              <PlusIcon /> 新增词条
            </Button>
          </span>
        </Hint>
      </div>
      <QueryState isLoading={terms.isLoading} error={terms.error} />
      {terms.data?.items.length === 0 && <Empty />}
      {!!terms.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>词条</TableHead>
              <TableHead>释义</TableHead>
              <TableHead>命中</TableHead>
              <TableHead>来源</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {terms.data.items.map((t) => (
              <TableRow key={t.id}>
                <TableCell>
                  <div className="font-medium">{t.term}</div>
                  {!!t.aliases.length && <div className="text-xs text-muted-foreground">{t.aliases.join("、")}</div>}
                  {groupId === null && <div className="text-xs text-muted-foreground">{groupName(t.group_id)}</div>}
                </TableCell>
                <TableCell className="max-w-xl text-sm whitespace-pre-wrap">{t.definition}</TableCell>
                <TableCell>
                  {t.hits}
                  <div className="text-xs text-muted-foreground">置信 {t.confidence.toFixed(2)}</div>
                </TableCell>
                <TableCell>
                  <Badge variant={t.source === "admin" ? "default" : "outline"}>{t.source}</Badge>
                  {t.locked && <LockIcon className="ml-1 inline size-3.5" />}
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button size="sm" variant="outline" onClick={() => setEditing(t)}>
                      <PencilIcon /> 编辑
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        lock.mutate({
                          path: { term_id: t.id },
                          body: { locked: !t.locked },
                        })
                      }
                    >
                      {t.locked ? <LockOpenIcon /> : <LockIcon />} {t.locked ? "解锁" : "锁定"}
                    </Button>
                    <ConfirmButton
                      title={`删除「${t.term}」？`}
                      onConfirm={() => remove.mutate({ path: { term_id: t.id } })}
                    >
                      删除
                    </ConfirmButton>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {terms.data && <Pager total={terms.data.total} limit={limit} offset={offset} onChange={setOffset} />}
      {editing && (
        <GlossaryDialog
          term={editing === "new" ? null : editing}
          groupId={editing === "new" ? (groupId ?? 0) : editing.group_id}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

function GlossaryDialog({
  term,
  groupId,
  onClose,
}: {
  term: GlossaryOut | null;
  groupId: number;
  onClose: () => void;
}) {
  const [form, setForm] = useState<GlossaryIn>({
    group_id: groupId,
    term: term?.term ?? "",
    aliases: term?.aliases ?? [],
    definition: term?.definition ?? "",
  });
  const create = useMutation({
    ...createGlossaryTermMutation(),
    onSuccess: onClose,
  });
  const update = useMutation({
    ...updateGlossaryTermMutation(),
    onSuccess: onClose,
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{term ? `编辑「${term.term}」` : `新增词条（群 ${groupId}）`}</DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted-foreground">管理员编辑的词条会被锁定，整理流水线不再覆盖。</p>
        <div className="grid gap-4">
          <Field label="词条">
            <Input value={form.term} onChange={(e) => setForm({ ...form, term: e.target.value })} />
          </Field>
          <Field label="别名">
            <TagInput value={form.aliases ?? []} onChange={(aliases) => setForm({ ...form, aliases })} />
          </Field>
          <Field label="释义">
            <Textarea
              rows={5}
              value={form.definition}
              onChange={(e) => setForm({ ...form, definition: e.target.value })}
            />
          </Field>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={!form.term.trim() || !form.definition.trim() || create.isPending || update.isPending}
            onClick={() =>
              term ? update.mutate({ path: { term_id: term.id }, body: form }) : create.mutate({ body: form })
            }
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Profiles({ groupId }: { groupId: number | null }) {
  const groupName = useGroupName();
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 50;
  const profiles = useQuery(
    listProfilesOptions({
      query: { group_id: groupId, q: q || null, limit, offset },
    }),
  );
  const [selected, setSelected] = useState<{
    group_id: number;
    user_id: number;
  } | null>(null);
  const remove = useMutation({
    ...deleteProfileMutation(),
    onSuccess: () => {
      toast.success("已删除画像");
      setSelected(null);
    },
  });
  return (
    <div className="grid gap-4">
      <Input className="w-72" placeholder="搜索昵称、QQ 号或摘要" value={q} onChange={(e) => setQ(e.target.value)} />
      <QueryState isLoading={profiles.isLoading} error={profiles.error} />
      {profiles.data?.items.length === 0 && <Empty />}
      {!!profiles.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>用户</TableHead>
              <TableHead>摘要</TableHead>
              <TableHead>兴趣</TableHead>
              <TableHead>更新</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {profiles.data.items.map((p) => (
              <TableRow key={`${p.group_id}:${p.user_id}`}>
                <TableCell>
                  <div className="font-medium">{p.display_name || p.user_id}</div>
                  {groupId === null && <div className="text-xs text-muted-foreground">{groupName(p.group_id)}</div>}
                  {p.opted_out && <Badge variant="outline">已退出记忆</Badge>}
                </TableCell>
                {/* TableCell defaults to nowrap; long profile text must wrap and stay short (full text in 查看). */}
                <TableCell className="min-w-80 max-w-xl text-sm whitespace-normal">
                  <div className="line-clamp-3">{p.summary}</div>
                </TableCell>
                <TableCell className="min-w-40 max-w-xs text-xs whitespace-normal">
                  <div className="line-clamp-3">{p.interests.join("、")}</div>
                </TableCell>
                <TableCell className="text-xs">{fmtTime(p.updated_at)}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        setSelected({
                          group_id: p.group_id,
                          user_id: p.user_id,
                        })
                      }
                    >
                      查看
                    </Button>
                    <ConfirmButton
                      title={`删除 ${p.display_name || p.user_id} 的画像？`}
                      description="同时删除画像下的全部事实。"
                      onConfirm={() =>
                        remove.mutate({
                          path: { group_id: p.group_id, user_id: p.user_id },
                        })
                      }
                    >
                      删除
                    </ConfirmButton>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {profiles.data && <Pager total={profiles.data.total} limit={limit} offset={offset} onChange={setOffset} />}
      {selected && <ProfileDialog {...selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function ProfileDialog({ group_id, user_id, onClose }: { group_id: number; user_id: number; onClose: () => void }) {
  const groupName = useGroupName();
  const profile = useQuery(getProfileOptions({ path: { group_id, user_id } }));
  const removeFact = useMutation(deleteProfileFactMutation());
  const p = profile.data;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {p?.display_name || user_id}{" "}
            <span className="text-sm font-normal text-muted-foreground">
              {groupName(group_id)} · <span className="font-mono">{user_id}</span>
            </span>
          </DialogTitle>
        </DialogHeader>
        <QueryState isLoading={profile.isLoading} error={profile.error} />
        {p && (
          <div className="grid gap-3 text-sm">
            <p className="whitespace-pre-wrap">{p.summary || "（无摘要）"}</p>
            <div>
              <span className="text-muted-foreground">兴趣：</span>
              {p.interests.join("、") || "—"}
            </div>
            <div>
              <span className="text-muted-foreground">特点：</span>
              {p.traits.join("、") || "—"}
            </div>
            <div className="font-medium">事实</div>
            {p.facts.length === 0 && <Empty>没有记录事实</Empty>}
            <div className="grid max-h-80 gap-1.5 overflow-auto">
              {p.facts.map((f) => (
                <div key={f.id} className="flex items-start justify-between gap-2 rounded-md border px-3 py-2">
                  <div>
                    {f.fact}
                    <div className="text-xs text-muted-foreground">{fmtTime(f.created_at)}</div>
                  </div>
                  <Button
                    size="xs"
                    variant="ghost"
                    onClick={() =>
                      removeFact.mutate({
                        path: { group_id, user_id, fact_id: f.id },
                      })
                    }
                  >
                    删除
                  </Button>
                </div>
              ))}
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            关闭
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Pipelines() {
  const pipelines = useQuery(listPipelinesOptions());
  return (
    <div className="grid gap-4">
      <QueryState isLoading={pipelines.isLoading} error={pipelines.error} />
      {pipelines.data?.length === 0 && <Empty>流水线还没有运行过</Empty>}
      {!!pipelines.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>流水线</TableHead>
              <TableHead>群</TableHead>
              <TableHead>水位（消息 id）</TableHead>
              <TableHead>积压</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>更新</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pipelines.data.map((p) => (
              <TableRow key={`${p.pipeline}:${p.group_id}`}>
                <TableCell className="font-mono">{p.pipeline}</TableCell>
                <TableCell className="font-mono">{p.group_id}</TableCell>
                <TableCell className="font-mono">{p.last_msg_id}</TableCell>
                <TableCell>{p.backlog >= 10000 ? "≥10000" : p.backlog}</TableCell>
                <TableCell>
                  {Object.keys(p.state).length ? <Pre value={p.state} className="max-h-32" /> : "—"}
                </TableCell>
                <TableCell className="text-xs">{fmtTime(p.updated_at)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
