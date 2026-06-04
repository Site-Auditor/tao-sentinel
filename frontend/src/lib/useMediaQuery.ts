import { useSyncExternalStore } from "react";

/**
 * Reactive media-query hook (SSR-safe, shared listeners via matchMedia).
 *
 * Used to drive TanStack column visibility: the table NEVER scrolls
 * horizontally — columns that don't fit the current effective width are
 * removed (their data lives on the detail page), which is how serious data
 * products handle small screens.
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", notify);
      return () => mql.removeEventListener("change", notify);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}
