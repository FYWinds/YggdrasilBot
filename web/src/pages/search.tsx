import { useMutation, useQuery } from "@tanstack/react-query";
import { PencilIcon, PlusIcon, ZapIcon } from "lucide-react";
import { useState } from "react";
import {
  createSearchProviderMutation,
  deleteSearchProviderMutation,
  listSearchProvidersOptions,
  testSearchProviderMutation,
  updateSearchProviderMutation,
} from "@/client/@tanstack/react-query.gen";
import type {
  SearchKind,
  SearchProviderIn,
  SearchProviderOut,
} from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  Field,
  JsonEditor,
  NumberInput,
  OptionSelect,
  PageHeader,
  QueryState,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { isPlainObject } from "@/lib/format";

const KINDS: {
  value: SearchKind;
  label: string;
  url: string;
  params: Record<string, unknown>;
  limit: number | null;
}[] = [
  {
    value: "exa",
    label: "Exa",
    url: "留空使用 https://api.exa.ai",
    params: { type: "auto" },
    limit: 1300,
  },
  {
    value: "searxng",
    label: "SearXNG",
    url: "自建实例地址，例如 http://searxng:8080",
    params: { language: "zh-CN" },
    limit: null,
  },
];

function kindOf(kind: string) {
  return KINDS.find((k) => k.value === kind) ?? KINDS[0];
}

