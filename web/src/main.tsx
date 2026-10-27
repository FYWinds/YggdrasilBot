import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { queryClient } from "@/lib/api";
import { router } from "@/router";
import "./index.css";

// UI annotation toolbar for handing feedback to coding agents; dev server only, never in the build. Set
// VITE_AGENTATION_ENDPOINT (e.g. in web/.env.local) to send annotations to a local agentation-mcp server.
const Agentation = import.meta.env.DEV ? lazy(() => import("agentation").then((m) => ({ default: m.Agentation }))) : null;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} />
        <Toaster richColors position="top-right" />
        {Agentation && (
          <Suspense>
            <Agentation endpoint={import.meta.env.VITE_AGENTATION_ENDPOINT || undefined} />
          </Suspense>
        )}
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
