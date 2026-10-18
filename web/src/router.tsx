import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Layout } from "@/components/layout";
import { AdminsPage } from "@/pages/admins";
import { EventsPage } from "@/pages/events";
import { GroupsPage } from "@/pages/groups";
import { InvocationsPage } from "@/pages/invocations";
import { MemoryPage } from "@/pages/memory";
import { OverviewPage } from "@/pages/overview";
import { PromptsPage } from "@/pages/prompts";
import { ProvidersPage } from "@/pages/providers";
import { ReviewsPage } from "@/pages/reviews";
import { RoutesPage } from "@/pages/routes";
import { SandboxPage } from "@/pages/sandbox";
import { SearchPage } from "@/pages/search";
import { SettingsPage } from "@/pages/settings";
import { StickersPage } from "@/pages/stickers";
import { TriggersPage } from "@/pages/triggers";

const rootRoute = createRootRoute({
  component: Layout,
  notFoundComponent: () => <p className="text-sm text-muted-foreground">页面不存在</p>,
});

const page = <const P extends string>(path: P, component: () => React.ReactNode) =>
  createRoute({ getParentRoute: () => rootRoute, path, component });

const routeTree = rootRoute.addChildren([
  page("/", OverviewPage),
  page("/providers", ProvidersPage),
  page("/routes", RoutesPage),
  page("/search", SearchPage),
  page("/groups", GroupsPage),
  page("/triggers", TriggersPage),
  page("/settings", SettingsPage),
  page("/prompts", PromptsPage),
  page("/reviews", ReviewsPage),
  page("/events", EventsPage),
  page("/memory", MemoryPage),
  page("/invocations", InvocationsPage),
  page("/sandbox", SandboxPage),
  page("/stickers", StickersPage),
  page("/admins", AdminsPage),
]);

export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
