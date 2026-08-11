# Scriptorium Local Conversation Export

Scriptorium Local Conversation Export, also called Capture in the Scriptorium suite, is a minimal Manifest V3 browser extension for exporting selected ChatGPT or Claude conversation history into a local ZIP file. It exists for one reason: when a useful AI session only lives in the browser, the user should be able to bring that history back into their own local project memory.

Capture is optional. It does not require the Provenance Python package, does not install a background service, and does not upload exported conversations anywhere.

## What it exports

When the user opens the popup and clicks **Export**, the extension reads the most recently updated conversations from the active supported site and writes one readable provider JSON payload per conversation into a ZIP file.

Supported sites:

- `chatgpt.com`
- `chat.openai.com`
- `claude.ai`

The exported JSON is parsed and re-serialized for readability. It is not a byte-for-byte copy of the provider HTTP response.

## Privacy posture

Capture is intentionally small:

- no telemetry;
- no analytics;
- no remote code;
- no third-party runtime library;
- no extension storage;
- no token or cookie logging;
- no background export.

It uses same-origin requests from the already authenticated active tab. API response bodies are written only to the user-requested ZIP download. ZIP files may still contain sensitive account and project data, so store and share them accordingly.

## Install unpacked

1. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
2. Enable developer mode.
3. Choose **Load unpacked** and select this directory.
4. Open an authenticated ChatGPT or Claude tab.
5. Open the extension popup, choose an export count from 1 to 100, and click **Export**.

For a release such as `capture-v1.0.0`, users who need only this exporter can download the versioned `scriptorium-local-conversation-export-*.zip` asset, verify the adjacent `.sha256` file, unzip it, and load the unpacked directory. The rest of the Provenance repository is not required for this workflow.

## Provider adapters

The ChatGPT adapter requests the in-page session and then reads conversation list/detail endpoints from the same origin. If the page exposes the `oai-did` cookie through `document.cookie`, the adapter sends it as the `oai-device-id` header.

The Claude adapter reads organization and conversation endpoints from the same origin. For accounts with multiple organizations, it must identify the active organization from page-readable state; if it cannot, it refuses to export.

These provider endpoints are undocumented and may change. Capture is not affiliated with or endorsed by OpenAI, Anthropic, or the supported providers. Use it only with accounts and data you are authorized to export and follow the applicable platform terms.

## ZIP format

`src/lib/zip-store.js` is a dependency-free ZIP writer. It emits UTF-8 file names, CRC-32 values, and STORE-only entries. It refuses entry counts, names, sizes, offsets, or archive sizes that would require ZIP64.

File names are synthesized from a sequence number, cleaned title, and cleaned conversation identifier. The JSON is pretty-printed without changing its data model and ends with one newline.

## Verify and package

Requirements are Python 3 and Node.js. No package installation is needed.

```text
npm test
npm run pack
```

The tests check JavaScript syntax, generate a ZIP with the browser writer, verify STORE mode, Unicode names/content, CRC-32, byte-for-byte content through Python's `zipfile`, and enforce the package allowlist.

`pack.py` does not follow links, refuses paths outside its explicit source allowlist, requires Manifest V3 and matching bounded versions, builds a deterministic extension archive, and reopens it to verify the exact entry set and manifest version. The distributable includes its MIT `LICENSE` and `NOTICE`.
