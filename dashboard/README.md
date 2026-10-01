# LifeOS dashboard source

Responsive, read-only UI for health, training, finance and automation status.
No private data source is connected. The `/review` route is a dated technical
review snapshot, not live monitoring or a current security certification.

Use Node.js >= 22.13.0, then `npm ci`, `npm test` and `npm run dev`.
The lockfile preserves the original frontend dependency versions.

The included `.openai/hosting.json` has empty bindings. No deployment identity,
private data, database export or secret is included. Provision your own hosting
configuration before deploying. This source upload does not deploy a website.

The source does NOT enforce authentication on the dashboard pages. Words such as
"私人網站" retained in the historical review are not a security control. A private
deployment must enforce access at its trusted gateway/server before adding data.
`app/chatgpt-auth.ts` trusts gateway-injected headers and is not a standalone
authentication mechanism for arbitrary hosts.