export function SearchPage() {
  const providers = useQuery(listSearchProvidersOptions());
  const [editing, setEditing] = useState<SearchProviderOut | "new" | null>(
    null,
  );
  const [testing, setTesting] = useState<SearchProviderOut | null>(null);
  const remove = useMutation(deleteSearchProviderMutation());

  return (
    <div>
      <PageHeader
        title="搜索源"
        description="web_search 按优先级从小到大依次尝试：报错、没有结果或本月额度用完，就换下一个。API key 加密存储，只显示末四位。"
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon /> 新增
          </Button>
        }
      />
      <QueryState isLoading={providers.isLoading} error={providers.error} />
      {providers.data?.length === 0 && (
        <Empty>还没有搜索源，web_search 会直接报错</Empty>
      )}
      {!!providers.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>优先级</TableHead>
              <TableHead>名称</TableHead>
              <TableHead>地址</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>本月用量</TableHead>
              <TableHead>状态</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {providers.data.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="tabular-nums">{p.priority}</TableCell>
                <TableCell className="font-medium">
                  {p.name}
                  <Badge variant="secondary" className="ml-2">
                    {kindOf(p.kind).label}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {p.base_url}
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {p.api_key_masked ?? "—"}
                </TableCell>
                <TableCell className="tabular-nums">
                  {p.used_this_month}
                  <span className="text-muted-foreground">
                    {" "}
                    / {p.monthly_limit ?? "不限"}
                  </span>
                </TableCell>
                <TableCell>
                  {p.enabled ? (
                    <Badge>启用</Badge>
                  ) : (
                    <Badge variant="outline">停用</Badge>
                  )}
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setTesting(p)}
                    >
                      <ZapIcon /> 测试
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setEditing(p)}
                    >
                      <PencilIcon /> 编辑
                    </Button>
                    <ConfirmButton
                      title={`删除 ${p.name}？`}
                      onConfirm={() =>
                        remove.mutate({ path: { provider_id: p.id } })
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
      {editing && (
        <SearchProviderDialog
          provider={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
      {testing && (
        <TestDialog provider={testing} onClose={() => setTesting(null)} />
      )}
    </div>
  );
}

function SearchProviderDialog({
  provider,
  onClose,
}: {
  provider: SearchProviderOut | null;
  onClose: () => void;
}) {
  const initialKind = kindOf(provider?.kind ?? "exa");
  const [form, setForm] = useState<SearchProviderIn>({
    name: provider?.name ?? "",
    kind: initialKind.value,
    base_url: provider?.base_url ?? "",
    api_key: "",
    clear_api_key: false,
    enabled: provider?.enabled ?? true,
    priority: provider?.priority ?? 100,
    monthly_limit: provider ? provider.monthly_limit : initialKind.limit,
    params: provider?.params ?? initialKind.params,
  });
  const create = useMutation({
    ...createSearchProviderMutation(),
    onSuccess: onClose,
  });
  const update = useMutation({
    ...updateSearchProviderMutation(),
    onSuccess: onClose,
  });
  const pending = create.isPending || update.isPending;
  const kind = kindOf(form.kind);
  const submit = () => {
    const body = { ...form, api_key: form.api_key || null };
    if (provider) update.mutate({ path: { provider_id: provider.id }, body });
    else create.mutate({ body });
  };
  // A new provider follows its kind's defaults until the user edits them.
  const switchKind = (value: string | null) => {
    const next = kindOf(value ?? "exa");
    setForm(
      provider
        ? { ...form, kind: next.value }
        : {
            ...form,
            kind: next.value,
            base_url: "",
            params: next.params,
            monthly_limit: next.limit,
          },
    );
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {provider ? `编辑 ${provider.name}` : "新增搜索源"}
          </DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <Field label="名称">
            <Input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </Field>
          <Field label="类型">
            <OptionSelect
              value={form.kind}
              onChange={switchKind}
              options={KINDS}
            />
          </Field>
          <Field label="地址" hint={kind.url}>
            <Input
              value={form.base_url ?? ""}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
          </Field>
          <Field
            label="API key"
            hint={
              provider?.api_key_masked
                ? `当前 ${provider.api_key_masked}；留空保持不变`
                : form.kind === "exa"
                  ? "必填"
                  : "可留空"
            }
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
                onCheckedChange={(c) =>
                  setForm({ ...form, clear_api_key: c === true })
                }
              />
              清除已存储的 key
            </label>
          )}
          <div className="grid grid-cols-2 gap-4">
            <Field label="优先级" hint="数字越小越先用">
              <NumberInput
                value={form.priority}
                onChange={(v) => setForm({ ...form, priority: v ?? 100 })}
              />
            </Field>
            <Field label="每月上限" hint="按 UTC 自然月计数，留空不限">
              <NumberInput
                value={form.monthly_limit}
                placeholder="不限"
                onChange={(v) => setForm({ ...form, monthly_limit: v })}
              />
            </Field>
          </div>
          <Field
            label="参数"
            hint='Exa：{"type": "auto"}（可选 fast、instant）；SearXNG：{"language": "zh-CN"}'
          >
            <JsonEditor
              rows={3}
              value={form.params}
              onChange={(v) =>
                isPlainObject(v) &&
                setForm({ ...form, params: v as Record<string, unknown> })
              }
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <Switch
              checked={form.enabled}
              onCheckedChange={(c) => setForm({ ...form, enabled: c })}
            />
            启用
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button disabled={pending || !form.name} onClick={submit}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TestDialog({
  provider,
  onClose,
}: {
  provider: SearchProviderOut;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("Python 最新版本");
  const test = useMutation(testSearchProviderMutation());
  const result = test.data;
  const hits = result?.hits ?? [];
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>测试 {provider.name}</DialogTitle>
        </DialogHeader>
        <div className="flex gap-2">
          <Input value={query} onChange={(e) => setQuery(e.target.value)} />
          <Button
            disabled={test.isPending || !query.trim()}
            onClick={() =>
              test.mutate({
                path: { provider_id: provider.id },
                body: { query },
              })
            }
          >
            <ZapIcon /> 搜索
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          只查这一个搜索源，会计入它的本月用量。
        </p>
        {result && !result.ok && (
          <p className="text-sm text-destructive">失败 · {result.error}</p>
        )}
        {result?.ok && (
          <div className="grid max-h-96 gap-3 overflow-auto">
            <p className="text-sm text-muted-foreground">
              {result.latency_ms}ms · {hits.length} 条结果
            </p>
            {hits.map((h) => (
              <div key={h.url} className="text-sm">
                <a
                  href={h.url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium hover:underline"
                >
                  {h.title}
                </a>
                <div className="truncate font-mono text-xs text-muted-foreground">
                  {h.url}
                </div>
                <p className="mt-1 text-xs">{h.snippet}</p>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
