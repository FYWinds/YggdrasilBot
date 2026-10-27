import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { getStatsOptions } from "@/client/@tanstack/react-query.gen";
import { Empty, PageHeader, QueryState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function OverviewPage() {
  const stats = useQuery({ ...getStatsOptions(), refetchInterval: 30_000 });
  const s = stats.data;
  const totalMessages = s?.messages_24h.reduce((a, g) => a + g.count, 0) ?? 0;
  const totalInvocations = s?.invocations_24h.reduce((a, g) => a + g.count, 0) ?? 0;
  return (
    <div>
      <PageHeader title="概览" description="最近 24 小时" />
      <QueryState isLoading={stats.isLoading} error={stats.error} variant="card" />
      {s && (
        <div className="grid gap-4 md:grid-cols-3">
          <Card>
            <CardHeader>
              <CardDescription>消息</CardDescription>
              <CardTitle className="text-3xl">{totalMessages}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1 text-sm">
              {s.messages_24h.length === 0 && <Empty>没有消息</Empty>}
              {s.messages_24h.map((g) => (
                <div key={g.group_id} className="flex justify-between gap-2">
                  <span className="truncate">
                    {g.name || g.group_id}
                    {!g.enabled && <Badge variant="outline" className="ml-1">未启用</Badge>}
                  </span>
                  <span className="font-mono">{g.count}</span>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>调用</CardDescription>
              <CardTitle className="text-3xl">{totalInvocations}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1 text-sm">
              {s.invocations_24h.length === 0 && <Empty>没有调用</Empty>}
              {s.invocations_24h.map((x) => (
                <div key={x.status} className="flex justify-between">
                  <span className="font-mono">{x.status}</span>
                  <span className="font-mono">{x.count}</span>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>待审核</CardDescription>
              <CardTitle className="text-3xl">{s.pending_reviews}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm">
              <Button variant="link" asChild className="h-auto p-0">
                <Link to="/reviews">打开审核队列</Link>
              </Button>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
