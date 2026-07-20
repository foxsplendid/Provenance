(() => {
  "use strict";

  const form = document.getElementById("export-form");
  const limitInput = document.getElementById("limit");
  const exportButton = document.getElementById("export");
  const cancelButton = document.getElementById("cancel");
  const progress = document.getElementById("progress");
  const status = document.getElementById("status");
  const site = document.getElementById("site");

  let activeRequestId = null;
  let activeTabId = null;

  function queryActiveTab() {
    return new Promise((resolve, reject) => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError) {
          reject(new Error("The active tab could not be read."));
          return;
        }
        if (!tabs[0] || typeof tabs[0].id !== "number") {
          reject(new Error("No active tab is available."));
          return;
        }
        resolve(tabs[0]);
      });
    });
  }

  function sendTabMessage(tabId, message) {
    return new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tabId, message, (response) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError) {
          reject(new Error("The page adapter is unavailable. Reload the supported page and try again."));
          return;
        }
        resolve(response);
      });
    });
  }

  function providerForUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:") {
        return null;
      }
      if (url.hostname === "chatgpt.com" || url.hostname === "chat.openai.com") {
        return "ChatGPT";
      }
      if (url.hostname === "claude.ai") {
        return "Claude";
      }
    } catch (_error) {
      return null;
    }
    return null;
  }

  function makeRequestId() {
    if (typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    const random = crypto.getRandomValues(new Uint32Array(2));
    return `${Date.now()}-${random[0]}-${random[1]}`;
  }

  function setBusy(busy) {
    limitInput.disabled = busy;
    exportButton.disabled = busy;
    cancelButton.disabled = !busy;
  }

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("error", isError);
  }

  function setProgress(completed, total) {
    if (Number.isInteger(total) && total > 0) {
      progress.max = total;
      progress.value = Math.min(Math.max(Number(completed) || 0, 0), total);
      return;
    }
    progress.removeAttribute("value");
  }

  function finish(message, isError = false) {
    activeRequestId = null;
    activeTabId = null;
    setBusy(false);
    setStatus(message, isError);
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "scriptorium.export.event") {
      return;
    }
    if (!activeRequestId || message.requestId !== activeRequestId) {
      return;
    }

    if (message.state === "complete") {
      progress.max = 1;
      progress.value = 1;
      finish(`Download started: ${message.filename}`);
      return;
    }
    if (message.state === "canceled") {
      progress.max = 1;
      progress.value = 0;
      finish("Export canceled.");
      return;
    }
    if (message.state === "error") {
      progress.max = 1;
      progress.value = 0;
      finish(message.message || "Export failed.", true);
      return;
    }

    setProgress(message.completed, message.total);
    setStatus(message.message || "Exporting conversations...");
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (activeRequestId) {
      return;
    }

    const limit = Number(limitInput.value);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      setStatus("Choose a whole number from 1 to 100.", true);
      limitInput.focus();
      return;
    }

    try {
      const tab = await queryActiveTab();
      const provider = providerForUrl(tab.url);
      if (!provider) {
        throw new Error("Open an authenticated ChatGPT or Claude tab before exporting.");
      }

      const requestId = makeRequestId();
      activeRequestId = requestId;
      activeTabId = tab.id;
      setBusy(true);
      setProgress(0, 0);
      setStatus(`Starting ${provider} export...`);

      const response = await sendTabMessage(tab.id, {
        type: "scriptorium.export.start",
        requestId,
        limit
      });
      if (!response || response.ok !== true) {
        throw new Error(response && response.error ? response.error : "The page rejected the export request.");
      }
    } catch (error) {
      progress.max = 1;
      progress.value = 0;
      finish(error instanceof Error ? error.message : "Export could not start.", true);
    }
  });

  cancelButton.addEventListener("click", async () => {
    if (!activeRequestId || activeTabId === null) {
      return;
    }
    const requestId = activeRequestId;
    cancelButton.disabled = true;
    setStatus("Canceling export...");
    try {
      const response = await sendTabMessage(activeTabId, {
        type: "scriptorium.export.cancel",
        requestId
      });
      if (!response || response.ok !== true) {
        throw new Error("The active export could not be canceled.");
      }
    } catch (error) {
      finish(error instanceof Error ? error.message : "The active export could not be canceled.", true);
    }
  });

  queryActiveTab()
    .then((tab) => {
      const provider = providerForUrl(tab.url);
      site.textContent = provider ? `${provider} tab detected.` : "Open ChatGPT or Claude to export.";
    })
    .catch(() => {
      site.textContent = "No supported active tab detected.";
    });
})();
