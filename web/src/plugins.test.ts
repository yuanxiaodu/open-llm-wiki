import { describe, expect, it } from "vitest";

import { PLUGIN_MODULES, pluginRoutes } from "./plugins.ts";

describe("frontend plugin registry", () => {
  it("registry 里的每项 id 与 key 一致、routes 是数组", () => {
    for (const [id, plugin] of Object.entries(PLUGIN_MODULES)) {
      expect(plugin.id).toBe(id);
      expect(Array.isArray(plugin.routes)).toBe(true);
    }
  });

  it("未知/未启用的插件 id 不贡献路由", () => {
    expect(pluginRoutes("missing")).toEqual([]);
  });
});
