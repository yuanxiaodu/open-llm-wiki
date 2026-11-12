// Build-time frontend plugin registry.
//
// A plugin's backend half is discovered at runtime from app.toml. Its frontend
// half must be known to the bundler, so we discover it with Vite's glob import:
// any `plugins/<id>/web/index.tsx` default-exporting { id, routes } is registered
// automatically. A fork only needs to add the plugin directory; it does not edit
// this file (which keeps upstream merges clean).
//
// /api/site advertises which plugins are enabled; App mounts only those routes
// and AppShell shows only those nav entries. The code of a disabled plugin may
// still be bundled (build-time discovery), but it is never rendered.
import type { FrontendPlugin, PluginRoute } from "./plugin-types.ts";

type PluginModule = { default: FrontendPlugin };

const modules = import.meta.glob<PluginModule>("../../plugins/*/web/index.tsx", {
  eager: true,
});

export const PLUGIN_MODULES: Record<string, FrontendPlugin> = {};
for (const mod of Object.values(modules)) {
  const plugin = mod?.default;
  if (plugin && typeof plugin.id === "string" && plugin.id.trim() !== "") {
    PLUGIN_MODULES[plugin.id] = plugin;
  }
}

/** Routes contributed by an enabled plugin (empty when unknown/disabled). */
export function pluginRoutes(id: string): PluginRoute[] {
  return PLUGIN_MODULES[id]?.routes ?? [];
}
