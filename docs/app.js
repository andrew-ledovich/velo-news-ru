const TOPIC_ORDER = ["CRYPTO", "AI", "MACRO", "EV"];
const TOPIC_LABELS = {
  CRYPTO: "CRYPTO",
  AI: "AI",
  MACRO: "MACRO",
  EV: "EV",
};
const TOPIC_ACCENT = {
  CRYPTO: "#ff8c00",
  AI: "#00d4ff",
  MACRO: "#b3b3b3",
  EV: "#00ff41",
};

const STATE = {
  items: [],
  generatedAt: null,
  sourceCount: 0,
  activeFilter: "ALL",
  query: "",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return char;
    }
  });
}

function parseTime(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function formatFull(iso) {
  const date = parseTime(iso);
  if (!date) return "—";
  return date.toLocaleString("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function relative(iso) {
  const date = parseTime(iso);
  if (!date) return { label: "—", className: "" };
  const diff = Date.now() - date.valueOf();
  const hours = diff / 36e5;
  if (hours < 1) return { label: "<1ч", className: "recent" };
  if (hours < 24) return { label: `${Math.round(hours)}ч`, className: "recent" };
  if (hours < 24 * 7) return { label: `${Math.round(hours / 24)}д`, className: "" };
  return { label: formatFull(iso), className: "stale" };
}

function buildFilters() {
  const counts = { ALL: STATE.items.length };
  for (const t of TOPIC_ORDER) counts[t] = 0;
  for (const item of STATE.items) {
    if (counts[item.topic] !== undefined) counts[item.topic] += 1;
  }
  return [
    { id: "ALL", label: "все", count: counts.ALL },
    ...TOPIC_ORDER.map((t) => ({ id: t, label: TOPIC_LABELS[t] || t, count: counts[t] })),
  ];
}

function filterItems() {
  let visible = STATE.items;
  if (STATE.activeFilter !== "ALL") {
    visible = visible.filter((item) => item.topic === STATE.activeFilter);
  }
  const q = STATE.query.trim().toLowerCase();
  if (q) {
    visible = visible.filter((item) => {
      const haystack = [item.titleRu, item.titleEn, item.source].filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(q);
    });
  }
  return visible;
}

function groupByTopic(items) {
  const groups = new Map();
  for (const t of TOPIC_ORDER) groups.set(t, []);
  for (const item of items) {
    if (!groups.has(item.topic)) groups.set(item.topic, []);
    groups.get(item.topic).push(item);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => {
      const ta = parseTime(a.published)?.valueOf() || 0;
      const tb = parseTime(b.published)?.valueOf() || 0;
      return tb - ta;
    });
  }
  return groups;
}

function renderFilters() {
  const root = document.getElementById("filter-row");
  if (!root) return;
  const filters = buildFilters();
  root.innerHTML = filters
    .map((f) => {
      const active = STATE.activeFilter === f.id ? " active" : "";
      const dataTopic = f.id === "ALL" ? "ALL" : f.id;
      const accent = f.id !== "ALL" ? ` style="--accent:${TOPIC_ACCENT[f.id] || "#ff8c00"}"` : "";
      return `<button class="pill${active}" data-filter="${escapeHtml(dataTopic)}"${accent}>${escapeHtml(f.label)}<span class="count">${f.count}</span></button>`;
    })
    .join("");
  root.querySelectorAll(".pill").forEach((button) => {
    button.addEventListener("click", () => {
      STATE.activeFilter = button.dataset.filter;
      renderFilters();
      render();
    });
  });
}

function renderItem(item) {
  const time = relative(item.published);
  const status = item.translationStatus || "translated";
  const titleClass = status === "failed" || status === "original" ? " item-title failed" : "";
  const source = escapeHtml(item.source || "");
  const topic = item.topic || "MACRO";
  const accent = TOPIC_ACCENT[topic] || "#ff8c00";
  const statusLabel = status === "translated" ? "" :
    `<span class="translation-${status}">${status === "failed" ? "RU ОШИБКА" : "RU ОРИГИНАЛ"}</span>`;
  const link = item.link || "https://github.com/andrew-ledovich/velo-news-ru";
  return `
    <div class="item" data-id="${escapeHtml(item.id)}" style="--accent:${accent}">
      <span class="item-time ${time.className}" title="${escapeHtml(formatFull(item.published))}">${escapeHtml(time.label)}</span>
      <span class="item-topic" title="Тема: ${topic}">${escapeHtml(topic)}</span>
      <div class="item-main">
        <div class="item-title${titleClass}">${escapeHtml(item.titleRu)}</div>
        <div class="item-meta">
          ${source ? `<span class="source">${source}</span>` : ""}
          <span>${escapeHtml(formatFull(item.published))} МСК</span>
          ${statusLabel}
        </div>
        <div class="item-body" hidden>
          <p>${escapeHtml(item.titleRu)}</p>
          <p class="original">EN: ${escapeHtml(item.titleEn)}</p>
          <a class="open" href="${escapeHtml(link)}" target="_blank" rel="noopener">ОТКРЫТЬ ИСТОЧНИК ↗</a>
        </div>
      </div>
      <span class="item-arrow">▶</span>
    </div>`;
}

function renderGroup(letter, label, items) {
  if (!items.length) return "";
  return `
    <div class="group" data-group="${letter}">
      <div class="group-head" style="--accent:${TOPIC_ACCENT[letter] || "#ff8c00"}">
        <span class="group-bullet">—</span>
        <span class="group-label">${escapeHtml(label)}</span>
        <span class="group-count">${items.length}</span>
        <span class="group-line"></span>
      </div>
      <div class="group-body">${items.map(renderItem).join("")}</div>
    </div>`;
}

function render() {
  const root = document.getElementById("content");
  if (!root) return;
  const visible = filterItems();
  if (!STATE.items.length) {
    root.innerHTML = '<p class="empty">Лента пока пуста.</p>';
    return;
  }
  if (!visible.length) {
    root.innerHTML = '<p class="empty">Нет новостей по выбранному фильтру.</p>';
    return;
  }
  const groups = groupByTopic(visible);
  let html = "";
  for (const topic of TOPIC_ORDER) {
    html += renderGroup(topic, TOPIC_LABELS[topic] || topic, groups.get(topic) || []);
  }
  if (STATE.activeFilter === "ALL") {
    root.innerHTML = html;
  } else {
    root.innerHTML = renderGroup(STATE.activeFilter, TOPIC_LABELS[STATE.activeFilter], groups.get(STATE.activeFilter) || []);
  }
  bindRowToggles(root);
}

function bindRowToggles(root) {
  root.querySelectorAll(".item").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      const body = row.querySelector(".item-body");
      if (!body) return;
      if (body.hasAttribute("hidden")) {
        body.removeAttribute("hidden");
        row.classList.add("expanded");
      } else {
        body.setAttribute("hidden", "");
        row.classList.remove("expanded");
      }
    });
  });
}

