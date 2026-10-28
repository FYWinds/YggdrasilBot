import { useMutation, useQuery } from "@tanstack/react-query";
import { GlobeIcon, PencilIcon, PlusIcon, SearchIcon, ZapIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import {
  createSearchProviderMutation,
  deleteSearchProviderMutation,
  getSearchProviderBalanceQueryKey,
  listSearchProvidersOptions,
  testSearchProviderMutation,
  updateSearchProviderMutation,
} from "@/client/@tanstack/react-query.gen";
import { getSearchProviderBalance } from "@/client/sdk.gen";
import type { SearchKind, SearchProviderIn, SearchProviderOut, SearchTestHit } from "@/client/types.gen";
import { BALANCE_QUERY, BalanceView, balanceResult } from "@/components/balance";
import {
  ApiKeyField,
  ConfirmButton,
  Empty,
  ExternalLink,
  Field,
  FormDialog,
  JsonEditor,
  NumberInput,
  OptionSelect,
  PageHeader,
  QueryState,
} from "@/components/common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  Trace,
  TraceCollapsible,
  TraceCollapsibleContent,
  TraceCollapsibleTrigger,
  TraceDescription,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace";
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
    value: "deepseek",
    label: "DeepSeek（也给 read_url 远程抓取用）",
    url: "留空使用 https://api.deepseek.com",
    params: {},
    limit: null,
  },
  {
    value: "searxng",
    label: "SearXNG",
    url: "自建实例地址，例如 http://searxng:8080",
    params: { language: "zh-CN" },
    limit: null,
  },
  {
    value: "x",
    label: "X API（给 search_x / read_x 用，不参与 web_search）",
    url: "留空使用 https://api.x.com；key 填 App 的 Bearer Token",
    params: {},
    limit: 300,
  },
];

function kindOf(kind: string) {
  return KINDS.find((k) => k.value === kind) ?? KINDS[0];
}

