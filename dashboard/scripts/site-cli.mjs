// Keep npm scripts portable across Windows, macOS and Linux.
process.env.WRANGLER_LOG_PATH ??= ".wrangler/wrangler.log";
await import("../node_modules/vinext/dist/cli.js");
