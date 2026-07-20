(() => {
  "use strict";

  const common = globalThis.ScriptoriumExportCommon;

  function conversationItems(payload) {
    if (payload && Array.isArray(payload.items)) {
      return payload.items;
    }
    if (Array.isArray(payload)) {
      return payload;
    }
    throw common.publicError("ChatGPT returned an unsupported conversation list.");
  }

  async function prepare(limit, signal) {
    const session = await common.fetchJson("/api/auth/session?unstable_client=true", {
      signal,
      label: "the ChatGPT session"
    });
    const accessToken = session && typeof session.accessToken === "string" ? session.accessToken : "";
    if (!accessToken) {
      throw common.publicError("ChatGPT did not provide an authenticated session. Sign in and try again.");
    }

    const headers = { authorization: `Bearer ${accessToken}` };
    const deviceId = common.readableCookie("oai-did");
    if (deviceId) {
      headers["oai-device-id"] = deviceId;
    }

    const list = await common.fetchJson(
      `/backend-api/conversations?offset=0&limit=${limit}&order=updated`,
      {
        signal,
        headers,
        label: "the ChatGPT conversation list"
      }
    );
    const items = conversationItems(list)
      .filter((item) => item && typeof item.id === "string" && item.id.length > 0)
      .slice(0, limit)
      .map((item) => ({
        id: item.id,
        title: typeof item.title === "string" ? item.title : "untitled"
      }));

    return {
      items,
      fetchDetail(item, detailSignal) {
        return common.fetchJson(`/backend-api/conversation/${encodeURIComponent(item.id)}`, {
          signal: detailSignal,
          headers,
          label: "a ChatGPT conversation"
        });
      }
    };
  }

  common.registerAdapter({ provider: "chatgpt", prepare });
})();
