// plugin_packages/default-proactive/dashboard_panel.tsx
import { Chip, JsonView, Markdown, Panel, Stack, api } from "@akashic/dashboard-ui";
import { jsx, jsxs } from "react/jsx-runtime";
function shortTime(value) {
  const date = new Date(String(value || ""));
  return Number.isNaN(date.getTime()) ? String(value || "-") : `${date.getMonth() + 1}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
function Detail({ item }) {
  if (!item) return /* @__PURE__ */ jsx("div", { className: "default-empty", children: "\u9009\u62E9\u4E00\u6761 Tick \u67E5\u770B\u65E7\u4E3B\u52A8\u63A8\u9001\u94FE\u8DEF\u3002" });
  return /* @__PURE__ */ jsxs(Stack, { className: "default-detail", children: [
    /* @__PURE__ */ jsxs(Panel, { className: "default-head", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "TICK" }),
        /* @__PURE__ */ jsx("strong", { children: shortTime(item.started_at) })
      ] }),
      /* @__PURE__ */ jsx(Chip, { children: String(item.terminal_action || "-") })
    ] }),
    /* @__PURE__ */ jsxs(Panel, { className: "default-message", children: [
      /* @__PURE__ */ jsx("span", { children: "\u6700\u7EC8\u6D88\u606F" }),
      /* @__PURE__ */ jsx(Markdown, { children: String(item.final_message || "\u672C\u8F6E\u6CA1\u6709\u53D1\u9001\u6D88\u606F\u3002") })
    ] }),
    /* @__PURE__ */ jsxs("div", { className: "default-trace", children: [
      /* @__PURE__ */ jsx("span", { children: "\u6267\u884C\u8BB0\u5F55" }),
      /* @__PURE__ */ jsx(JsonView, { value: item })
    ] })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "default-proactive",
  label: "Default Tick",
  viewLabel: "default proactive",
  rowKey: "tick_id",
  pageSize: 50,
  defaultSortBy: "started_at",
  defaultSortOrder: "desc",
  columns: [
    { key: "session_key", label: "Session", width: 150 },
    { key: "started_at", label: "Started", width: 104, fmt: "short-time" },
    { key: "terminal_action", label: "Result", width: 110 },
    { key: "final_message", label: "Message", flex: true }
  ],
  async getCount() {
    const data = await api("/api/dashboard/proactive/overview");
    return data.counts.tick_logs || 0;
  },
  async fetchPage({ page, pageSize, sortBy, sortOrder }) {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize), sort_by: sortBy, sort_order: sortOrder });
    return api(`/api/dashboard/proactive/tick_logs?${query}`);
  },
  async fetchDetail(item) {
    return api(`/api/dashboard/proactive/tick_logs/${encodeURIComponent(String(item.tick_id))}`);
  },
  Detail,
  formatters: { "short-time": shortTime }
});
