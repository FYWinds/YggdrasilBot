import { useMutation, useQuery } from "@tanstack/react-query";
import { ListIcon, PencilIcon, PlusIcon, RefreshCwIcon, ServerIcon, ZapIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  createProviderMutation,
  deleteProviderMutation,
  getProviderBalanceQueryKey,
  listProvidersOptions,
  refreshProviderModelsMutation,
  setProviderAliasesMutation,
  testProviderMutation,
  updateProviderMutation,
} from "@/client/@tanstack/react-query.gen";
import { getProviderBalance } from "@/client/sdk.gen";
import type { ProviderIn, ProviderOut, ProviderProbe } from "@/client/types.gen";
import { BALANCE_QUERY, BalanceView, balanceResult } from "@/components/balance";
import { CapBadges } from "@/components/model-caps";
import {
  ApiKeyField,
  ConfirmButton,
  Empty,
  Field,
  FormDialog,
  Hint,
  JsonEditor,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  const [modelsOf, setModelsOf] = useState<number | null>(null);
  const modelsProvider = providers.data?.find((p) => p.id === modelsOf);
  const test = useMutation({ ...testProviderMutation(),
      onSuccess: (p) => (p.ok ? toast.success(probeText(p)) : toast.error(probeText(p))),
    });
  const refresh = useMutation({ ...refreshProviderModelsMutation(),
      onSuccess: (r) => {
        if (!r.probe.ok) return toast.error(probeText(r.probe));
        const withCaps = Object.keys(r.provider.model_caps).length;
        toast.success(`已拉取 ${r.probe.model_count} 个模型，${withCaps} 个匹配到能力信息`);
        if (r.catalog_error) toast.warning(r.catalog_error);
      },
    });
  const remove = useMutation({ ...deleteProviderMutation(), onSuccess: () => toast.success("已删除") });
  const addButton = (
    <Button onClick={() => setEditing("new")}>
      <PlusIcon data-icon="inline-start" /> 新增
    </Button>
  );

  return (
    <div>
      <PageHeader
        title="Providers"
        description="OpenAI 兼容的接口（new-api、TEI、llama.cpp…）。API key 加密存储，只显示末四位。"
        actions={addButton}
      />
      <QueryState isLoading={providers.isLoading} error={providers.error} />
      {providers.data?.length === 0 && (
        <Empty icon={<ServerIcon />} description="模型路由和向量空间都要先选一个 provider。" action={addButton}>
          还没有 provider
        </Empty>
      )}
      {!!providers.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名称</TableHead>
              <TableHead>Base URL</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>模型</TableHead>
              <TableHead>余额</TableHead>
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
                  <Hint content={modelsHint(p)}>
                    <Button variant="link" size="sm" className="h-auto p-0" onClick={() => setModelsOf(p.id)}>
                      {p.models.length} 个
                    </Button>
                  </Hint>
                </TableCell>
                <TableCell>
                  <BalanceCell provider={p} />
                </TableCell>
                <TableCell>{p.enabled ? <Badge>启用</Badge> : <Badge variant="outline">停用</Badge>}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <PendingButton
                      size="sm"
                      variant="outline"
                      pending={test.isPending && test.variables?.path.provider_id === p.id}
                      disabled={test.isPending}
                      onClick={() => test.mutate({ path: { provider_id: p.id } })}
                    >
                      {!(test.isPending && test.variables?.path.provider_id === p.id) && (
                        <ZapIcon data-icon="inline-start" />
                      )}
                      测试
                    </PendingButton>
                    <PendingButton
                      size="sm"
                      variant="outline"
                      pending={refresh.isPending && refresh.variables?.path.provider_id === p.id}
                      disabled={refresh.isPending}
                      onClick={() => refresh.mutate({ path: { provider_id: p.id } })}
                    >
                      {!(refresh.isPending && refresh.variables?.path.provider_id === p.id) && (
                        <RefreshCwIcon data-icon="inline-start" />
                      )}
                      拉取模型
                    </PendingButton>
                    <Button size="sm" variant="outline" onClick={() => setModelsOf(p.id)}>
                      <ListIcon data-icon="inline-start" /> 模型与别名
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setEditing(p)}>
                      <PencilIcon data-icon="inline-start" /> 编辑
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
      {modelsProvider && <ModelsDialog provider={modelsProvider} onClose={() => setModelsOf(null)} />}
    </div>
  );
}

