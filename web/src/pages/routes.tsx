import { useMutation, useQuery } from "@tanstack/react-query";
import { PencilIcon, PlusIcon } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";
import {
  createEmbedSpaceMutation,
  deleteRouteMutation,
  listEmbedSpacesOptions,
  listProvidersOptions,
  listRoutesOptions,
  reembedEmbedSpaceMutation,
  upsertRouteMutation,
} from "@/client/@tanstack/react-query.gen";
import type { ProviderOut, RouteIn, TaskRouteRow } from "@/client/types.gen";
import { ConfirmButton, Field, JsonEditor, NumberInput, OptionSelect, PageHeader, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime, isPlainObject } from "@/lib/format";

const TASK_LABELS: Record<string, string> = {
  chat: "对话",
  tldr: "TL;DR",
  quick_ack: "快速响应",
  vision: "看图",
  moderation: "审核",
  embedding: "向量",
  rerank: "重排",
  memory_topic: "话题分段",
  memory_glossary: "黑话词典",
  memory_profile: "用户画像",
  memory_digest: "每日摘要",
};

/** Text input with the provider's cached model list as suggestions; any value is allowed. */
function ModelInput({
  provider,
  value,
  onChange,
}: {
  provider: ProviderOut | undefined;
  value: string;
  onChange: (v: string) => void;
}) {
  const listId = useId();
  return (
    <>
      <Input
        list={listId}
        value={value}
        placeholder={provider?.models.length ? "选择或输入模型" : "输入模型名（可先在 Providers 拉取模型列表）"}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {provider?.models.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
    </>
  );
}

export function RoutesPage() {
  const routes = useQuery(listRoutesOptions());
  const providers = useQuery(listProvidersOptions());
  const [editing, setEditing] = useState<TaskRouteRow | null>(null);
  const remove = useMutation({ ...deleteRouteMutation(), onSuccess: () => toast.success("已删除路由") });
  const providerName = (id: number | null | undefined) =>
    providers.data?.find((p) => p.id === id)?.name ?? (id ? `#${id}` : "—");

  return (
    <div>
      <PageHeader title="模型路由" description="每个任务使用的模型、参数、兜底模型和 UsageLimits。" />
      <QueryState isLoading={routes.isLoading} error={routes.error} />
      {routes.data && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>任务</TableHead>
              <TableHead>模型</TableHead>
              <TableHead>兜底</TableHead>
              <TableHead>参数</TableHead>
              <TableHead>限制</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {routes.data.map((row) => (
              <TableRow key={row.task}>
                <TableCell>
                  <div className="font-medium">{TASK_LABELS[row.task] ?? row.task}</div>
                  <div className="font-mono text-xs text-muted-foreground">{row.task}</div>
                </TableCell>
                <TableCell>
                  {row.route ? (
                    <>
                      <div className="font-mono text-xs">{row.route.model}</div>
                      <div className="text-xs text-muted-foreground">{providerName(row.route.provider_id)}</div>
                    </>
                  ) : (
                    <Badge variant="outline">未配置</Badge>
                  )}
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {row.route?.fallback_model
                    ? `${providerName(row.route.fallback_provider_id)} / ${row.route.fallback_model}`
                    : "—"}
                </TableCell>
                <TableCell className="max-w-56 truncate font-mono text-xs">
                  {row.route && Object.keys(row.route.params ?? {}).length ? JSON.stringify(row.route.params) : "—"}
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {row.route?.limits && Object.values(row.route.limits).some((v) => v != null)
                    ? JSON.stringify(Object.fromEntries(Object.entries(row.route.limits).filter(([, v]) => v != null)))
                    : "—"}
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button size="sm" variant="outline" onClick={() => setEditing(row)}>
                      <PencilIcon /> {row.route ? "编辑" : "配置"}
                    </Button>
                    {row.route && (
                      <ConfirmButton
                        title={`删除 ${row.task} 的路由？`}
                        description="删除后该任务不可用，直到重新配置。"
                        onConfirm={() => remove.mutate({ path: { task: row.task } })}
                      >
                        删除
                      </ConfirmButton>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {editing && providers.data && (
        <RouteDialog row={editing} providers={providers.data} onClose={() => setEditing(null)} />
      )}
      <EmbedSpaces providers={providers.data ?? []} />
    </div>
  );
}

function RouteDialog({
  row,
  providers,
  onClose,
}: {
  row: TaskRouteRow;
  providers: ProviderOut[];
  onClose: () => void;
}) {
  const [form, setForm] = useState<RouteIn>({
    provider_id: row.route?.provider_id ?? providers[0]?.id ?? 0,
    model: row.route?.model ?? "",
    params: row.route?.params ?? {},
    fallback_provider_id: row.route?.fallback_provider_id ?? null,
    fallback_model: row.route?.fallback_model ?? null,
    limits: row.route?.limits ?? {},
  });
  const [paramsValid, setParamsValid] = useState(true);
  const save = useMutation({ ...upsertRouteMutation(), onSuccess: onClose });
  const providerOptions = providers.map((p) => ({ value: String(p.id), label: p.name }));
  const limits = form.limits ?? {};
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {TASK_LABELS[row.task] ?? row.task} <span className="font-mono text-sm text-muted-foreground">{row.task}</span>
          </DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Provider">
              <OptionSelect
                value={String(form.provider_id)}
                onChange={(v) => setForm({ ...form, provider_id: Number(v) })}
                options={providerOptions}
              />
            </Field>
            <Field label="模型">
              <ModelInput
                provider={providers.find((p) => p.id === form.provider_id)}
                value={form.model}
                onChange={(model) => setForm({ ...form, model })}
              />
            </Field>
            <Field label="兜底 Provider">
              <OptionSelect
                value={form.fallback_provider_id == null ? null : String(form.fallback_provider_id)}
                onChange={(v) => setForm({ ...form, fallback_provider_id: v === null ? null : Number(v) })}
                noneLabel="无"
                options={providerOptions}
              />
            </Field>
            <Field label="兜底模型">
              <ModelInput
                provider={providers.find((p) => p.id === form.fallback_provider_id)}
                value={form.fallback_model ?? ""}
                onChange={(m) => setForm({ ...form, fallback_model: m || null })}
              />
            </Field>
          </div>
          <Field label="参数" hint="temperature、max_tokens、top_p、timeout、seed、extra_body…">
            <JsonEditor
              rows={5}
              value={form.params}
              onValidityChange={setParamsValid}
              onChange={(v) => isPlainObject(v) && setForm({ ...form, params: v })}
            />
          </Field>
          <div className="grid grid-cols-3 gap-3">
            <Field label="request_limit">
              <NumberInput
                value={limits.request_limit}
                onChange={(v) => setForm({ ...form, limits: { ...limits, request_limit: v } })}
                placeholder="默认"
              />
            </Field>
            <Field label="tool_calls_limit">
              <NumberInput
                value={limits.tool_calls_limit}
                onChange={(v) => setForm({ ...form, limits: { ...limits, tool_calls_limit: v } })}
                placeholder="默认"
              />
            </Field>
            <Field label="total_tokens_limit">
              <NumberInput
                value={limits.total_tokens_limit}
                onChange={(v) => setForm({ ...form, limits: { ...limits, total_tokens_limit: v } })}
                placeholder="默认"
              />
            </Field>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={save.isPending || !form.model || !form.provider_id || !paramsValid}
            onClick={() => save.mutate({ path: { task: row.task }, body: form })}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EmbedSpaces({ providers }: { providers: ProviderOut[] }) {
  const spaces = useQuery(listEmbedSpacesOptions());
  const [providerId, setProviderId] = useState<number | null>(null);
  const [model, setModel] = useState("");
  const create = useMutation({ ...createEmbedSpaceMutation(),
      onSuccess: () => {
        toast.success("已创建，重嵌入任务已入队");
        setModel("");
      },
    });
  const reembed = useMutation({ ...reembedEmbedSpaceMutation(), onSuccess: () => toast.success("已重新入队") });
  return (
    <Card className="mt-8">
      <CardHeader>
        <CardTitle>向量空间</CardTitle>
        <CardDescription>
          更换 embedding 模型时新建一个空间，worker 会把全部记忆重新嵌入，完成后切换为生效。维度固定为 1024。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <QueryState isLoading={spaces.isLoading} error={spaces.error} />
        {!!spaces.data?.length && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>维度</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {spaces.data.map((s) => (
                <TableRow key={s.id}>
                  <TableCell>{s.id}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {s.provider_name ?? `#${s.provider_id}`} / {s.model}
                  </TableCell>
                  <TableCell>{s.dim}</TableCell>
                  <TableCell className="flex gap-1">
                    {s.active && <Badge>生效中</Badge>}
                    <Badge variant="outline">{s.status}</Badge>
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(s.created_at)}</TableCell>
                  <TableCell className="text-right">
                    {s.status !== "retired" && !s.active && (
                      <Button size="sm" variant="outline" onClick={() => reembed.mutate({ path: { space_id: s.id } })}>
                        重新入队
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Provider" className="w-48">
            <OptionSelect
              value={providerId === null ? null : String(providerId)}
              onChange={(v) => setProviderId(v === null ? null : Number(v))}
              options={providers.map((p) => ({ value: String(p.id), label: p.name }))}
            />
          </Field>
          <Field label="模型" className="w-72">
            <ModelInput provider={providers.find((p) => p.id === providerId)} value={model} onChange={setModel} />
          </Field>
          <Button
            disabled={!providerId || !model || create.isPending}
            onClick={() => providerId && create.mutate({ body: { provider_id: providerId, model } })}
          >
            <PlusIcon /> 新建并重嵌入
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
