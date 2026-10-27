import { useMutation, useQuery } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import { addAdminMutation, listAdminsOptions, removeAdminMutation } from "@/client/@tanstack/react-query.gen";
import type { AdminIn } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  Field,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const KIND_LABELS: Record<AdminIn["kind"], string> = { qq: "QQ（超级用户）", email: "邮箱（控制台）" };

export function AdminsPage() {
  const admins = useQuery(listAdminsOptions());
  const [form, setForm] = useState<AdminIn>({ kind: "email", value: "", note: "" });
  const add = useMutation({ ...addAdminMutation(),
      onSuccess: () => {
        toast.success("已添加");
        setForm({ ...form, value: "", note: "" });
      },
    });
  const remove = useMutation({ ...removeAdminMutation(), onSuccess: () => toast.success("已移除") });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (form.value.trim() && !add.isPending) add.mutate({ body: form });
  };
  return (
    <div className="max-w-4xl">
      <PageHeader
        title="管理员"
        description="QQ 超级用户可以私聊处理审核、查看状态；邮箱管理员可以登录这个控制台。环境变量里的管理员只能在部署配置里修改。"
      />
      <Card className="mb-6">
        <CardContent>
          <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
            <Field label="类型" className="w-44">
              <OptionSelect
                value={form.kind}
                onChange={(v) => v && setForm({ ...form, kind: v as AdminIn["kind"] })}
                options={Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label }))}
              />
            </Field>
            <Field label={form.kind === "qq" ? "QQ 号" : "邮箱"} className="w-64">
              <Input
                required
                type={form.kind === "email" ? "email" : "text"}
                inputMode={form.kind === "qq" ? "numeric" : undefined}
                value={form.value}
                onChange={(e) => setForm({ ...form, value: e.target.value })}
              />
            </Field>
            <Field label="备注" className="w-64">
              <Input value={form.note ?? ""} onChange={(e) => setForm({ ...form, note: e.target.value })} />
            </Field>
            <PendingButton type="submit" pending={add.isPending} disabled={!form.value.trim()}>
              {!add.isPending && <PlusIcon data-icon="inline-start" />}
              添加
            </PendingButton>
          </form>
        </CardContent>
      </Card>
      <QueryState isLoading={admins.isLoading} error={admins.error} />
      {admins.data?.length === 0 && <Empty description="在上面添加 QQ 超级用户或控制台邮箱。">还没有管理员</Empty>}
      {!!admins.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>类型</TableHead>
              <TableHead>值</TableHead>
              <TableHead>备注</TableHead>
              <TableHead>添加时间</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {admins.data.map((a) => (
              <TableRow key={`${a.kind}:${a.value}`}>
                <TableCell>{KIND_LABELS[a.kind]}</TableCell>
                <TableCell className="font-mono">{a.value}</TableCell>
                <TableCell>{a.note}</TableCell>
                <TableCell className="text-xs">{fmtTime(a.created_at)}</TableCell>
                <TableCell className="text-right">
                  {a.bootstrap ? (
                    <Badge variant="outline">环境变量</Badge>
                  ) : (
                    <ConfirmButton
                      title={`移除 ${a.value}？`}
                      onConfirm={() => remove.mutate({ path: { kind: a.kind, value: a.value } })}
                    >
                      移除
                    </ConfirmButton>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
