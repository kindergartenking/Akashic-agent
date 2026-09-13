// plugin_packages/wake-proactive/dashboard_panel.tsx
import { Chip, JsonView, Markdown, api } from "@akashic/dashboard-ui";
import { jsx, jsxs } from "react/jsx-runtime";
function shortTime(value) {
  if (!value) return "\u2014";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}
function count(value) {
  if (Array.isArray(value)) return value.length;
  return value && typeof value === "object" ? Object.keys(value).length : 0;
}
function actionLabel(value) {
  if (value === "reply") return "\u51B3\u5B9A\u56DE\u590D";
  if (value === "skip") return "\u51B3\u5B9A\u8DF3\u8FC7";
  return "\u7B49\u5F85\u5224\u65AD";
}
function JsonSection({ title, value, open = false }) {
  return /* @__PURE__ */ jsxs("details", { className: "wake-audit", open, children: [
    /* @__PURE__ */ jsxs("summary", { children: [
      /* @__PURE__ */ jsx("span", { children: title }),
      /* @__PURE__ */ jsx(Chip, { tone: "muted", children: count(value) })
    ] }),
    /* @__PURE__ */ jsx(JsonView, { value })
  ] });
}
function Detail({ item }) {
  if (!item) {
    return /* @__PURE__ */ jsx("div", { className: "wake-empty", children: "\u9009\u62E9\u4E00\u6B21\u5524\u9192\uFF0C\u67E5\u770B\u5B8C\u6574\u5224\u65AD\u94FE\u3002" });
  }
  const observations = Array.isArray(item.observations) ? item.observations : [];
  const action = String(item.terminal_action || "pending");
  return /* @__PURE__ */ jsxs("main", { className: "wake-detail", "aria-labelledby": "wake-detail-title", children: [
    /* @__PURE__ */ jsxs("header", { className: "wake-summary", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u4E3B\u52A8\u5524\u9192\u5224\u65AD" }),
        /* @__PURE__ */ jsx("h2", { id: "wake-detail-title", children: actionLabel(action) }),
        /* @__PURE__ */ jsxs("small", { children: [
          shortTime(item.now_utc),
          " \xB7 ",
          String(item.session_key || "\u672A\u5173\u8054\u4F1A\u8BDD")
        ] })
      ] }),
      /* @__PURE__ */ jsx(Chip, { tone: action === "skip" ? "muted" : "accent", dot: true, children: actionLabel(action) })
    ] }),
    /* @__PURE__ */ jsxs("section", { className: "wake-message", "aria-label": "\u6700\u7EC8\u884C\u4E3A", children: [
      /* @__PURE__ */ jsx("span", { children: action === "reply" ? "\u6700\u7EC8\u53D1\u9001\u5185\u5BB9" : "\u672C\u8F6E\u7ED3\u679C" }),
      /* @__PURE__ */ jsx(Markdown, { children: String(item.final_message || "\u672C\u8F6E\u51B3\u5B9A\u4E0D\u4E3B\u52A8\u6253\u6270\u3002") })
    ] }),
    /* @__PURE__ */ jsxs("section", { className: "wake-reasoning", "aria-labelledby": "wake-reasoning-title", children: [
      /* @__PURE__ */ jsxs("div", { className: "wake-section-heading", children: [
        /* @__PURE__ */ jsx("span", { children: "\u5224\u65AD\u8DEF\u5F84" }),
        /* @__PURE__ */ jsx("h3", { id: "wake-reasoning-title", children: "\u4ECE\u89E6\u53D1\u4FE1\u53F7\u5230\u6700\u7EC8\u52A8\u4F5C" })
      ] }),
      observations.map((observation, index) => /* @__PURE__ */ jsxs("div", { className: "wake-phase", children: [
        /* @__PURE__ */ jsxs("div", { className: "wake-phase-title", children: [
          /* @__PURE__ */ jsx("b", { children: index + 1 }),
          /* @__PURE__ */ jsxs("span", { children: [
            "\u89C2\u6D4B \xB7 ",
            String(observation.kind || "unknown")
          ] })
        ] }),
        /* @__PURE__ */ jsx(JsonSection, { title: "\u89E6\u53D1\u539F\u56E0", value: observation.trigger, open: true }),
        /* @__PURE__ */ jsx(JsonSection, { title: "\u8FDB\u5165\u5224\u65AD\u7684\u5019\u9009", value: observation.candidates }),
        /* @__PURE__ */ jsx(JsonSection, { title: "\u9001\u7ED9 LLM \u7684\u8F93\u5165", value: observation.llm_input })
      ] }, `${String(observation.kind)}-${index}`))
    ] }),
    /* @__PURE__ */ jsxs("section", { className: "wake-technical", "aria-labelledby": "wake-technical-title", children: [
      /* @__PURE__ */ jsxs("div", { className: "wake-section-heading", children: [
        /* @__PURE__ */ jsx("span", { children: "\u5BA1\u8BA1\u4FE1\u606F" }),
        /* @__PURE__ */ jsx("h3", { id: "wake-technical-title", children: "\u8BA1\u5212\u3001\u8C03\u67E5\u4E0E\u5F15\u7528" })
      ] }),
      /* @__PURE__ */ jsx(JsonSection, { title: "\u521D\u7B5B\u8BA1\u5212 Scratchpad", value: item.scratchpad }),
      /* @__PURE__ */ jsx(JsonSection, { title: "\u6B63\u6587\u4E0E\u8BB0\u5FC6\u8C03\u67E5\u7ED3\u679C", value: item.investigations }),
      /* @__PURE__ */ jsx(JsonSection, { title: "\u6700\u7EC8\u5F15\u7528 ID", value: item.cited_ids }),
      /* @__PURE__ */ jsx(JsonSection, { title: "\u5C55\u793A\u5E8F\u53F7\u6620\u5C04", value: item.display_event_map }),
      /* @__PURE__ */ jsx(JsonSection, { title: "\u6765\u6E90\u5F15\u7528", value: item.source_refs })
    ] })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "wake-proactive",
  label: "\u4E3B\u52A8\u5524\u9192",
  viewLabel: "\u4E3B\u52A8\u5524\u9192",
  rowKey: "wake_id",
  pageSize: 50,
  defaultSortBy: "now_utc",
  defaultSortOrder: "desc",
  columns: [
    {
      key: "session_key",
      label: "\u4F1A\u8BDD",
      width: 170,
      fmt: "mono-session",
      cellClass: "mono cell-session",
      rawTitle: true
    },
    {
      key: "now_utc",
      label: "\u65F6\u95F4",
      width: 112,
      fmt: "wake-time",
      cellClass: "mono cell-time",
      rawTitle: true
    },
    {
      key: "terminal_action",
      label: "\u7ED3\u679C",
      width: 88,
      cellClass: "cell-status",
      renderCell(value) {
        const action = String(value || "pending");
        const tone = action === "reply" ? "proactive-result-reply" : action === "skip" ? "proactive-result-skip" : "proactive-result-unknown";
        return `<span class="status-pill ${tone}">${escapeHtml(actionLabel(action))}</span>`;
      }
    },
    {
      key: "final_message",
      label: "\u6700\u7EC8\u5185\u5BB9",
      flex: true,
      fmt: "text-preview",
      cellClass: "content-preview",
      rawTitle: true
    }
  ],
  async getCount() {
    const data = await api("/api/dashboard/wake-proactive/runs?page=1&page_size=1");
    return data.total || 0;
  },
  async fetchPage({ page, pageSize }) {
    return api(`/api/dashboard/wake-proactive/runs?page=${page}&page_size=${pageSize}`);
  },
  async fetchDetail(item) {
    return api(
      `/api/dashboard/wake-proactive/runs/${encodeURIComponent(String(item.wake_id))}`
    );
  },
  Detail,
  formatters: { "wake-time": shortTime }
});
