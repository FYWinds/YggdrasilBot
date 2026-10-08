import { useMutation, useQuery } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { addAdminMutation, listAdminsOptions, removeAdminMutation } from "@/client/@tanstack/react-query.gen";
import type { AdminIn } from "@/client/types.gen";
import { ConfirmButton, Empty, Field, OptionSelect, PageHeader, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  return (
    <div className="max-w-4xl">
      <PageHeader
        title="管理员"
        description="QQ 超级用户可以私聊处理审核、查看状态；邮箱管理员可以登录这个控制台。环境变量里的管理员只能在部署配置里修改。"
      />
      <Card className="mb-6">
        <CardContent className="flex flex-wrap items-end gap-3">
          <Field label="类型" className="w-44">
            <OptionSelect
              value={form.kind}
              onChange={(v) => v && setForm({ ...form, kind: v as AdminIn["kind"] })}
              options={Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label }))}
            />
          </Field>
          <Field label={form.kind === "qq" ? "QQ 号" : "邮箱"} className="w-64">
            <Input value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} />
          </Field>
          <Field label="备注" className="w-64">
            <Input value={form.note ?? ""} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          </Field>
          <Button disabled={!form.value.trim() || add.isPending} onClick={() => add.mutate({ body: form })}>
            <PlusIcon /> 添加
          </Button>
        </CardContent>
      </Card>
      <QueryState isLoading={admins.isLoading} error={admins.error} />
      {admins.data?.length === 0 && <Empty>还没有管理员</Empty>}
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
