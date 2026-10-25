import { useQuery } from "@tanstack/react-query";
import { Link, Outlet } from "@tanstack/react-router";
import {
  BotIcon,
  BrainIcon,
  CalendarClockIcon,
  ContainerIcon,
  GaugeIcon,
  HistoryIcon,
  MessageSquareTextIcon,
  RouteIcon,
  SearchIcon,
  ServerIcon,
  Settings2Icon,
  ShieldCheckIcon,
  SmileIcon,
  TerminalSquareIcon,
  UsersIcon,
} from "lucide-react";
import { getMeOptions } from "@/client/@tanstack/react-query.gen";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/api";

export const NAV = [
  { to: "/", label: "概览", icon: GaugeIcon },
  { to: "/providers", label: "Providers", icon: ServerIcon },
  { to: "/routes", label: "模型路由", icon: RouteIcon },
  { to: "/search", label: "搜索源", icon: SearchIcon },
  { to: "/groups", label: "群管理", icon: UsersIcon },
  { to: "/triggers", label: "指令", icon: TerminalSquareIcon },
  { to: "/settings", label: "设置", icon: Settings2Icon },
  { to: "/prompts", label: "提示词", icon: MessageSquareTextIcon },
  { to: "/reviews", label: "审核队列", icon: ShieldCheckIcon },
  { to: "/events", label: "事件", icon: CalendarClockIcon },
  { to: "/memory", label: "记忆", icon: BrainIcon },
  { to: "/invocations", label: "调用记录", icon: HistoryIcon },
  { to: "/sandbox", label: "工作区", icon: ContainerIcon },
  { to: "/stickers", label: "表情包", icon: SmileIcon },
  { to: "/admins", label: "管理员", icon: BotIcon },
] as const;

export function Layout() {
  const me = useQuery(getMeOptions());
  return (
    <div className="flex min-h-svh">
      <aside className="sticky top-0 flex h-svh w-52 shrink-0 flex-col border-r bg-muted/30">
        <div className="px-4 py-5 text-lg font-semibold">Yggdrasil</div>
        <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              activeOptions={{ exact: to === "/" }}
              className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
              activeProps={{ className: "bg-muted font-medium text-foreground" }}
            >
              <Icon className="size-4" />
              {label}
            </Link>
          ))}
        </nav>
        <div className="border-t px-4 py-3 text-xs text-muted-foreground">
          {me.data ? (
            <div className="grid gap-1">
              <span className="truncate" title={me.data.email}>
                {me.data.email}
              </span>
              {me.data.environment !== "prod" && (
                <Badge variant="outline" className="w-fit">
                  {me.data.environment}
                </Badge>
              )}
            </div>
          ) : me.error ? (
            <span className="text-destructive">{errorMessage(me.error)}</span>
          ) : (
            "…"
          )}
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-8 py-6">
        <Outlet />
      </main>
    </div>
  );
}
