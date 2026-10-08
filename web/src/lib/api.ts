import { MutationCache, QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

type ValidationItem = { loc?: (string | number)[]; msg?: string };

/** Human-readable message from a FastAPI error body ({detail}) or a thrown Error. */
export function errorMessage(e: unknown): string {
  if (e && typeof e === "object" && "detail" in e) {
    const detail = (e as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return (detail as ValidationItem[])
        .map((d) => {
          const loc = (d.loc ?? []).filter((l) => l !== "body").join(".");
          return loc ? `${loc}: ${d.msg}` : (d.msg ?? "");
        })
        .join("\n");
    }
  }
  if (e instanceof Error) return e.message;
  return typeof e === "string" ? e : JSON.stringify(e);
}

export const queryClient: QueryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: false } },
  mutationCache: new MutationCache({
    onError: (e) => {
      toast.error(errorMessage(e));
    },
    // Every admin mutation may affect several views; refetching all is cheap at this scale.
    onSuccess: () => queryClient.invalidateQueries(),
  }),
});
