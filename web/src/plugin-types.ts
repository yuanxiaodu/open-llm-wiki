// Shared types for build-time frontend plugins. Kept in its own module so plugin
// entry files can import the types without importing the registry (and therefore
// without a runtime import cycle).
import type { ReactElement } from "react";

export interface PluginRoute {
  /** React Router path relative to the parent route; omit for an index route. */
  path?: string;
  /** Mark this child as the parent's index route. */
  index?: boolean;
  element: ReactElement;
  children?: PluginRoute[];
}

export interface FrontendPlugin {
  id: string;
  routes: PluginRoute[];
}
