import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import { decideReviewMutation, listReviewsOptions } from "@/client/@tanstack/react-query.gen";
import type { ReviewOut } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  FilterBar,
  FormDialog,
  GroupSelect,
  OptionSelect,
  PageHeader,
  Pager,
  QueryState,
  useGroupName,
} from "@/components/common";
import { ReplyView } from "@/components/qq";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { fmtTime } from "@/lib/format";

type Status = "pending" | "approved" | "rejected" | "expired";
const STATUS_LABELS: Record<Status, string> = { pending: "待审核", approved: "已通过", rejected: "已拒绝", expired: "已过期" };
const LIMIT = 20;

export function ReviewsPage() {
  const [status, setStatus] = useState<Status | null>("pending");
  const [groupId, setGroupId] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const reviews = useQuery(listReviewsOptions({ query: { status, group_id: groupId, limit: LIMIT, offset } }));
  const decide = useMutation({ ...decideReviewMutation(), onSuccess: (r) => toast.success(`R${r.id} ${STATUS_LABELS[r.status as Status] ?? r.status}`) });
  const [editing, setEditing] = useState<ReviewOut | null>(null);
  const [text, setText] = useState("");
  const groupName = useGroupName();

  return (
    <div>
      <PageHeader title="审核队列" description="被审核模型拦下的回复。也可以在私聊里用 admin.review 指令处理。" />
      <FilterBar>
        <GroupSelect
          className="w-56"
          value={groupId}
          onChange={(v) => {
            setGroupId(v);
            setOffset(0);
          }}
        />
        <OptionSelect
          className="w-36"
          aria-label="状态"
          value={status}
          onChange={(v) => {
            setStatus(v as Status | null);
            setOffset(0);
          }}
          noneLabel="全部"
          options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))}
        />
      </FilterBar>
      <QueryState isLoading={reviews.isLoading} error={reviews.error} />
      {reviews.data?.items.length === 0 && <Empty>队列是空的</Empty>}
      <div className="grid gap-4">
        {reviews.data?.items.map((r) => (
          <Card key={r.id}>
            <CardHeader>
              <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                <span className="font-mono">R{r.id}</span>
                <Badge variant={r.status === "pending" ? "default" : "outline"}>
                  {STATUS_LABELS[r.status as Status] ?? r.status}
                </Badge>
                <span className="text-sm font-normal text-muted-foreground">
                  {groupName(r.group_id)} · {r.user_id} · {fmtTime(r.created_at)}
                  {r.status === "pending" ? ` · ${fmtTime(r.expires_at)} 过期` : r.decided_by ? ` · ${r.decided_by}` : ""}
                </span>
                {r.invocation_id != null && (
                  <Button variant="link" asChild className="ml-auto h-auto p-0 text-sm font-normal">
                    <Link to="/invocations/$id" params={{ id: String(r.invocation_id) }}>
                      调用 #{r.invocation_id}
                    </Link>
                  </Button>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3">
              {r.request_text && (
                <div className="text-sm">
                  <span className="text-muted-foreground">请求：</span>
                  {r.request_text}
                </div>
              )}
              <ReplyView reply={r.reply} defaultOpen />
              <div className="flex flex-wrap items-center gap-1 text-sm">
                <span className="text-muted-foreground">审核结果：</span>
                <Badge variant="destructive">{r.verdict.verdict}</Badge>
                <Badge variant="outline" className="font-mono">
                  {r.verdict.score.toFixed(2)}
                </Badge>
                {(r.verdict.categories ?? []).map((c) => (
                  <Badge key={c} variant="secondary">
                    {c}
                  </Badge>
                ))}
                <Badge variant="outline">{r.verdict.action}</Badge>
              </div>
              {r.status === "pending" && (
                <div className="flex gap-2">
                  <Button
                    disabled={decide.isPending}
                    onClick={() => decide.mutate({ path: { review_id: r.id }, body: { action: "approve" } })}
                  >
                    通过并发送
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setEditing(r);
                      setText([r.reply.title, ...(r.reply.sections ?? []).map((s) => s.content)].filter(Boolean).join("\n\n"));
                    }}
                  >
                    修改后发送
                  </Button>
                  <ConfirmButton
                    size="default"
                    title={`拒绝 R${r.id}？`}
                    description="这条回复不会发送。"
                    disabled={decide.isPending}
                    onConfirm={() => decide.mutate({ path: { review_id: r.id }, body: { action: "reject" } })}
                  >
                    拒绝
                  </ConfirmButton>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
      {reviews.data && <Pager total={reviews.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      {editing && (
        <FormDialog
          open
          onOpenChange={(o) => !o && setEditing(null)}
          className="sm:max-w-2xl"
          title={`修改 R${editing.id} 后发送`}
          description="编辑后的文本会代替原回复发送到群里。"
          submitLabel="发送"
          pending={decide.isPending}
          submitDisabled={!text.trim()}
          onSubmit={() =>
            decide.mutate(
              { path: { review_id: editing.id }, body: { action: "edit", text } },
              { onSuccess: () => setEditing(null) },
            )
          }
        >
          <Textarea rows={12} aria-label="回复文本" value={text} onChange={(e) => setText(e.target.value)} />
        </FormDialog>
      )}
    </div>
  );
}