function renderMeta() {
  const count = document.getElementById("meta-count");
  const last24 = document.getElementById("meta-24h");
  const src = document.getElementById("meta-src");
  const updated = document.getElementById("meta-updated");
  if (count) count.textContent = STATE.items.length;
  if (last24) {
    const cutoff = Date.now() - 24 * 36e5;
    const recent = STATE.items.filter((it) => {
      const t = parseTime(it.published);
      return t && t.valueOf() >= cutoff;
    }).length;
    last24.textContent = recent;
  }
  if (src) src.textContent = STATE.sourceCount || new Set(STATE.items.map((i) => i.source).filter(Boolean)).size;
  if (updated) updated.textContent = formatFull(STATE.generatedAt);
}

function setStatus(state, text) {
  const pill = document.getElementById("status-state");
  const label = document.getElementById("status-text");
  if (pill) {
    pill.classList.remove("ok", "warn", "err");
    pill.classList.add(state);
    pill.textContent = state === "err" ? "ОШИБКА" : state === "warn" ? "ВНИМАНИЕ" : "ОНЛАЙН";
  }
  if (label && text) label.textContent = text;
}

async function loadFeed() {
  try {
    const response = await fetch("./data/news.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    STATE.items = Array.isArray(data.items) ? data.items : [];
    STATE.generatedAt = data.generatedAt;
    STATE.sourceCount = data.sourceCount || 0;
    setStatus("ok", `Обновлено: ${formatFull(STATE.generatedAt)} · источников: ${STATE.sourceCount}`);
    renderMeta();
    renderFilters();
    render();
  } catch (error) {
    setStatus("err", `Не удалось загрузить ленту: ${error.message}`);
    const root = document.getElementById("content");
    if (root) {
      root.innerHTML = `<p class="empty">Лента недоступна. Проверьте <a href="https://github.com/andrew-ledovich/velo-news-ru/actions" target="_blank" rel="noopener">Actions</a>.</p>`;
    }
  }
}

function wireSearch() {
  const input = document.getElementById("search-input");
  if (!input) return;
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      STATE.query = input.value;
      render();
    }, 120);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wireSearch();
  loadFeed();
});
