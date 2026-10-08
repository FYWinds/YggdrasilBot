import { useMutation, useQuery } from "@tanstack/react-query";
import { PencilIcon, PlusIcon, RefreshCwIcon, ZapIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import {
  createProviderMutation,
  deleteProviderMutation,
  listProvidersOptions,
  refreshProviderModelsMutation,
  testProviderMutation,
  updateProviderMutation,
} from "@/client/@tanstack/react-query.gen";
import type { ProviderIn, ProviderOut, ProviderProbe } from "@/client/types.gen";
import { ConfirmButton, Empty, Field, JsonEditor, OptionSelect, PageHeader, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime, isPlainObject } from "@/lib/format";

function probeText(p: ProviderProbe): string {
  return p.ok
    ? `连通 · ${p.latency_ms}ms · ${p.model_count ?? 0} 个模型`
    : `失败${p.status_code ? ` (${p.status_code})` : ""} · ${p.error ?? ""}`;
}

export function ProvidersPage() {
  const providers = useQuery(listProvidersOptions());
  const [editing, setEditing] = useState<ProviderOut | "new" | null>(null);
  const test = useMutation({ ...testProviderMutation(),
      onSuccess: (p) => (p.ok ? toast.success(probeText(p)) : toast.error(probeText(p))),
    });
  const refresh = useMutation({ ...refreshProviderModelsMutation(),
      onSuccess: (r) => (r.probe.ok ? toast.success(`已拉取 ${r.probe.model_count} 个模型`) : toast.error(probeText(r.probe))),
    });
  const remove = useMutation({ ...deleteProviderMutation(), onSuccess: () => toast.success("已删除") });

  return (
    <div>
      <PageHeader
        title="Providers"
        description="OpenAI 兼容的接口（new-api、TEI、llama.cpp…）。API key 加密存储，只显示末四位。"
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon /> 新增
          </Button>
        }
      />
      <QueryState isLoading={providers.isLoading} error={providers.error} />
      {providers.data?.length === 0 && <Empty>还没有 provider</Empty>}
      {!!providers.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名称</TableHead>
              <TableHead>Base URL</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>模型</TableHead>
              <TableHead>状态</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {providers.data.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-medium">
                  {p.name}
                  {p.api_format !== "openai" && (
                    <Badge variant="secondary" className="ml-2">
                      {p.api_format}
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="font-mono text-xs">{p.base_url}</TableCell>
                <TableCell className="font-mono text-xs">{p.api_key_masked ?? "—"}</TableCell>
                <TableCell>
                  <span title={p.models.join("\n")}>{p.models.length} 个</span>
                  <div className="text-xs text-muted-foreground">{fmtTime(p.models_fetched_at)}</div>
                </TableCell>
                <TableCell>{p.enabled ? <Badge>启用</Badge> : <Badge variant="outline">停用</Badge>}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={test.isPending}
                      onClick={() => test.mutate({ path: { provider_id: p.id } })}
                    >
                      <ZapIcon /> 测试
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={refresh.isPending}
                      onClick={() => refresh.mutate({ path: { provider_id: p.id } })}
                    >
                      <RefreshCwIcon /> 拉取模型
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setEditing(p)}>
                      <PencilIcon /> 编辑
                    </Button>
                    <ConfirmButton
                      title={`删除 ${p.name}？`}
                      description="仍被模型路由或向量空间引用时无法删除。"
                      onConfirm={() => remove.mutate({ path: { provider_id: p.id } })}
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
      {editing && <ProviderDialog provider={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function ProviderDialog({ provider, onClose }: { provider: ProviderOut | null; onClose: () => void }) {
  const [form, setForm] = useState<ProviderIn>({
    name: provider?.name ?? "",
    base_url: provider?.base_url ?? "",
    api_key: "",
    clear_api_key: false,
    headers: provider?.headers ?? {},
    enabled: provider?.enabled ?? true,
    api_format: provider?.api_format === "anthropic" ? "anthropic" : "openai",
  });
  const create = useMutation({ ...createProviderMutation(), onSuccess: onClose });
  const update = useMutation({ ...updateProviderMutation(), onSuccess: onClose });
  const pending = create.isPending || update.isPending;
  const submit = () => {
    const body = { ...form, api_key: form.api_key || null };
    if (provider) update.mutate({ path: { provider_id: provider.id }, body });
    else create.mutate({ body });
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{provider ? `编辑 ${provider.name}` : "新增 provider"}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <Field label="名称">
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Base URL" hint="包含 /v1，例如 https://api.example.com/v1">
            <Input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
          </Field>
          <Field label="接口格式" hint="对话请求使用的协议；模型列表始终通过 GET {base_url}/models 拉取">
            <OptionSelect
              value={form.api_format ?? "openai"}
              onChange={(v) => setForm({ ...form, api_format: v === "anthropic" ? "anthropic" : "openai" })}
              options={[
                { value: "openai", label: "OpenAI（/chat/completions）" },
                { value: "anthropic", label: "Anthropic（/messages，原生工具调用）" },
              ]}
            />
          </Field>
          <Field
            label="API key"
            hint={provider?.api_key_masked ? `当前 ${provider.api_key_masked}；留空保持不变` : "可留空（本地服务）"}
          >
            <Input
              type="password"
              autoComplete="off"
              value={form.api_key ?? ""}
              disabled={!!form.clear_api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            />
          </Field>
          {provider?.api_key_masked && (
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={!!form.clear_api_key}
                onCheckedChange={(c) => setForm({ ...form, clear_api_key: c === true })}
              />
              清除已存储的 key
            </label>
          )}
          <Field label="额外请求头" hint='JSON 对象，例如 {"X-Foo": "bar"}'>
            <JsonEditor
              rows={3}
              value={form.headers}
              onChange={(v) => isPlainObject(v) && setForm({ ...form, headers: v as Record<string, string> })}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={form.enabled} onCheckedChange={(c) => setForm({ ...form, enabled: c })} />
            启用
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button disabled={pending || !form.name || !form.base_url} onClick={submit}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
