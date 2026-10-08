import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { decideReviewMutation, listReviewsOptions } from "@/client/@tanstack/react-query.gen";
import type { ReviewOut } from "@/client/types.gen";
import { Empty, OptionSelect, PageHeader, Pager, Pre, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { fmtTime } from "@/lib/format";

type Status = "pending" | "approved" | "rejected" | "expired";
const STATUS_LABELS: Record<Status, string> = { pending: "待审核", approved: "已通过", rejected: "已拒绝", expired: "已过期" };
const LIMIT = 20;

/** Best-effort plain text of a serialized BotReply (inline_text, or title + sections). */
function replyText(reply: Record<string, unknown>): string {
  if (typeof reply.inline_text === "string" && reply.inline_text) return reply.inline_text;
  const parts: string[] = [];
  if (typeof reply.title === "string" && reply.title) parts.push(reply.title);
  if (Array.isArray(reply.sections)) {
    for (const s of reply.sections) {
      if (s && typeof s === "object") {
        const { title, body } = s as { title?: unknown; body?: unknown };
        parts.push([title, body].filter((x) => typeof x === "string" && x).join("\n"));
      }
    }
  }
  return parts.join("\n\n");
}

export function ReviewsPage() {
  const [status, setStatus] = useState<Status | null>("pending");
  const [offset, setOffset] = useState(0);
  const reviews = useQuery(listReviewsOptions({ query: { status, limit: LIMIT, offset } }));
  const decide = useMutation({ ...decideReviewMutation(), onSuccess: (r) => toast.success(`R${r.id} ${STATUS_LABELS[r.status as Status] ?? r.status}`) });
  const [editing, setEditing] = useState<ReviewOut | null>(null);
  const [text, setText] = useState("");

  return (
    <div>
      <PageHeader
        title="审核队列"
        description="被审核模型拦下的回复。也可以在私聊里用 admin.review 指令处理。"
        actions={
          <OptionSelect
            className="w-36"
            value={status}
            onChange={(v) => {
              setStatus(v as Status | null);
              setOffset(0);
            }}
            noneLabel="全部"
            options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))}
          />
        }
      />
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
                  群 {r.group_id} · 用户 {r.user_id} · {fmtTime(r.created_at)}
                  {r.status === "pending" ? ` · ${fmtTime(r.expires_at)} 过期` : r.decided_by ? ` · ${r.decided_by}` : ""}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3">
              {r.request_text && (
                <div className="text-sm">
                  <span className="text-muted-foreground">请求：</span>
                  {r.request_text}
                </div>
              )}
              <Pre value={replyText(r.reply) || r.reply} className="whitespace-pre-wrap" />
              <details>
                <summary className="cursor-pointer text-sm text-muted-foreground">审核结果</summary>
                <Pre value={r.verdict} className="mt-2" />
              </details>
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
                      setText(replyText(r.reply));
                    }}
                  >
                    修改后发送
                  </Button>
                  <Button
                    variant="destructive"
                    disabled={decide.isPending}
                    onClick={() => decide.mutate({ path: { review_id: r.id }, body: { action: "reject" } })}
                  >
                    拒绝
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
      {reviews.data && <Pager total={reviews.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      {editing && (
        <Dialog open onOpenChange={(o) => !o && setEditing(null)}>
          <DialogContent className="sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>修改 R{editing.id} 后发送</DialogTitle>
            </DialogHeader>
            <Textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} />
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditing(null)}>
                取消
              </Button>
              <Button
                disabled={!text.trim() || decide.isPending}
                onClick={() =>
                  decide.mutate(
                    { path: { review_id: editing.id }, body: { action: "edit", text } },
                    { onSuccess: () => setEditing(null) },
                  )
                }
              >
                发送
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