function ProviderDialog({ provider, onClose }: { provider: ProviderOut | null; onClose: () => void }) {
  const [form, setForm] = useState<ProviderIn>({
    name: provider?.name ?? "",
    base_url: provider?.base_url ?? "",
    api_key: "",
    clear_api_key: false,
    balance_key: "",
    clear_balance_key: false,
    headers: provider?.headers ?? {},
    enabled: provider?.enabled ?? true,
    api_format: provider?.api_format === "anthropic" ? "anthropic" : "openai",
  });
  const create = useMutation({ ...createProviderMutation(), onSuccess: onClose });
  const update = useMutation({ ...updateProviderMutation(), onSuccess: onClose });
  const submit = () => {
    const body = { ...form, api_key: form.api_key || null, balance_key: form.balance_key || null };
    if (provider) update.mutate({ path: { provider_id: provider.id }, body });
    else create.mutate({ body });
  };
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={provider ? `编辑 ${provider.name}` : "新增 provider"}
      description="OpenAI 兼容的接口地址和凭据；保存后可以在列表里测试连通、拉取模型。"
      onSubmit={submit}
      pending={create.isPending || update.isPending}
      submitDisabled={!form.name || !form.base_url}
    >
      <Field label="名称">
        <Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <Field label="Base URL" hint="包含 /v1，例如 https://api.example.com/v1">
        <Input required value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
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
      <ApiKeyField
        masked={provider?.api_key_masked}
        value={form.api_key ?? ""}
        clear={!!form.clear_api_key}
        emptyHint="可留空（本地服务）"
        onChange={(patch) => setForm({ ...form, ...patch })}
      />
      <ApiKeyField
        label="余额查询 key（可选）"
        clearLabel="清除已存储的余额查询 key"
        masked={provider?.balance_key_masked}
        value={form.balance_key ?? ""}
        clear={!!form.clear_balance_key}
        emptyHint="只用于查询余额，可留空"
        note="OpenRouter 填管理 key（management key）显示账户余额；不填则显示 API key 自身的额度与用量"
        onChange={({ api_key, clear_api_key }) =>
          setForm({
            ...form,
            ...(api_key !== undefined && { balance_key: api_key }),
            ...(clear_api_key !== undefined && { clear_balance_key: clear_api_key }),
          })
        }
      />
      <Field label="额外请求头" hint='JSON 对象，例如 {"X-Foo": "bar"}'>
        <JsonEditor
          rows={3}
          value={form.headers}
          onChange={(v) => isPlainObject(v) && setForm({ ...form, headers: v as Record<string, string> })}
        />
      </Field>
      <Field label="启用" orientation="horizontal">
        <Switch checked={form.enabled} onCheckedChange={(c) => setForm({ ...form, enabled: c })} />
      </Field>
    </FormDialog>
  );
}

/** Lazily fetched balance of one provider (disabled providers are not queried). */
function BalanceCell({ provider }: { provider: ProviderOut }) {
  const path = { provider_id: provider.id };
  const balance = useQuery({
    queryKey: getProviderBalanceQueryKey({ path }),
    queryFn: ({ signal }) => balanceResult(getProviderBalance({ path, signal })),
    enabled: provider.enabled,
    ...BALANCE_QUERY,
  });
  return <BalanceView query={balance} enabled={provider.enabled} />;
}

// Model ids shown in the list's hover hint; the dialog has the full, searchable list.
const HINT_EXAMPLES = 5;
// Rows the models dialog renders at once (OpenRouter lists 500+ models); searching narrows the rest.
const MAX_ROWS = 100;

function modelsHint(p: ProviderOut): string {
  const more = p.models.length > HINT_EXAMPLES ? `\n…共 ${p.models.length} 个，点开查看全部` : "";
  return `${p.models.slice(0, HINT_EXAMPLES).join("\n")}${more}\n\n更新于 ${fmtTime(p.models_fetched_at)}`;
}

