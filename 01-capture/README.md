# Scriptorium Local Conversation Export

This directory contains a minimal Manifest V3 extension for Chrome and Edge. It exports the most recently updated conversations from the currently active ChatGPT or Claude tab into a ZIP containing one complete provider JSON payload per conversation. Payloads are parsed and re-serialized as readable JSON; the files are not byte-for-byte copies of the HTTP response.

The extension runs an export only after the user opens the popup and clicks **Export**. It performs same-origin `GET` requests with `credentials: "same-origin"`. It has no telemetry, background worker, analytics, remote code, third-party library, extension storage, or token/cookie logging.

## Install unpacked

1. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
2. Enable developer mode.
3. Choose **Load unpacked** and select this directory.
4. Open an authenticated `chatgpt.com`, `chat.openai.com`, or `claude.ai` tab.
5. Open the extension popup, select a count from 1 to 100, and click **Export**.

For a dedicated Capture release such as `capture-v1.0.0`, users who need only
this exporter can download the versioned
`scriptorium-local-conversation-export-*.zip` release asset. Unzip that asset and
load the resulting directory as described above; the Provenance Python package
and the rest of the repository are not required for this workflow.
Download the adjacent `.sha256` file as well and verify the archive before
unpacking it. The checksum uses only the ZIP basename, so `sha256sum -c` works
when both downloaded files are in the same directory.

The popup reports list/detail progress and provides a cancel button. Cancellation aborts the active same-origin request. If ZIP computation has already started, the extension finishes that in-memory computation, yields to process the cancellation, and checks again before starting any download.

## API adapters

The ChatGPT adapter requests the in-page session from `/api/auth/session?unstable_client=true`, then uses `/backend-api/conversations?offset=0&limit=N&order=updated` and `/backend-api/conversation/{id}`. Its bearer token exists only in the active export closure. If the page exposes the `oai-did` cookie through `document.cookie`, the adapter sends its value as the `oai-device-id` header.

The Claude adapter uses `/api/organizations`, `/api/organizations/{org}/chat_conversations`, and `/api/organizations/{org}/chat_conversations/{uuid}?tree=True&rendering_mode=messages&render_all_tools=true`. For accounts with multiple organizations it must match the page's readable `lastActiveOrg` cookie; if the active organization cannot be determined, it refuses to export. Claude's list endpoints may return metadata for more than the selected number of conversations before the adapter keeps the most recent N detail payloads.

Both adapters use only relative same-origin URLs. API response bodies are not written anywhere except the user-requested ZIP download. The extension uses provider-controlled, undocumented endpoints that may change; it is not affiliated with or endorsed by the providers. Use it only with accounts and data you are authorized to export and follow the applicable platform terms.

## ZIP format

`src/lib/zip-store.js` is a new, dependency-free ZIP writer. It emits UTF-8 file names, CRC-32 values, and STORE-only entries. It refuses entry counts, names, sizes, offsets, or archive sizes that would require ZIP64.

File names are synthesized from a sequence number, cleaned title, and cleaned conversation identifier. The JSON is pretty-printed without changing its data model and ends with one newline. ZIP files and their JSON contents may contain sensitive account data; store and share them accordingly.

## Verify and package

Requirements are Python 3 and Node.js. No package installation is needed.

```text
npm test
npm run pack
```

The tests run `node --check` over every JavaScript source, generate a ZIP with the browser writer, and verify STORE mode, Unicode names/content, CRC-32, and byte-for-byte content through Python's `zipfile`. They also verify the package allowlist and confirm that an unknown file makes packaging fail.

`pack.py` scans this project without following links, refuses any path outside its explicit source allowlist, requires Manifest V3 and matching bounded versions, and keeps output at the versioned `dist` path under the project root. It builds a deterministic extension archive and reopens it to verify the exact entry set and archived manifest version. The distributable includes its MIT `LICENSE` and `NOTICE`.
