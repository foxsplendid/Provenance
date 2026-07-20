"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..");

async function testClaudeOrganizationSelection() {
  let adapter = null;
  let activeOrganization = "";
  const calls = [];
  const common = {
    publicError(message) {
      return new Error(message);
    },
    readableCookie(name) {
      assert.equal(name, "lastActiveOrg");
      return activeOrganization;
    },
    registerAdapter(value) {
      adapter = value;
    },
    async fetchJson(url) {
      calls.push(url);
      if (url === "/api/organizations") {
        return [{ uuid: "org-a" }, { uuid: "org-b" }];
      }
      if (url === "/api/organizations/org-b/chat_conversations") {
        return [
          { uuid: "older", name: "Older", updated_at: "2099-01-01T00:00:00Z" },
          { uuid: "newer", name: "Newer", updated_at: "2099-01-02T00:00:00Z" }
        ];
      }
      if (url.includes("/api/organizations/org-b/chat_conversations/newer?")) {
        return { uuid: "newer", content: "[SYNTHETIC]" };
      }
      throw new Error(`Unexpected Claude test URL: ${url}`);
    }
  };
  const context = { AbortController, ScriptoriumExportCommon: common };
  context.globalThis = context;
  vm.runInNewContext(
    fs.readFileSync(path.join(ROOT, "src", "content", "claude.js"), "utf8"),
    context,
    { filename: "claude.js" }
  );
  assert.ok(adapter);

  await assert.rejects(
    adapter.prepare(1, new AbortController().signal),
    /multiple organizations/
  );
  assert.deepEqual(calls, ["/api/organizations"]);

  calls.length = 0;
  activeOrganization = "org-b";
  const prepared = await adapter.prepare(1, new AbortController().signal);
  assert.equal(prepared.items.length, 1);
  assert.equal(prepared.items[0].id, "newer");
  assert.deepEqual(calls, [
    "/api/organizations",
    "/api/organizations/org-b/chat_conversations"
  ]);
  const detail = await prepared.fetchDetail(prepared.items[0], new AbortController().signal);
  assert.equal(detail.uuid, "newer");
}

async function testCancelAfterSynchronousBuild() {
  let listener = null;
  let startResponse = null;
  const events = [];
  globalThis.chrome = {
    runtime: {
      id: "extension-id",
      lastError: null,
      onMessage: {
        addListener(value) {
          listener = value;
        }
      },
      sendMessage(message, callback) {
        events.push(message);
        callback();
      }
    }
  };
  globalThis.document = {
    cookie: "",
    createElement() {
      throw new Error("A canceled build must not create a download element.");
    },
    documentElement: {
      appendChild() {}
    }
  };
  globalThis.ScriptoriumZipStore = {
    createZip() {
      setTimeout(() => {
        listener(
          { type: "scriptorium.export.cancel", requestId: "request-1" },
          { id: "extension-id" },
          () => {}
        );
      }, 0);
      return new Uint8Array([1, 2, 3]);
    }
  };

  require(path.join(ROOT, "src", "lib", "export-common.js"));
  globalThis.ScriptoriumExportCommon.registerAdapter({
    provider: "synthetic",
    async prepare() {
      return {
        items: [{ id: "DEMO0001", title: "[SYNTHETIC]" }],
        async fetchDetail() {
          return { id: "DEMO0001", content: "[SYNTHETIC]" };
        }
      };
    }
  });
  assert.equal(typeof listener, "function");
  listener(
    { type: "scriptorium.export.start", requestId: "request-1", limit: 1 },
    { id: "extension-id" },
    (response) => {
      startResponse = response;
    }
  );
  await new Promise((resolve) => setTimeout(resolve, 30));

  assert.deepEqual(startResponse, { ok: true });
  assert.ok(events.some((event) => event.state === "canceled"));
  assert.ok(!events.some((event) => event.state === "complete"));
  assert.ok(!events.some((event) => event.state === "error"));
}

async function main() {
  await testClaudeOrganizationSelection();
  await testCancelAfterSynchronousBuild();
  process.stdout.write("Runtime privacy boundaries passed\n");
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