/** Per-model capabilities (models.dev or the provider's own /models) and aliases for ids the automatic match misses. */
function ModelsDialog({ provider, onClose }: { provider: ProviderOut; onClose: () => void }) {
  const [aliases, setAliases] = useState<Record<string, string>>(provider.model_aliases);
  const [extra, setExtra] = useState("");
  const [search, setSearch] = useState("");
  const ids = useMemo(
    () => [...new Set([...provider.models, ...Object.keys(aliases)])].sort(),
    [provider.models, aliases],
  );
  const query = search.trim().toLowerCase();
  // By id only: matching the aliases would drop a row while its alias is being edited.
  const matched = useMemo(() => (query ? ids.filter((m) => m.toLowerCase().includes(query)) : ids), [ids, query]);
  const rows = matched.slice(0, MAX_ROWS);
  const save = useMutation({
    ...setProviderAliasesMutation(),
    onSuccess: (r) => {
      if (r.catalog_error) toast.warning(r.catalog_error);
      else if (r.unmatched?.length) toast.warning(`这些别名在 models.dev 里找不到：${r.unmatched.join("、")}`);
      else toast.success("已保存别名并更新能力信息");
    },
  });
  const extraName = extra.trim();
  const canAdd = !!extraName && !ids.includes(extraName);
  const addExtra = () => {
    if (!canAdd) return;
    setAliases({ ...aliases, [extraName]: "" });
    setExtra("");
  };
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`${provider.name} 的模型能力`}
      description="能力信息来自 models.dev，模型名去掉网关前缀后自动匹配（cvp/claude-sonnet-5-5 → claude-sonnet-5-5）；OpenRouter 这类在 /models 里自带能力的 provider 直接用上游信息。匹配不上或匹配错了，就填别名（优先于两者），例如 anthropic/claude-sonnet-5-5。"
      onSubmit={() => save.mutate({ path: { provider_id: provider.id }, body: { aliases } })}
      pending={save.isPending}
      submitLabel="保存别名"
      className="sm:max-w-4xl"
    >
      {ids.length > 0 && (
        <Input
          aria-label="搜索模型"
          placeholder={`搜索 ${ids.length} 个模型`}
          className="font-mono text-xs"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => {
            // Enter only filters here instead of submitting the aliases.
            if (e.key === "Enter") e.preventDefault();
          }}
        />
      )}
      {ids.length ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>模型</TableHead>
              <TableHead>能力</TableHead>
              <TableHead className="w-64">别名（models.dev id）</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((m) => (
              <TableRow key={m}>
                <TableCell className="font-mono text-xs">{m}</TableCell>
                <TableCell>
                  <CapBadges caps={provider.model_caps[m]} showId />
                </TableCell>
                <TableCell>
                  <Input
                    aria-label={`${m} 的别名`}
                    className="h-8 font-mono text-xs"
                    value={aliases[m] ?? ""}
                    placeholder="自动匹配"
                    onChange={(e) => setAliases({ ...aliases, [m]: e.target.value })}
                  />
                </TableCell>
              </TableRow>
            ))}
            {!rows.length && (
              <TableRow>
                <TableCell colSpan={3} className="text-center text-muted-foreground">
                  没有匹配「{search.trim()}」的模型
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      ) : (
        <Empty description="先在列表里拉取模型，或在下面添加路由里手填的模型名。">还没有模型</Empty>
      )}
      {matched.length > rows.length && (
        <p className="text-xs text-muted-foreground">
          只显示前 {rows.length} 个，还有 {matched.length - rows.length} 个，输入以筛选
        </p>
      )}
      <div className="flex items-end gap-2">
        <Field label="添加不在列表里的模型（路由里手填的）" className="flex-1">
          <Input
            className="font-mono text-xs"
            value={extra}
            onChange={(e) => setExtra(e.target.value)}
            onKeyDown={(e) => {
              // Enter adds the row here instead of submitting the aliases.
              if (e.key === "Enter") {
                e.preventDefault();
                addExtra();
              }
            }}
          />
        </Field>
        <Button type="button" variant="outline" disabled={!canAdd} onClick={addExtra}>
          <PlusIcon data-icon="inline-start" /> 添加
        </Button>
      </div>
    </FormDialog>
  );
}
