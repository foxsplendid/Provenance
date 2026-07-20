# Privacy

Scriptorium Local Conversation Export processes data only when the user explicitly clicks **Export** in the extension popup.

## Data accessed

- The selected number of recent conversation detail payloads, from 1 to 100.
- Same-origin ChatGPT or Claude API responses needed for the requested export.
- Claude organization metadata and the active organization's conversation-list metadata. The provider list endpoint may return metadata for more than the selected N conversations; only N detail payloads are added to the ZIP. For multi-organization accounts, the export is refused unless the readable `lastActiveOrg` cookie identifies the active organization.
- For ChatGPT, an in-memory session bearer token. The adapter momentarily scans the page's readable `document.cookie` string to locate `oai-did`; it ignores and does not retain other cookie values.

## Data handling

- Requests are `GET` requests to the origin of the active supported tab and use `credentials: "same-origin"`.
- Tokens and selected cookie values remain in memory only for the active export. The extension does not add them to exported JSON, extension storage, logs, telemetry, or requests outside the active provider origin.
- Conversation responses remain in memory until the ZIP download is created or the export is canceled or fails. ZIP computation is synchronous, so a cancellation received during that computation is applied at the next event-loop turn, before a download starts.
- The only persistent output created by the extension is the ZIP the user requested through the browser download flow.

## Data not collected

The extension has no telemetry, analytics, advertising, remote code, developer-operated external service, background synchronization, or storage permission. It does not send user data outside the active ChatGPT or Claude origin and does not sell, transfer, or upload data to the developer.

## Permissions

- Host access is limited to `https://chatgpt.com/*`, `https://chat.openai.com/*`, and `https://claude.ai/*` so the installed content adapters can receive the user's export command and make their documented same-origin requests. The adapters are loaded on matching pages but do not request conversation data until the user clicks **Export**.
