// plugins/akasha/dashboard_panel_inspector.ts
function shortTime(value) {
  if (!value) return "\u2014";
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(parsed);
}
function fixed(value, digits = 3) {
  if (value === null || value === void 0 || value === "") return "\u2014";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "\u2014";
}
function sourceText(item) {
  const sources = item.sources ?? [];
  if (sources.length) return sources.join(" \xB7 ");
  if (item.first_relation) return item.first_relation;
  return item.graph_only ? "\u4EC5\u7531\u5173\u7CFB\u8865\u5168" : "\u5916\u90E8\u7EBF\u7D22";
}
function renderItems(items, empty) {
  if (!items.length) {
    return `<p class="akasha-empty">${escapeHtml(empty)}</p>`;
  }
  return `
    <ol class="akasha-evidence-list">
      ${items.map((item, index) => {
    const score = item.score ?? item.value ?? item.completion_mass ?? item.seed_score;
    const path = item.relation_path?.length ? `<span class="akasha-path" title="${escapeHtml(item.relation_path.join(" \u2192 "))}">${escapeHtml(item.relation_path.join(" \u2192 "))}</span>` : "";
    return `
          <li class="akasha-evidence">
            <span class="akasha-evidence-rank" aria-hidden="true">${index + 1}</span>
            <div class="akasha-evidence-main">
              <p>${escapeHtml(item.user_text || "\uFF08\u7A7A\u6D88\u606F\uFF09")}</p>
              ${item.assistant_preview ? `<p class="akasha-assistant">${escapeHtml(item.assistant_preview)}</p>` : ""}
            </div>
            <div class="akasha-evidence-meta">
              <time class="akasha-chip akasha-chip--time">${escapeHtml(shortTime(item.ts))}</time>
              <span class="akasha-chip">${escapeHtml(sourceText(item))}</span>
              ${score == null ? "" : `<b class="akasha-chip akasha-chip--score">${fixed(score)}</b>`}
            </div>
            ${path}
          </li>
        `;
  }).join("")}
    </ol>
  `;
}
function evidenceLane(title, description, lane, items, count, empty, open = false) {
  return `
    <details class="akasha-section akasha-lane akasha-lane--${escapeHtml(lane)}" ${open ? "open" : ""}>
      <summary>
        <span class="akasha-lane-copy">
          <strong>${escapeHtml(title)}</strong>
          <small>${escapeHtml(description)}</small>
        </span>
        <span class="akasha-lane-count">${escapeHtml(String(count))}</span>
      </summary>
      ${renderItems(items, empty)}
    </details>
  `;
}
function metric(label, value, detail) {
  return `
    <div class="akasha-metric">
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(String(value))}</dd>
      <p>${escapeHtml(detail)}</p>
    </div>
  `;
}
function renderFilters(container, dispatch) {
  const value = dispatch.filters["q"] ?? "";
  const existing = container.querySelector("[data-akasha-search]");
  if (existing) {
    if (document.activeElement !== existing && existing.value !== value) {
      existing.value = value;
    }
    return;
  }
  container.innerHTML = `
    <div class="akasha-filter">
      <label>
        <span>\u641C\u7D22\u68C0\u7D22\u8BB0\u5F55</span>
        <input
          type="search"
          value="${escapeHtml(value)}"
          placeholder="Query\u3001\u56DE\u590D\u6216 Session"
          data-akasha-search
        />
      </label>
      <md-text-button data-akasha-clear ${value ? "" : "disabled"}>\u6E05\u7A7A</md-text-button>
    </div>
  `;
  const input = container.querySelector("[data-akasha-search]");
  const clear = container.querySelector("[data-akasha-clear]");
  let timer = 0;
  input.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      const query = input.value.trim();
      if (query) dispatch.setFilter("q", query);
      else dispatch.clearFilter("q");
    }, 200);
  });
  clear.addEventListener("click", () => {
    input.value = "";
    dispatch.clearFilter("q");
  });
}
function renderDetail(item, closePane) {
  const recallCount = item.recall_capture_available ? item.left_count + item.right_count : item.left_count;
  return `
    <article class="akasha-inspector">
      <header class="akasha-query">
        <div>
          <h2>${escapeHtml(item.query_text)}</h2>
          <p class="akasha-query-meta">${escapeHtml(shortTime(item.ts))} \xB7 seq ${item.seq}<span>${escapeHtml(item.session_key)}</span></p>
        </div>
        ${closePane ? '<md-icon-button class="akasha-close" data-akasha-close aria-label="\u5173\u95ED\u8BE6\u60C5"><span aria-hidden="true">\xD7</span></md-icon-button>' : ""}
      </header>

      <section class="akasha-overview" aria-labelledby="akasha-overview-title">
        <div class="akasha-overview-heading">
          <div>
            <h3 id="akasha-overview-title">${recallCount} \u6761\u8BB0\u5FC6\u53C2\u4E0E\u56DE\u7B54</h3>
          </div>
          <p>${item.inject_chars > 0 ? `\u5DF2\u5199\u5165 ${item.inject_chars} \u5B57\u4E0A\u4E0B\u6587` : "\u6CA1\u6709\u5199\u5165 Prompt"}</p>
        </div>
        <dl class="akasha-metrics">
          ${metric("\u76F4\u63A5\u7EBF\u7D22", item.seed_count, "Dense\u3001BM25 \u4E0E\u65F6\u5E8F")}
          ${metric("\u7CBE\u786E\u56DE\u5FC6", item.left_count, "\u8BED\u4E49\u6700\u63A5\u8FD1\u7684\u5386\u53F2")}
          ${metric("\u6A21\u5F0F\u8054\u60F3", item.recall_capture_available ? item.right_count : "\u2014", item.recall_capture_available ? `${item.basin_count} \u4E2A\u60C5\u666F\u7C07` : "\u672C\u8F6E\u672A\u8BB0\u5F55")}
        </dl>
      </section>

      <details class="akasha-answer">
        <summary>
          <span><strong>\u52A9\u624B\u56DE\u590D</strong><small>\u67E5\u770B\u8FD9\u4E00\u8F6E\u7684\u5B8C\u6574\u56DE\u7B54</small></span>
          <span class="akasha-answer-action">\u5C55\u5F00</span>
        </summary>
        <div class="akasha-answer-body">${escapeHtml(item.assistant_text || "\uFF08\u52A9\u624B\u6CA1\u6709\u6587\u672C\u56DE\u590D\uFF09")}</div>
      </details>

      <section class="akasha-evidence-group" aria-labelledby="akasha-evidence-title">
        <div class="akasha-section-heading">
          <h3 id="akasha-evidence-title">\u8BB0\u5FC6\u8BC1\u636E</h3>
          <small>\u9009\u62E9\u4E00\u7EC4\u5C55\u5F00\u67E5\u770B</small>
        </div>
        <div class="akasha-lanes">
        ${evidenceLane("\u76F4\u63A5\u7EBF\u7D22", "\u6700\u521D\u547D\u4E2D\u7684\u6D88\u606F", "seed", item.seeds, item.seeds.length, "\u8FD9\u4E00\u8F6E\u6CA1\u6709\u5F62\u6210\u53EF\u6301\u4E45\u5316\u7EBF\u7D22\u3002")}
        ${item.activation_capture_available ? evidenceLane("\u56FE\u6269\u6563\u5019\u9009", "\u7531\u5173\u7CFB\u7F51\u7EDC\u8865\u5165\u7684\u5019\u9009", "activation", item.activation_items, item.activation_items.length, "\u56FE\u6269\u6563\u6CA1\u6709\u589E\u52A0\u5019\u9009\u3002") : ""}
        ${evidenceLane("\u7CBE\u786E\u56DE\u5FC6", "\u8BED\u4E49\u6700\u63A5\u8FD1\u7684\u5386\u53F2\u6D88\u606F", "precise", item.left, item.left_count, "\u6CA1\u6709\u7CBE\u786E\u547D\u4E2D\u3002")}
        ${evidenceLane("\u6A21\u5F0F\u8054\u60F3", "\u8DE8\u5173\u7CFB\u8865\u5168\u4E14\u5DF2\u4E0E\u7CBE\u786E\u7ED3\u679C\u53BB\u91CD", "completion", item.right, item.recall_capture_available ? item.right_count : "\u672A\u8BB0\u5F55", "\u6CA1\u6709\u4EA7\u751F\u6A21\u5F0F\u8054\u60F3\u3002")}
        ${item.tool_left_count ? evidenceLane("\u5DE5\u5177\u7CBE\u786E\u56DE\u5FC6", "recall_memory \u7684\u8BED\u4E49\u547D\u4E2D", "precise", item.tool_left, item.tool_left_count, "\u5DE5\u5177\u6CA1\u6709\u4EA7\u751F\u7CBE\u786E\u547D\u4E2D\u3002") : ""}
        ${item.tool_right_count ? evidenceLane("\u5DE5\u5177\u6A21\u5F0F\u8054\u60F3", "recall_memory \u7684\u56FE\u5173\u7CFB\u7ED3\u679C", "completion", item.tool_right, item.tool_right_count, "\u5DE5\u5177\u6CA1\u6709\u4EA7\u751F\u6A21\u5F0F\u8054\u60F3\u3002") : ""}
        </div>
      </section>

      <details class="akasha-learning">
        <summary><span><strong>\u5B66\u4E60\u53D8\u5316\u4E0E\u6280\u672F\u6307\u6807</strong><small>${item.activation_count} \u6761\u6269\u6563\u5019\u9009 \xB7 ${item.pushes} \u6B21\u6269\u6563</small></span></summary>
        <dl>
          ${metric("\u60CA\u559C\u5EA6", fixed(item.surprise), "\u5F53\u524D cue \u4E0E\u5DF2\u6709\u6A21\u5F0F\u7684\u5DEE\u5F02")}
          ${metric("\u89C2\u5BDF\u8D28\u91CF", fixed(item.observed_mass), "\u7531\u5916\u90E8\u8BC1\u636E\u652F\u6301\u7684\u5B66\u4E60\u8D28\u91CF")}
          ${metric("\u518D\u6FC0\u6D3B", fixed(item.reactivated_mass), "\u5DF2\u6709\u5173\u7CFB\u91CD\u65B0\u83B7\u5F97\u7684\u6D3B\u6027")}
          ${metric("\u589E\u5F3A / \u6291\u5236", `${fixed(item.potentiated_mass)} / ${fixed(item.inhibited_mass)}`, "\u8FDE\u63A5\u9884\u7B97\u5185\u7684\u7ADE\u4E89\u7ED3\u679C")}
        </dl>
      </details>

      <details class="akasha-prompt">
        <summary><span><strong>\u5199\u5165 Prompt \u7684\u8BB0\u5FC6</strong><small>${item.inject_chars} \u5B57 \xB7 \u539F\u59CB\u4E0A\u4E0B\u6587\u9884\u89C8</small></span></summary>
        <pre>${escapeHtml(item.text_block_preview || "\u8FD9\u4E00\u8F6E\u6CA1\u6709\u6CE8\u5165\u8BB0\u5FC6\u3002")}</pre>
      </details>
    </article>
  `;
}
window.AkashicDashboard.registerPlugin({
  id: "akasha_inspector",
  label: "Akasha \u68C0\u7D22",
  viewLabel: "Akasha \u68C0\u7D22",
  pageSize: 25,
  rowKey: "query_id",
  countTitle(total) {
    return `${total} \u8F6E\u68C0\u7D22`;
  },
  columns: [
    { key: "session_key", label: "\u4F1A\u8BDD", width: 120, fmt: "mono-session", cellClass: "mono cell-session", rawTitle: true },
    {
      key: "ts",
      label: "\u65F6\u95F4",
      width: 110,
      cellClass: "mono cell-time",
      rawTitle: true,
      renderCell(value) {
        return escapeHtml(shortTime(value));
      }
    },
    { key: "query_text", label: "\u7528\u6237\u95EE\u9898", flex: true, fmt: "text-preview", cellClass: "content-preview" },
    { key: "seed_count", label: "\u7EBF\u7D22", width: 64, fmt: "metric", cellClass: "mono cell-metric", align: "right" },
    { key: "completion_count", label: "\u53EC\u56DE", width: 64, fmt: "metric", cellClass: "mono cell-metric", align: "right" }
  ],
  renderFilters,
  async getCount() {
    try {
      const result = await api("/api/dashboard/akasha-inspector/overview");
      return result.available ? result.total : null;
    } catch {
      return null;
    }
  },
  async fetchPage({ page, pageSize, filters }) {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize)
    });
    if (filters?.["session_key"]) params.set("session_key", filters["session_key"]);
    if (filters?.["q"]) params.set("q", filters["q"]);
    const result = await api(
      `/api/dashboard/akasha-inspector/turns?${params.toString()}`
    );
    return { items: result.items, total: result.total };
  },
  async fetchDetail(item) {
    return api(
      `/api/dashboard/akasha-inspector/turns/${encodePath(String(item["query_id"] ?? ""))}`
    );
  },
  renderDetail(item, container, dispatch) {
    if (!item) {
      container.innerHTML = `
        <div class="detail-empty">
          <div class="detail-empty-title">Akasha Inspector</div>
          <div class="detail-empty-text">\u9009\u62E9\u4E00\u8F6E\u68C0\u7D22\uFF0C\u67E5\u770B\u5B83\u4ECE\u54EA\u4E9B\u7EBF\u7D22\u5F00\u59CB\u3001\u6269\u6563\u5230\u54EA\u91CC\uFF0C\u4EE5\u53CA\u6700\u7EC8\u8FDB\u5165 Prompt \u7684\u5185\u5BB9\u3002</div>
        </div>
      `;
      return;
    }
    container.innerHTML = renderDetail(
      item,
      dispatch?.closePane
    );
    container.querySelector("[data-akasha-close]")?.addEventListener(
      "click",
      () => dispatch?.closePane?.()
    );
    const lanes = Array.from(container.querySelectorAll(".akasha-lane"));
    for (const lane of lanes) {
      lane.addEventListener("toggle", () => {
        if (!lane.open) return;
        for (const sibling of lanes) {
          if (sibling !== lane) sibling.open = false;
        }
      });
    }
  }
});
