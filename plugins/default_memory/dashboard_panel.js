// plugins/default_memory/dashboard_panel.ts
function dmemShortTs(value) {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.getMonth() + 1}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
function dmemMetric(value) {
  return String(value ?? 0);
}
function dmemTypeClass(t) {
  return `memory-type-${t || "unknown"}`;
}
function dmemTypePill(memoryType) {
  return `<span class="type-pill ${escapeHtml(dmemTypeClass(memoryType))}">${escapeHtml(memoryType)}</span>`;
}
function dmemStatusPill(status) {
  return `<span class="status-pill memory-status-${escapeHtml(status)}">${escapeHtml(status)}</span>`;
}
var dmemCachedCountsKey = "__initial__";
var dmemCachedCounts = [];
function dmemCountsCacheKey(filters) {
  const q = filters["q"] ?? "";
  const status = filters["status"] ?? "";
  const scope_channel = filters["scope_channel"] ?? "";
  const scope_chat_id = filters["scope_chat_id"] ?? "";
  return JSON.stringify({ q, status, scope_channel, scope_chat_id });
}
async function dmemFetchTypeCounts(filters) {
  const cacheKey = dmemCountsCacheKey(filters);
  const q = filters["q"] ?? "";
  const status = filters["status"] ?? "";
  const scope_channel = filters["scope_channel"] ?? "";
  const scope_chat_id = filters["scope_chat_id"] ?? "";
  if (cacheKey === dmemCachedCountsKey) return dmemCachedCounts;
  const memoryTypes = ["procedure", "preference", "event", "profile"];
  const result = [];
  for (const t of memoryTypes) {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    params.set("memory_type", t);
    if (status) params.set("status", status);
    if (scope_channel) params.set("scope_channel", scope_channel);
    if (scope_chat_id) params.set("scope_chat_id", scope_chat_id);
    params.set("page", "1");
    params.set("page_size", "1");
    params.set("sort_by", "updated_at");
    params.set("sort_order", "desc");
    const payload = await api(`/api/dashboard/memories?${params.toString()}`);
    if ((payload.total || 0) > 0) result.push({ memory_type: t, total: payload.total });
  }
  dmemCachedCountsKey = cacheKey;
  dmemCachedCounts = result;
  return result;
}
function dmemRenderTypeList(counts, activeType) {
  return counts.map((item) => `
    <button class="memory-quick-item ${activeType === item.memory_type ? "active" : ""}" type="button" data-mem-type="${escapeHtml(item.memory_type)}">
      <div class="nav-item-row">
        <span class="nav-type-dot ${escapeHtml(dmemTypeClass(item.memory_type))}"></span>
        <span class="nav-item-name">${escapeHtml(item.memory_type)}</span>
        <span class="nav-item-count">${item.total}</span>
      </div>
    </button>
  `).join("");
}
function dmemUpdateTotal(container, counts) {
  const totalEl = container.querySelector("[data-mem-total]");
  if (!totalEl) return;
  totalEl.textContent = String(counts.reduce((sum, item) => sum + item.total, 0));
  totalEl.removeAttribute("title");
}
function dmemShowCountsError(container) {
  const totalEl = container.querySelector("[data-mem-total]");
  if (!totalEl) return;
  totalEl.textContent = "!";
  totalEl.setAttribute("title", "\u8BB0\u5FC6\u7C7B\u578B\u8BA1\u6570\u52A0\u8F7D\u5931\u8D25");
}
function dmemBindTypeListClicks(container, dispatch) {
  const allBtn = container.querySelector("[data-mem-all]");
  if (allBtn) {
    allBtn.onclick = () => {
      dispatch.activate();
      dispatch.clearFilter("memory_type");
    };
  }
  container.querySelectorAll("[data-mem-type]").forEach((btn) => {
    btn.onclick = () => {
      const t = btn.getAttribute("data-mem-type") ?? "";
      dispatch.activate();
      dispatch.setFilter("memory_type", t);
    };
  });
}
function dmemRenderNavBody(container, dispatch) {
  const filters = dispatch.filters;
  const activeType = filters["memory_type"] ?? "";
  const currentCountsKey = container.getAttribute("data-mem-counts-key") ?? "";
  const nextCountsKey = dmemCountsCacheKey(filters);
  const existingAll = container.querySelector("[data-mem-all]");
  if (existingAll) {
    const allBtn = container.querySelector("[data-mem-all]");
    if (allBtn) allBtn.className = `all-messages-row ${activeType === "" ? "active" : ""}`;
    container.querySelectorAll("[data-mem-type]").forEach((btn) => {
      const t = btn.getAttribute("data-mem-type") ?? "";
      btn.className = `memory-quick-item ${activeType === t ? "active" : ""}`;
    });
    if (currentCountsKey !== nextCountsKey) {
      container.setAttribute("data-mem-counts-key", nextCountsKey);
      dmemFetchTypeCounts(filters).then((counts) => {
        if (container.getAttribute("data-mem-counts-key") !== nextCountsKey) return;
        dmemUpdateTotal(container, counts);
        const list = container.querySelector("[data-mem-count-list]");
        if (!list) return;
        list.innerHTML = dmemRenderTypeList(counts, activeType);
        dmemBindTypeListClicks(container, dispatch);
      }).catch((error) => {
        console.error("[default_memory] \u52A0\u8F7D\u8BB0\u5FC6\u7C7B\u578B\u8BA1\u6570\u5931\u8D25 endpoint=/api/dashboard/memories", error);
        if (container.getAttribute("data-mem-counts-key") !== nextCountsKey) return;
        dmemShowCountsError(container);
        container.removeAttribute("data-mem-counts-key");
      });
    } else if (dmemCachedCountsKey === nextCountsKey) {
      dmemUpdateTotal(container, dmemCachedCounts);
    }
    return;
  }
  container.setAttribute("data-mem-counts-key", nextCountsKey);
  container.innerHTML = `
    <button class="all-messages-row ${activeType === "" ? "active" : ""}" type="button" data-mem-all>
      <span>\u5168\u90E8\u8BB0\u5FC6</span><strong data-mem-total>...</strong>
    </button>
    <div class="memory-quick-list" data-mem-count-list></div>
  `;
  dmemBindTypeListClicks(container, dispatch);
  dmemFetchTypeCounts(filters).then((counts) => {
    if (container.getAttribute("data-mem-counts-key") !== nextCountsKey) return;
    dmemUpdateTotal(container, counts);
    const list = container.querySelector("[data-mem-count-list]");
    if (list) {
      list.innerHTML = dmemRenderTypeList(counts, activeType);
      dmemBindTypeListClicks(container, dispatch);
    }
  }).catch((error) => {
    console.error("[default_memory] \u52A0\u8F7D\u8BB0\u5FC6\u7C7B\u578B\u8BA1\u6570\u5931\u8D25 endpoint=/api/dashboard/memories", error);
    if (container.getAttribute("data-mem-counts-key") !== nextCountsKey) return;
    dmemShowCountsError(container);
    container.removeAttribute("data-mem-counts-key");
  });
}
function dmemRenderFilters(container, dispatch) {
  const filters = dispatch.filters;
  const q = filters["q"] ?? "";
  const memType = filters["memory_type"] ?? "";
  const status = filters["status"] ?? "";
  const scopeChannel = filters["scope_channel"] ?? "";
  const scopeChatId = filters["scope_chat_id"] ?? "";
  const existingSearch = container.querySelector("[data-mem-search]");
  if (existingSearch) {
    const typeSelect2 = container.querySelector("[data-mem-type-select]");
    const statusSelect2 = container.querySelector("[data-mem-status-select]");
    if (typeSelect2 && typeSelect2.value !== memType) typeSelect2.value = memType;
    if (statusSelect2 && statusSelect2.value !== status) statusSelect2.value = status;
    const scopeArea2 = container.querySelector("[data-mem-scope-area]");
    if (scopeArea2) {
      if (scopeChannel || scopeChatId) {
        scopeArea2.innerHTML = `<div class="active-session-chip"><span>scope</span><code>${escapeHtml(scopeChannel)}:${escapeHtml(scopeChatId)}</code><button type="button" data-mem-scope-clear>\xD7</button></div>`;
        scopeArea2.querySelector("[data-mem-scope-clear]")?.addEventListener("click", () => {
          dispatch.clearFilters(["scope_channel", "scope_chat_id"]);
        });
      } else {
        scopeArea2.innerHTML = "";
      }
    }
    return;
  }
  container.innerHTML = `
    <div class="filter-row">
      <label class="search"><span>\u2315</span><input type="text" placeholder="\u641C\u7D22 memory / source_ref" value="${escapeHtml(q)}" data-mem-search /></label>
      <select data-mem-type-select>
        <option value="">\u5168\u90E8 type</option>
        <option value="procedure">procedure</option>
        <option value="preference">preference</option>
        <option value="event">event</option>
        <option value="profile">profile</option>
      </select>
      <select data-mem-status-select>
        <option value="">\u5168\u90E8 status</option>
        <option value="active">active</option>
        <option value="superseded">superseded</option>
      </select>
      <span data-mem-scope-area></span>
    </div>
  `;
  const searchInput = container.querySelector("[data-mem-search]");
  const typeSelect = container.querySelector("[data-mem-type-select]");
  const statusSelect = container.querySelector("[data-mem-status-select]");
  typeSelect.value = memType;
  statusSelect.value = status;
  let debounceTimer = 0;
  searchInput.addEventListener("input", () => {
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => {
      const val = searchInput.value.trim();
      if (val) dispatch.setFilter("q", val);
      else dispatch.clearFilter("q");
    }, 300);
  });
  typeSelect.addEventListener("change", () => {
    if (typeSelect.value) dispatch.setFilter("memory_type", typeSelect.value);
    else dispatch.clearFilter("memory_type");
  });
  statusSelect.addEventListener("change", () => {
    if (statusSelect.value) dispatch.setFilter("status", statusSelect.value);
    else dispatch.clearFilter("status");
  });
  const scopeArea = container.querySelector("[data-mem-scope-area]");
  if (scopeChannel || scopeChatId) {
    scopeArea.innerHTML = `<div class="active-session-chip"><span>scope</span><code>${escapeHtml(scopeChannel)}:${escapeHtml(scopeChatId)}</code><button type="button" data-mem-scope-clear>\xD7</button></div>`;
    scopeArea.querySelector("[data-mem-scope-clear]")?.addEventListener("click", () => {
      dispatch.clearFilters(["scope_channel", "scope_chat_id"]);
    });
  }
}
function dmemRenderTopbarAction(container, dispatch) {
  const currentToken = Number(container.getAttribute("data-mem-render-token") ?? "0") + 1;
  container.setAttribute("data-mem-render-token", String(currentToken));
  api("/api/dashboard/memory/optimizer").then((status) => {
    if (container.getAttribute("data-mem-render-token") !== String(currentToken)) return;
    if (!status.enabled) {
      container.innerHTML = "";
      return;
    }
    container.innerHTML = `<button class="ghost" type="button" data-mem-optimizer>\u8BB0\u5FC6\u4F18\u5316</button>`;
    const btn = container.querySelector("[data-mem-optimizer]");
    let pollTimer = 0;
    const poll = () => {
      window.clearInterval(pollTimer);
      pollTimer = window.setInterval(() => {
        api("/api/dashboard/memory/optimizer").then((s) => {
          if (container.getAttribute("data-mem-render-token") !== String(currentToken)) {
            window.clearInterval(pollTimer);
            return;
          }
          if (!s.running) {
            window.clearInterval(pollTimer);
            btn.disabled = false;
            btn.textContent = s.last_status === "succeeded" ? "\u4F18\u5316\u5DF2\u5B8C\u6210" : s.last_status === "failed" ? "\u4F18\u5316\u5931\u8D25" : s.last_status === "skipped" ? "\u5DF2\u8DF3\u8FC7" : "\u8BB0\u5FC6\u4F18\u5316";
            dispatch.refresh();
          }
        }).catch((error) => {
          window.clearInterval(pollTimer);
          console.error("[default_memory] \u67E5\u8BE2\u8BB0\u5FC6\u4F18\u5316\u72B6\u6001\u5931\u8D25 endpoint=/api/dashboard/memory/optimizer", error);
          if (container.getAttribute("data-mem-render-token") === String(currentToken)) {
            btn.disabled = true;
            btn.textContent = "\u72B6\u6001\u67E5\u8BE2\u5931\u8D25\uFF0C\u8BF7\u5237\u65B0";
          }
        });
      }, 2e3);
    };
    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "\u6B63\u5728\u542F\u52A8\u4F18\u5316";
      api("/api/dashboard/memory/optimize", { method: "POST" }).then(() => {
        btn.textContent = "\u8BB0\u5FC6\u4F18\u5316\u4E2D";
        poll();
      }).catch((error) => {
        console.error("[default_memory] \u542F\u52A8\u8BB0\u5FC6\u4F18\u5316\u5931\u8D25 endpoint=/api/dashboard/memory/optimize", error);
        if (container.getAttribute("data-mem-render-token") !== String(currentToken)) return;
        btn.disabled = false;
        btn.textContent = "\u542F\u52A8\u5931\u8D25\uFF0C\u8BF7\u91CD\u8BD5";
      });
    });
    if (status.running) {
      btn.disabled = true;
      btn.textContent = "\u8BB0\u5FC6\u4F18\u5316\u4E2D";
      poll();
    }
  }).catch((error) => {
    console.error("[default_memory] \u52A0\u8F7D\u8BB0\u5FC6\u4F18\u5316\u5668\u72B6\u6001\u5931\u8D25 endpoint=/api/dashboard/memory/optimizer", error);
    if (container.getAttribute("data-mem-render-token") === String(currentToken)) {
      container.innerHTML = `<span class="muted-text" title="\u8BB0\u5FC6\u4F18\u5316\u5668\u72B6\u6001\u52A0\u8F7D\u5931\u8D25">\u4F18\u5316\u72B6\u6001\u4E0D\u53EF\u7528</span>`;
    }
  });
}
function dmemRenderDetail(item, container, dispatch) {
  if (!item) {
    container.innerHTML = `
      <div class="detail-empty">
        <div class="detail-empty-title">\u8BE6\u60C5</div>
        <div class="detail-empty-text">\u70B9\u5F00 memory \u540E\uFF0C\u8FD9\u91CC\u4F1A\u663E\u793A\u5B8C\u6574\u5B57\u6BB5\u3001JSON \u548C\u76F8\u4F3C\u8BB0\u5FC6\u3002</div>
      </div>
    `;
    return;
  }
  const mem = item;
  const similar = item["_similar"] ?? [];
  const similarError = item["_similar_error"] === true;
  const extraJson = mem.extra_json ?? {};
  const scopeChannel = String(extraJson["scope_channel"] ?? "");
  const scopeChatId = String(extraJson["scope_chat_id"] ?? "");
  const hasScopeBtn = Boolean(scopeChannel || scopeChatId);
  const typePillHtml = dmemTypePill(mem.memory_type);
  const statusPillHtml = dmemStatusPill(mem.status);
  const similarHtml = similarError ? `<div class="muted-text">\u76F8\u4F3C\u8BB0\u5FC6\u52A0\u8F7D\u5931\u8D25\u3002</div>` : similar.length ? similar.map((s) => `<div class="detail-callout"><code>${escapeHtml(s.id)}</code><div>${escapeHtml(s.summary)}</div></div>`).join("") : `<div class="muted-text">\u6CA1\u6709\u76F8\u4F3C\u8BB0\u5FC6\u3002</div>`;
  const scopeBtnHtml = hasScopeBtn ? `<button class="ghost" type="button" data-mem-scope-btn>\u67E5\u770B\u540C scope \u8BB0\u5FC6</button>` : "";
  container.innerHTML = `
    <div class="detail-wrap">
      <div class="detail-toolbar">
        <div>
          <div class="detail-title">\u8BB0\u5FC6\u8BE6\u60C5</div>
          <div class="detail-subtext">${escapeHtml(mem.id)}</div>
        </div>
      </div>
      <div class="detail-block">
        <div class="detail-label">Summary</div>
        <div class="detail-content">${escapeHtml(mem.summary)}</div>
      </div>
      <div class="detail-grid">
        <div class="detail-row"><div class="detail-row-label">type</div><div class="detail-row-val">${typePillHtml}</div></div>
        <div class="detail-row"><div class="detail-row-label">status</div><div class="detail-row-val">${statusPillHtml}</div></div>
        <div class="detail-row"><div class="detail-row-label">source_ref</div><div class="detail-row-val"><code>${escapeHtml(mem.source_ref || "-")}</code></div></div>
        <div class="detail-row"><div class="detail-row-label">embedding</div><div class="detail-row-val"><code>${mem.has_embedding ? `${mem.embedding_dim} dims` : "none"}</code></div></div>
      </div>
      ${scopeBtnHtml}
      <div class="detail-block">
        <div class="detail-label">Extra JSON</div>
        ${jvPlaceholder(extraJson)}
      </div>
      <div class="detail-block">
        <div class="detail-label">Similar</div>
        <div class="detail-similar-list">${similarHtml}</div>
      </div>
    </div>
  `;
  attachJsonViewers(container);
  if (hasScopeBtn && dispatch) {
    container.querySelector("[data-mem-scope-btn]")?.addEventListener("click", () => {
      dispatch.activate();
      dispatch.setFilters({
        scope_channel: scopeChannel,
        scope_chat_id: scopeChatId
      });
    });
  }
}
async function dmemFetchDetail(item) {
  const id = String(item["id"] ?? "");
  const similarEndpoint = `/api/dashboard/memories/${encodePath(id)}/similar?top_k=6`;
  const [detail, similar] = await Promise.all([
    api(`/api/dashboard/memories/${encodePath(id)}`),
    api(similarEndpoint).catch((error) => {
      console.error(`[default_memory] \u52A0\u8F7D\u76F8\u4F3C\u8BB0\u5FC6\u5931\u8D25 endpoint=${similarEndpoint}`, error);
      return { items: [], total: 0, failed: true };
    })
  ]);
  return {
    ...detail,
    _similar: similar.items ?? [],
    _similar_error: similar.failed === true
  };
}
async function dmemFetchPage(opts) {
  const filters = opts.filters ?? {};
  const params = new URLSearchParams();
  if (filters["q"]) params.set("q", filters["q"]);
  if (filters["memory_type"]) params.set("memory_type", filters["memory_type"]);
  if (filters["status"]) params.set("status", filters["status"]);
  if (filters["scope_channel"]) params.set("scope_channel", filters["scope_channel"]);
  if (filters["scope_chat_id"]) params.set("scope_chat_id", filters["scope_chat_id"]);
  params.set("page", String(opts.page));
  params.set("page_size", String(opts.pageSize));
  params.set("sort_by", opts.sortBy || "created_at");
  params.set("sort_order", opts.sortOrder || "desc");
  const payload = await api(`/api/dashboard/memories?${params.toString()}`);
  return { items: payload.items || [], total: payload.total || 0 };
}
function dmemRenderDeleteBtn(_value, item) {
  const id = escapeHtml(String(item.id ?? ""));
  return `<button class="icon-btn row-delete-btn" type="button" onclick="event.stopPropagation();void(async()=>{try{await api('/api/dashboard/memories/batch-delete',{method:'POST',body:JSON.stringify({ids:['${id}']})});window.dispatchEvent(new CustomEvent('akashic-dashboard-refresh'))}catch(e){alert(e.message||String(e))}})()" title="\u5220\u9664\u6B64\u6761">\u2715</button>`;
}
async function dmemGetCount() {
  try {
    const info = await api("/api/dashboard/memory/engine-info");
    if (info.name !== "default") return null;
    const payload = await api("/api/dashboard/memories?page=1&page_size=1&sort_by=created_at&sort_order=desc");
    return payload.total || 0;
  } catch {
    return null;
  }
}
window.AkashicDashboard.registerPlugin({
  id: "default_memory",
  label: "Memory",
  viewLabel: "memory",
  pageSize: 25,
  rowKey: "id",
  defaultSortBy: "created_at",
  defaultSortOrder: "desc",
  countTitle(n) {
    return `${n} \u6761\u8BB0\u5FC6`;
  },
  columns: [
    { key: "memory_type", label: "Type", width: 96, cellClass: "cell-type", renderCell: (v) => dmemTypePill(String(v ?? "")) },
    { key: "summary", label: "Summary", flex: true, cellClass: "content-preview" },
    { key: "reinforcement", label: "Uses", width: 64, fmt: "metric", cellClass: "mono cell-metric", align: "right", sortable: true },
    { key: "emotional_weight", label: "Weight", width: 72, fmt: "metric", cellClass: "mono cell-metric", align: "right", sortable: true },
    { key: "source_ref", label: "Source", width: 120, cellClass: "cell-source" },
    { key: "created_at", label: "Created", width: 96, fmt: "mono-time", cellClass: "mono cell-time", sortable: true },
    { key: "updated_at", label: "Updated", width: 96, fmt: "mono-time", cellClass: "mono cell-time", sortable: true },
    { key: "status", label: "Status", width: 88, cellClass: "cell-status", renderCell: (v) => dmemStatusPill(String(v ?? "")) },
    { key: "_actions", label: "", width: 40, cellClass: "row-delete-cell", renderCell: dmemRenderDeleteBtn }
  ],
  batchActions: [
    {
      label: "\u6279\u91CF\u5220\u9664",
      className: "danger-ghost",
      async run(ids) {
        await api("/api/dashboard/memories/batch-delete", {
          method: "POST",
          body: JSON.stringify({ ids })
        });
      }
    }
  ],
  getCount: dmemGetCount,
  fetchPage: dmemFetchPage,
  fetchDetail: dmemFetchDetail,
  renderNavBody: dmemRenderNavBody,
  renderFilters: dmemRenderFilters,
  renderTopbarAction: dmemRenderTopbarAction,
  renderDetail: dmemRenderDetail,
  formatters: {
    "mono-time": (v) => dmemShortTs(v),
    "metric": (v) => dmemMetric(v)
  }
});
