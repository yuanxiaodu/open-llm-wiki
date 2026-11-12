# Plugins

A plugin is a Python package under this directory plus an optional frontend entry:

```
plugins/
  my-plugin/
    __init__.py          # module-level `plugin` object
    backend/             # routers, storage, etc.
    web/
      index.tsx          # default export { id, routes }
      my-plugin.css      # optional, imported by index.tsx
```

The backend is discovered at runtime from `app.toml`:

```toml
[plugins]
enabled = ["my-plugin"]

[plugins.my-plugin]
# arbitrary options passed to plugin.configure()
```

The frontend is discovered at build time by `web/src/plugins.ts` via
`import.meta.glob("../../plugins/*/web/index.tsx")`. A fork only adds the plugin
directory; it does not edit the core registry.

See `docs/PLUGINS.md` for the full interface, lifecycle, nav contract, testing,
and the OpenAPI policy (plugin routes are intentionally excluded from the core
OpenAPI schema).

This repository intentionally ships **no feature plugins**. Private deployment
cases may add their own plugins in a fork; see `docs/DEPLOYMENT_CASE_PMWIKI.md`.
