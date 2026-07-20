(() => {
  "use strict";

  const common = globalThis.ScriptoriumExportCommon;

  function arrayFrom(payload, keys, label) {
    if (Array.isArray(payload)) {
      return payload;
    }
    if (payload && typeof payload === "object") {
      for (const key of keys) {
        if (Array.isArray(payload[key])) {
          return payload[key];
        }
      }
    }
    throw common.publicError(`Claude returned an unsupported ${label} response.`);
  }

  function updatedTime(item) {
    const value = item.updated_at ?? item.updatedAt ?? item.created_at ?? item.createdAt;
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string") {
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    return 0;
  }

  async function prepare(limit, signal) {
    const organizationsResponse = await common.fetchJson("/api/organizations", {
      signal,
      label: "the Claude organization list"
    });
    const organizations = arrayFrom(organizationsResponse, ["organizations", "items"], "organization list");
    const availableOrganizations = organizations.filter((item) => {
      const identifier = item && (item.uuid ?? item.id);
      return typeof identifier === "string" && identifier.length > 0;
    });
    const activeOrganizationId = common.readableCookie("lastActiveOrg");
    let organization = availableOrganizations[0];
    if (availableOrganizations.length > 1) {
      organization = availableOrganizations.find(
        (item) => (item.uuid ?? item.id) === activeOrganizationId
      );
      if (!organization) {
        throw common.publicError(
          "Claude has multiple organizations, but the active organization could not be determined. Open a conversation in the intended organization and try again."
        );
      }
    }
    if (!organization) {
      throw common.publicError("Claude did not return an organization for this account.");
    }
    const organizationId = organization.uuid ?? organization.id;
    const encodedOrganizationId = encodeURIComponent(organizationId);

    const listResponse = await common.fetchJson(
      `/api/organizations/${encodedOrganizationId}/chat_conversations`,
      {
        signal,
        label: "the Claude conversation list"
      }
    );
    const conversations = arrayFrom(listResponse, ["chat_conversations", "conversations", "items"], "conversation list");
    const items = conversations
      .map((item, originalIndex) => ({ item, originalIndex, timestamp: item ? updatedTime(item) : 0 }))
      .filter(({ item }) => {
        const identifier = item && (item.uuid ?? item.id);
        return typeof identifier === "string" && identifier.length > 0;
      })
      .sort((left, right) => right.timestamp - left.timestamp || left.originalIndex - right.originalIndex)
      .slice(0, limit)
      .map(({ item }) => ({
        id: item.uuid ?? item.id,
        title:
          typeof item.name === "string"
            ? item.name
            : typeof item.title === "string"
              ? item.title
              : "untitled"
      }));

    return {
      items,
      fetchDetail(item, detailSignal) {
        const encodedConversationId = encodeURIComponent(item.id);
        return common.fetchJson(
          `/api/organizations/${encodedOrganizationId}/chat_conversations/${encodedConversationId}?tree=True&rendering_mode=messages&render_all_tools=true`,
          {
            signal: detailSignal,
            label: "a Claude conversation"
          }
        );
      }
    };
  }

  common.registerAdapter({ provider: "claude", prepare });
})();
