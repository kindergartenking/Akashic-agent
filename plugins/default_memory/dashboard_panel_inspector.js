// plugins/default_memory/dashboard_panel_inspector.ts
function _memoryTypeClass(t) {
  const map = {
    profile: "recall-type-profile",
    preference: "recall-type-preference",
    event: "recall-type-event",
    procedure: "recall-type-procedure"
  };
  return map[t] ?? "";
}
function _renderRecallItems(items, source) {
  if (!items.length) {
    return '<div class="recall-empty">\u6CA1\u6709\u53EC\u56DE\u6761\u76EE\u3002</div>';
  }
  return `
    <div class="recall-item-list">
      ${items.map((item) => {
    const typeTag = item.memory_type ? `<span class="recall-tag recall-tag-type ${escapeHtml(_memoryTypeClass(item.memory_type))}">${escapeHtml(item.memory_type)}</span>` : "";
    const scoreTag = item.score != null ? `<span class="recall-tag recall-tag-score">${item.score.toFixed(2)}</span>` : "";
    const injectedTag = item.injected === true ? '<span class="recall-tag recall-tag-injected">\u6CE8\u5165</span>' : "";
    const forcedTag = item.forced === true ? '<span class="recall-tag recall-tag-forced">\u5F3A\u5236</span>' : "";
    const extraTags = (item.tags ?? []).map((tag) => `<span class="recall-tag">${escapeHtml(tag)}</span>`).join("");
    return `
        <div class="recall-item recall-item-${escapeHtml(source)}">
          <div class="recall-item-head">
            ${typeTag}${injectedTag}${forcedTag}${scoreTag}${extraTags}
            <code>${escapeHtml(item.id || "-")}</code>
          </div>
          <div class="recall-summary">${escapeHtml(item.summary || "")}</div>
        </div>`;
  }).join("")}
    </div>
  `;
}
window.AkashicDashboard.registerPlugin({
  id: "recall_inspector",
  label: "Recall Inspector",
  viewLabel: "recall inspector",
  pageSize: 25,
  rowKey: "turn_id",
  countTitle(total) {
    return `${total} \u8F6E\u53EC\u56DE`;
  },
  columns: [
    { key: "session_key", label: "Session", width: 108, fmt: "mono-session", cellClass: "mono cell-session", rawTitle: true },
    { key: "timestamp", label: "Time", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "user_text", label: "User", flex: true, fmt: "text-preview", cellClass: "content-preview" },
    { key: "context_prepare_count", label: "Prepare", width: 72, fmt: "metric", cellClass: "mono cell-metric", align: "right" },
    { key: "recall_memory_count", label: "Recall", width: 72, fmt: "metric", cellClass: "mono cell-metric", align: "right" }
  ],
  async getCount() {
    try {
      const r = await api("/api/dashboard/recall-inspector/overview");
      return r.available ? r.total || 0 : null;
    } catch {
      return null;
    }
  },
  async fetchPage({ page, pageSize }) {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    const data = await api(`/api/dashboard/recall-inspector/turns?${params.toString()}`);
    return {
      items: data.items || [],
      total: data.total || 0
    };
  },
  async fetchDetail(item) {
    const turnId = String(item["turn_id"] ?? "");
    return api(`/api/dashboard/recall-inspector/turns/${encodePath(turnId)}`);
  },
  renderDetail(item, container) {
    if (!item) {
      container.innerHTML = `
        <div class="detail-empty">
          <div class="detail-empty-title">Recall Inspector</div>
          <div class="detail-empty-text">\u70B9\u5F00\u4E00\u8F6E\u8BB0\u5F55\u540E\uFF0C\u8FD9\u91CC\u4F1A\u663E\u793A context prepare \u548C recall_memory \u53EC\u56DE\u7684\u8BB0\u5FC6\u3002</div>
        </div>
      `;
      return;
    }
    const turn = item;
    const contextPrepare = turn.context_prepare ?? { items: [], injected_items: [] };
    const recallCalls = turn.recall_memory_calls ?? [];
    container.innerHTML = `
      <div class="detail-wrap">
        <div class="detail-toolbar">
          <div>
            <div class="detail-title">\u53EC\u56DE\u8BB0\u5F55</div>
            <div class="detail-subtext">${escapeHtml(turn.session_key || "")} \xB7 ${escapeHtml(turn.turn_id || "")}</div>
          </div>
        </div>
        <div class="detail-block">
          <div class="detail-label">\u7528\u6237\u6D88\u606F</div>
          <div class="detail-content">${renderMarkdown(turn.user_text || "")}</div>
        </div>
        <div class="detail-block">
          <div class="detail-label">\u9884\u68C0\u7D22\u603B\u53EC\u56DE</div>
          ${_renderRecallItems(contextPrepare.items, "context")}
        </div>
        <div class="detail-block">
          <div class="detail-label">\u6700\u7EC8\u6CE8\u5165</div>
          ${_renderRecallItems(contextPrepare.injected_items, "inject")}
        </div>
        <div class="detail-block">
          <div class="detail-label">\u53EC\u56DE\u7ED3\u679C</div>
          ${recallCalls.length ? recallCalls.map((call) => _renderRecallItems(call.items || [], "recall")).join("") : '<div class="recall-empty">\u672C\u8F6E\u6CA1\u6709\u663E\u5F0F\u8C03\u7528 recall_memory\u3002</div>'}
        </div>
      </div>
    `;
  }
});
