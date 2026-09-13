// plugin_packages/wake-proactive/dashboard_panel_meter.tsx
import { useEffect, useState } from "react";
import { api } from "@akashic/dashboard-ui";
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
function meterStatus(data) {
  if (data.should_wake) {
    return { label: "\u5DF2\u51B2\u7834", detail: "\u4FE1\u606F\u538B\u529B\u5DF2\u8D8A\u7EBF\uFF0C\u8FDB\u5165 LLM \u6700\u7EC8\u5224\u65AD\u3002" };
  }
  const ratio = data.threshold > 0 ? (data.hazard_after + data.preference_pressure) / data.threshold : 0;
  if (ratio >= 0.75) {
    return { label: "\u63A5\u8FD1\u9608\u503C", detail: "\u518D\u51FA\u73B0\u4E00\u6761\u5F3A\u76F8\u5173\u4FE1\u606F\u5C31\u53EF\u80FD\u8FDB\u5165\u6700\u7EC8\u5224\u65AD\u3002" };
  }
  if (ratio >= 0.35) {
    return { label: "\u6B63\u5728\u7D2F\u79EF", detail: "\u76F8\u5173\u4FE1\u606F\u6B63\u5728\u63D0\u9AD8\u4E3B\u52A8\u5524\u9192\u538B\u529B\u3002" };
  }
  return { label: "\u4F4E\u538B\u7A33\u5B9A", detail: "\u5F53\u524D\u4FE1\u606F\u4E0D\u8DB3\u4EE5\u6253\u6270\u7528\u6237\u3002" };
}
function percent(value, threshold) {
  if (threshold <= 0) return 0;
  return Math.min(100, Math.max(0, value / (threshold * 1.25) * 100));
}
function MeterPage() {
  const [data, setData] = useState(null);
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const next = await api("/api/dashboard/wake-proactive/meter");
      if (active) setData(next);
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15e3);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);
  if (!data) {
    return /* @__PURE__ */ jsx("div", { className: "meter-loading", children: "\u6B63\u5728\u8BFB\u53D6\u538B\u529B\u4F20\u611F\u5668\u2026" });
  }
  const accumulated = percent(data.hazard_after, data.threshold);
  const pressure = Math.min(
    100 - accumulated,
    percent(data.preference_pressure, data.threshold)
  );
  const total = data.hazard_after + data.preference_pressure;
  const status = meterStatus(data);
  const crossed = Boolean(data.should_wake);
  const ratio = data.threshold > 0 ? total / data.threshold : 0;
  return /* @__PURE__ */ jsxs("main", { className: `excitement-console${crossed ? " is-crossed" : ""}`, "aria-labelledby": "meter-title", children: [
    /* @__PURE__ */ jsxs("header", { className: "meter-header", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u4E3B\u52A8\u5524\u9192\u538B\u529B" }),
        /* @__PURE__ */ jsx("h2", { id: "meter-title", children: "\u662F\u5426\u503C\u5F97\u73B0\u5728\u6253\u6270\u7528\u6237" })
      ] }),
      /* @__PURE__ */ jsxs("div", { className: "meter-state", "aria-live": "polite", children: [
        /* @__PURE__ */ jsx("i", { "aria-hidden": "true" }),
        status.label
      ] })
    ] }),
    /* @__PURE__ */ jsxs("section", { className: "meter-machine", "aria-label": "\u5F53\u524D\u538B\u529B\u4E0E\u9608\u503C", children: [
      /* @__PURE__ */ jsxs("div", { className: "meter-readout", children: [
        /* @__PURE__ */ jsx("span", { children: "\u5F53\u524D\u538B\u529B" }),
        /* @__PURE__ */ jsx("strong", { children: total.toFixed(2) }),
        /* @__PURE__ */ jsxs("em", { children: [
          "/ ",
          data.threshold.toFixed(2)
        ] }),
        /* @__PURE__ */ jsx("p", { children: status.detail })
      ] }),
      /* @__PURE__ */ jsxs("div", { className: "pressure-visual", children: [
        /* @__PURE__ */ jsxs("div", { className: "pressure-scale", children: [
          /* @__PURE__ */ jsx("span", { children: "0" }),
          /* @__PURE__ */ jsxs("span", { children: [
            "\u9608\u503C ",
            data.threshold.toFixed(2)
          ] }),
          /* @__PURE__ */ jsx("span", { children: "125%" })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "pressure-track", role: "img", "aria-label": `\u5F53\u524D\u538B\u529B ${total.toFixed(2)}\uFF0C\u9608\u503C ${data.threshold.toFixed(2)}`, children: [
          /* @__PURE__ */ jsx("i", { className: "pressure-segment is-accumulated", style: { width: `${accumulated}%` } }),
          /* @__PURE__ */ jsx("i", { className: "pressure-segment is-instant", style: { left: `${accumulated}%`, width: `${pressure}%` } }),
          /* @__PURE__ */ jsx("b", { className: "pressure-threshold" })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "pressure-legend", children: [
          /* @__PURE__ */ jsxs("span", { children: [
            /* @__PURE__ */ jsx("i", { className: "is-accumulated" }),
            "\u6301\u7EED\u79EF\u7D2F ",
            data.hazard_after.toFixed(3)
          ] }),
          /* @__PURE__ */ jsxs("span", { children: [
            /* @__PURE__ */ jsx("i", { className: "is-instant" }),
            "\u77AC\u65F6\u5174\u8DA3 ",
            data.preference_pressure.toFixed(3)
          ] }),
          /* @__PURE__ */ jsxs("strong", { children: [
            Math.round(ratio * 100),
            "%"
          ] })
        ] })
      ] })
    ] }),
    /* @__PURE__ */ jsxs("div", { className: "meter-telemetry", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsxs("span", { children: [
          /* @__PURE__ */ jsx("i", { className: "tone-cobalt", "aria-hidden": "true" }),
          "\u6301\u7EED\u84C4\u79EF"
        ] }),
        /* @__PURE__ */ jsx("strong", { children: data.hazard_after.toFixed(3) })
      ] }),
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsxs("span", { children: [
          /* @__PURE__ */ jsx("i", { className: "tone-amber", "aria-hidden": "true" }),
          "\u77AC\u65F6\u5174\u8DA3\u63A8\u529B"
        ] }),
        /* @__PURE__ */ jsx("strong", { children: data.preference_pressure.toFixed(3) })
      ] }),
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u672A\u8BFB\u5185\u5BB9" }),
        /* @__PURE__ */ jsx("strong", { children: data.unread_count }),
        /* @__PURE__ */ jsxs("small", { children: [
          data.candidate_count,
          " \u6761\u53C2\u4E0E\u672C\u8F6E"
        ] })
      ] }),
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u6700\u8FD1\u8BA1\u7B97" }),
        /* @__PURE__ */ jsx("strong", { title: String(data.evaluated_at || ""), children: shortTime(data.evaluated_at) }),
        /* @__PURE__ */ jsxs("small", { children: [
          data.candidate_count,
          " \u6761\u5DF2\u8BA1\u7B97"
        ] })
      ] }),
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u6700\u8FD1 LLM \u5224\u65AD" }),
        /* @__PURE__ */ jsx("strong", { children: data.last_action || "\u5C1A\u672A\u89E6\u53D1" }),
        /* @__PURE__ */ jsx("small", { title: String(data.last_action_at || ""), children: shortTime(data.last_action_at) })
      ] })
    ] }),
    /* @__PURE__ */ jsxs("footer", { className: "meter-footnote", children: [
      /* @__PURE__ */ jsx("span", { children: "\u8D8A\u7EBF\u53EA\u4EE3\u8868\u5141\u8BB8\u5524\u9192 LLM \u5224\u65AD\uFF0C\u4E0D\u7B49\u4E8E\u4E00\u5B9A\u63A8\u9001\u3002" }),
      /* @__PURE__ */ jsx("code", { title: data.driver_item_id || "NO ACTIVE DRIVER", children: data.driver_item_id || "NO ACTIVE DRIVER" })
    ] })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "wake-meter",
  label: "\u5174\u594B\u9608\u503C",
  viewLabel: "\u5174\u594B\u9608\u503C",
  layout: "workbench",
  rowKey: "id",
  columns: [],
  async getCount() {
    const data = await api("/api/dashboard/wake-proactive/meter");
    return data.unread_count;
  },
  async fetchPage() {
    return { items: [], total: 0 };
  },
  Main: MeterPage
});