export function SearchPage() {
  const providers = useQuery(listSearchProvidersOptions());
  const [editing, setEditing] = useState<SearchProviderOut | "new" | null>(null);
  const [testing, setTesting] = useState<SearchProviderOut | null>(null);
  const remove = useMutation({ ...deleteSearchProviderMutation(), onSuccess: () => toast.success("已删除") });
  const addButton = (
    <Button onClick={() => setEditing("new")}>
      <PlusIcon data-icon="inline-start" /> 新增
    </Button>
  );

  return (
    <div>
      <PageHeader
        title="搜索源"
        description="web_search 按优先级从小到大依次尝试：报错、没有结果或本月额度用完，就换下一个。API key 加密存储，只显示末四位。"
        actions={addButton}
      />
      <QueryState isLoading={providers.isLoading} error={providers.error} />
      {providers.data?.length === 0 && (
        <Empty icon={<SearchIcon />} description="没有搜索源时 web_search 会直接报错。" action={addButton}>
          还没有搜索源
        </Empty>
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
              <TableHead>余额</TableHead>
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
                <TableCell className="font-mono text-xs">{p.base_url}</TableCell>
                <TableCell className="font-mono text-xs">{p.api_key_masked ?? "—"}</TableCell>
                <TableCell className="tabular-nums">
                  {p.used_this_month}
                  <span className="text-muted-foreground"> / {p.monthly_limit ?? "不限"}</span>
                </TableCell>
                <TableCell>
                  {p.kind === "deepseek" || p.kind === "x" ? (
                    <BalanceCell provider={p} />
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell>{p.enabled ? <Badge>启用</Badge> : <Badge variant="outline">停用</Badge>}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button size="sm" variant="outline" onClick={() => setTesting(p)}>
                      <ZapIcon data-icon="inline-start" /> 测试
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setEditing(p)}>
                      <PencilIcon data-icon="inline-start" /> 编辑
                    </Button>
                    <ConfirmButton
                      title={`删除 ${p.name}？`}
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
      {editing && (
        <SearchProviderDialog provider={editing === "new" ? null : editing} onClose={() => setEditing(null)} />
      )}
      {testing && <TestDialog provider={testing} onClose={() => setTesting(null)} />}
    </div>
  );
}

/** Lazily fetched DeepSeek or X API account balance (disabled providers are not queried). */
function BalanceCell({ provider }: { provider: SearchProviderOut }) {
  const path = { provider_id: provider.id };
  const balance = useQuery({
    queryKey: getSearchProviderBalanceQueryKey({ path }),
    queryFn: ({ signal }) => balanceResult(getSearchProviderBalance({ path, signal })),
    enabled: provider.enabled,
    ...BALANCE_QUERY,
  });
  return <BalanceView query={balance} enabled={provider.enabled} />;
}

function SearchProviderDialog({ provider, onClose }: { provider: SearchProviderOut | null; onClose: () => void }) {
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
  const create = useMutation({ ...createSearchProviderMutation(), onSuccess: onClose });
  const update = useMutation({ ...updateSearchProviderMutation(), onSuccess: onClose });
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
        : { ...form, kind: next.value, base_url: "", params: next.params, monthly_limit: next.limit },
    );
  };
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={provider ? `编辑 ${provider.name}` : "新增搜索源"}
      description="web_search 按优先级依次尝试启用的搜索源；保存后可以在列表里单独测试。"
      onSubmit={submit}
      pending={create.isPending || update.isPending}
      submitDisabled={!form.name}
    >
      <Field label="名称">
        <Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <Field label="类型">
        <OptionSelect value={form.kind} onChange={switchKind} options={KINDS} />
      </Field>
      <Field label="地址" hint={kind.url}>
        <Input value={form.base_url ?? ""} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
      </Field>
      <ApiKeyField
        masked={provider?.api_key_masked}
        value={form.api_key ?? ""}
        clear={!!form.clear_api_key}
        emptyHint={form.kind === "searxng" ? "可留空" : "必填"}
        onChange={(patch) => setForm({ ...form, ...patch })}
      />
      <div className="grid grid-cols-2 gap-4">
        <Field label="优先级" hint="数字越小越先用">
          <NumberInput value={form.priority} onChange={(v) => setForm({ ...form, priority: v ?? 100 })} />
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
        hint='Exa：{"type": "auto"}（可选 fast、instant）；SearXNG：{"language": "zh-CN"}；DeepSeek、X API：不需要参数'
      >
        <JsonEditor
          rows={3}
          value={form.params}
          onChange={(v) => isPlainObject(v) && setForm({ ...form, params: v as Record<string, unknown> })}
        />
      </Field>
      <Field label="启用" orientation="horizontal">
        <Switch checked={form.enabled} onCheckedChange={(c) => setForm({ ...form, enabled: c })} />
      </Field>
    </FormDialog>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** Search results as Sources: one line per hit, opening into its snippet and link. */
function Hits({ hits }: { hits: SearchTestHit[] }) {
  if (!hits.length) return <Empty icon={<SearchIcon />}>没有结果</Empty>;
  return (
    <Trace>
      {hits.map((h, i) => (
        <TraceCollapsible key={`${i}:${h.url}`}>
          <TraceCollapsibleTrigger>
            <TraceLine>
              <TraceIcon>
                <GlobeIcon />
              </TraceIcon>
              <TraceTitle>{h.title || hostOf(h.url)}</TraceTitle>
              <TraceDescription>{hostOf(h.url)}</TraceDescription>
            </TraceLine>
          </TraceCollapsibleTrigger>
          <TraceCollapsibleContent className="grid gap-1.5 text-sm">
            {h.snippet && <p className="whitespace-pre-wrap text-foreground">{h.snippet}</p>}
            <ExternalLink href={h.url} className="font-mono text-xs break-all">
              {h.url}
              <span className="sr-only">（在新标签页打开）</span>
            </ExternalLink>
          </TraceCollapsibleContent>
        </TraceCollapsible>
      ))}
    </Trace>
  );
}

function TestDialog({ provider, onClose }: { provider: SearchProviderOut; onClose: () => void }) {
  const [query, setQuery] = useState("Python 最新版本");
  const test = useMutation(testSearchProviderMutation());
  const result = test.data;
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`测试 ${provider.name}`}
      description="只查这一个搜索源，会计入它的本月用量。"
      onSubmit={() => test.mutate({ path: { provider_id: provider.id }, body: { query } })}
      pending={test.isPending}
      submitLabel="搜索"
      submitDisabled={!query.trim()}
      className="sm:max-w-2xl"
    >
      <Field label="查询">
        <Input required value={query} onChange={(e) => setQuery(e.target.value)} />
      </Field>
      {result && !result.ok && (
        <Alert variant="destructive">
          <AlertTitle>搜索失败 · {result.latency_ms}ms</AlertTitle>
          <AlertDescription className="whitespace-pre-wrap">{result.error}</AlertDescription>
        </Alert>
      )}
      {result?.ok && (
        <div className="grid gap-3">
          <p className="text-sm text-muted-foreground">
            {result.latency_ms}ms · {result.hits?.length ?? 0} 条结果
          </p>
          <Hits hits={result.hits ?? []} />
        </div>
      )}
    </FormDialog>
  );
}
