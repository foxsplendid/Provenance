(() => {
  "use strict";

  const zipStore = globalThis.ScriptoriumZipStore;
  if (!zipStore || typeof zipStore.createZip !== "function") {
    throw new Error("The ZIP writer was not loaded before the export helper.");
  }

  class PublicExportError extends Error {
    constructor(message) {
      super(message);
      this.name = "PublicExportError";
    }
  }

  function publicError(message) {
    return new PublicExportError(message);
  }

  function abortError() {
    return new DOMException("Export canceled.", "AbortError");
  }

  function throwIfAborted(signal) {
    if (signal.aborted) {
      throw abortError();
    }
  }

  function readableCookie(name) {
    const prefix = `${name}=`;
    for (const section of document.cookie.split(";")) {
      const item = section.trim();
      if (!item.startsWith(prefix)) {
        continue;
      }
      const encodedValue = item.slice(prefix.length);
      let value;
      try {
        value = decodeURIComponent(encodedValue);
      } catch (_error) {
        value = encodedValue;
      }
      return /^[^\u0000-\u001f\u007f]+$/.test(value) ? value : "";
    }
    return "";
  }

  function sendEvent(requestId, state, fields = {}) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(
        {
          type: "scriptorium.export.event",
          requestId,
          state,
          ...fields
        },
        () => {
          void chrome.runtime.lastError;
          resolve();
        }
      );
    });
  }

  async function fetchJson(relativePath, { signal, headers = {}, label }) {
    let url;
    try {
      url = new URL(relativePath, window.location.origin);
    } catch (_error) {
      throw publicError(`The ${label} request path is invalid.`);
    }
    if (url.origin !== window.location.origin || !relativePath.startsWith("/")) {
      throw publicError(`The ${label} request was not same-origin.`);
    }

    let response;
    try {
      response = await fetch(url.href, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: new Headers(headers),
        signal
      });
    } catch (_error) {
      throwIfAborted(signal);
      throw publicError(`The page request was blocked or interrupted while fetching ${label}.`);
    }

    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        throw publicError(`Authentication was rejected while fetching ${label}.`);
      }
      throw publicError(`Fetching ${label} failed with HTTP ${response.status}.`);
    }

    try {
      return await response.json();
    } catch (_error) {
      throw publicError(`The page returned invalid JSON while fetching ${label}.`);
    }
  }

  function cleanFilePart(value, fallback, maximumCharacters) {
    let text = typeof value === "string" ? value : "";
    text = text.normalize("NFKC");
    text = text.replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g, "-");
    text = text.replace(/\s+/g, " ").trim().replace(/[. ]+$/g, "");
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(text)) {
      text = `_${text}`;
    }
    text = Array.from(text).slice(0, maximumCharacters).join("").trim();
    return text || fallback;
  }

  function entryName(item, index, total) {
    const width = Math.max(2, String(total).length);
    const sequence = String(index + 1).padStart(width, "0");
    const title = cleanFilePart(item.title, "untitled", 72);
    const identifier = cleanFilePart(item.id, "unknown-id", 32);
    return `${sequence}-${title}-${identifier}.json`;
  }

  function archiveName(provider) {
    const timestamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
    const providerPart = cleanFilePart(provider, "conversations", 24).toLowerCase();
    return `scriptorium-${providerPart}-${timestamp}.zip`;
  }

  function triggerDownload(bytes, filename) {
    const blob = new Blob([bytes], { type: "application/zip" });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = "noopener";
    anchor.style.display = "none";
    document.documentElement.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  function terminalMessage(error) {
    if (error instanceof PublicExportError) {
      return error.message;
    }
    if (error && error.name === "ZipStoreError") {
      return "The requested data is too large or invalid for a 32-bit non-ZIP64 archive.";
    }
    return "Export failed because the page returned an unexpected result.";
  }

  async function executeExport(adapter, requestId, limit, signal) {
    let prepared = null;
    const entries = [];
    try {
      await sendEvent(requestId, "listing", {
        completed: 0,
        total: 0,
        message: "Fetching the conversation list..."
      });
      prepared = await adapter.prepare(limit, signal);
      throwIfAborted(signal);

      if (!prepared || !Array.isArray(prepared.items) || typeof prepared.fetchDetail !== "function") {
        throw publicError("The page returned an unsupported conversation list.");
      }
      const items = prepared.items.slice(0, limit);
      if (items.length === 0) {
        throw publicError("No conversations were returned for this account.");
      }

      for (let index = 0; index < items.length; index += 1) {
        throwIfAborted(signal);
        await sendEvent(requestId, "details", {
          completed: index,
          total: items.length,
          message: `Fetching conversation ${index + 1} of ${items.length}...`
        });
        const detail = await prepared.fetchDetail(items[index], signal);
        throwIfAborted(signal);
        entries.push({
          name: entryName(items[index], index, items.length),
          data: `${JSON.stringify(detail, null, 2)}\n`
        });
      }

      await sendEvent(requestId, "building", {
        completed: items.length,
        total: items.length,
        message: "Building the ZIP archive..."
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
      throwIfAborted(signal);
      const bytes = zipStore.createZip(entries);
      await new Promise((resolve) => setTimeout(resolve, 0));
      throwIfAborted(signal);
      const filename = archiveName(adapter.provider);
      triggerDownload(bytes, filename);
      await sendEvent(requestId, "complete", { filename });
    } catch (error) {
      if (signal.aborted || (error && error.name === "AbortError")) {
        await sendEvent(requestId, "canceled");
      } else {
        await sendEvent(requestId, "error", { message: terminalMessage(error) });
      }
    } finally {
      entries.length = 0;
      prepared = null;
    }
  }

  function registerAdapter(adapter) {
    if (!adapter || typeof adapter.provider !== "string" || typeof adapter.prepare !== "function") {
      throw new TypeError("An export adapter needs provider and prepare fields.");
    }

    let activeTask = null;
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      if (!sender || sender.id !== chrome.runtime.id || !message || typeof message !== "object") {
        return false;
      }

      if (message.type === "scriptorium.export.start") {
        if (activeTask) {
          sendResponse({ ok: false, error: "An export is already running in this tab." });
          return false;
        }
        if (
          typeof message.requestId !== "string" ||
          message.requestId.length === 0 ||
          message.requestId.length > 128 ||
          !Number.isInteger(message.limit) ||
          message.limit < 1 ||
          message.limit > 100
        ) {
          sendResponse({ ok: false, error: "The export request is invalid." });
          return false;
        }

        const controller = new AbortController();
        activeTask = { requestId: message.requestId, controller };
        sendResponse({ ok: true });
        executeExport(adapter, message.requestId, message.limit, controller.signal).finally(() => {
          if (activeTask && activeTask.requestId === message.requestId) {
            activeTask = null;
          }
        });
        return false;
      }

      if (message.type === "scriptorium.export.cancel") {
        if (!activeTask || activeTask.requestId !== message.requestId) {
          sendResponse({ ok: false, error: "No matching export is running." });
          return false;
        }
        activeTask.controller.abort();
        sendResponse({ ok: true });
        return false;
      }

      return false;
    });
  }

  globalThis.ScriptoriumExportCommon = Object.freeze({
    fetchJson,
    publicError,
    readableCookie,
    registerAdapter
  });
})();
