const STATE = {
  items: [],
  meta: {},
  activeFilter: "all",
  query: "",
  generatedAt: null,
};

const TRANSLATION_STATUS = {
  translated: { label: "", className: "" },
  failed: { label: "RU ОШИБКА", className: "translation-failed" },
  original: { label: "RU ОРИГИНАЛ", className: "translation-failed" },
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

function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.valueOf())) return "—";
  return date.toLocaleString("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function relativeTime(iso) {
  if (!iso) return { label: "—", className: "" };
  const date = new Date(iso);
  if (Number.isNaN(date.valueOf())) return { label: "—", className: "" };
  const diffMs = Date.now() - date.valueOf();
  const hours = diffMs / 36e5;
  let label;
  let className = "";
  if (hours < 1) {
    label = "<1ч";
    className = "recent";
  } else if (hours < 24) {
    label = `${Math.round(hours)}ч`;
    className = "recent";
  } else if (hours < 24 * 7) {
    label = `${Math.round(hours / 24)}д`;
  } else {
    label = formatTime(iso);
    className = "stale";
  }
  return { label, className };
}

function uniqueSource(items) {
  const set = new Set();
  for (const item of items) {
    if (item.source) set.add(item.source);
  }
  return [...set].sort();
}

function buildFilters(items) {
  const filters = [{ id: "all", label: "все", count: items.length }];
  for (const source of uniqueSource(items)) {
    const count = items.filter((item) => item.source === source).length;
    filters.push({ id: `source:${source}`, label: source, count });
  }
  return filters;
}

function filterItems(items, activeFilter, query) {
  let visible = items;
  if (activeFilter && activeFilter.startsWith("source:")) {
    const source = activeFilter.slice("source:".length);
    visible = visible.filter((item) => item.source === source);
  }
  const trimmed = query.trim().toLowerCase();
  if (trimmed) {
    visible = visible.filter((item) => {
      const haystack = [
        item.titleRu,
        item.titleEn,
        item.source,
        ...(item.coins || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(trimmed);
    });
  }
  return visible;
}

function renderFilters() {
  const root = document.getElementById("filter-row");
  if (!root) return;
  const filters = buildFilters(STATE.items);
  root.innerHTML = filters
    .map((filter) => {
      const active = STATE.activeFilter === filter.id ? " active" : "";
      return `<button class="pill${active}" data-filter="${escapeHtml(filter.id)}">${escapeHtml(filter.label)}<span class="count">${filter.count}</span></button>`;
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
  const time = relativeTime(item.time);
  const timeClass = time.className;
  const translated = item.translationStatus || "translated";
  const status = TRANSLATION_STATUS[translated] || TRANSLATION_STATUS.translated;
  const titleClass = translated === "failed" || translated === "original" ? " item-title failed" : "";
  const coins = (item.coins || [])
    .map((coin) => `<span class="coin">${escapeHtml(coin)}</span>`)
    .join(" ");
  const source = escapeHtml(item.source || "Velo");
  const priority = Number(item.priority) || 3;
  const priorityLabel = priority === 1 ? "!" : priority === 2 ? "··" : "·";
  const link = item.link
    ? `<a class="open" href="${escapeHtml(item.link)}" target="_blank" rel="noopener">ОТКРЫТЬ ИСТОЧНИК ↗</a>`
    : `<a class="open" href="https://velo.xyz/news" target="_blank" rel="noopener">ОТКРЫТЬ VELO ↗</a>`;
  return `
    <div class="item" data-id="${escapeHtml(item.id)}">
      <span class="item-time ${timeClass}" title="${escapeHtml(formatTime(item.time))}">${escapeHtml(time.label)}</span>
      <span class="item-prio p${priority}" title="Приоритет ${priority}">${priorityLabel}</span>
      <div class="item-main">
        <div class="item-title${titleClass}">${escapeHtml(item.titleRu)}</div>
        <div class="item-meta">
          <span class="source">${source}</span>
          <span>${escapeHtml(formatTime(item.time))} МСК</span>
          ${coins}
          ${status.label ? `<span class="${status.className}">${status.label}</span>` : ""}
        </div>
        <div class="item-body" hidden>
          <p>${escapeHtml(item.titleRu)}</p>
          <p class="original">EN: ${escapeHtml(item.titleEn)}</p>
          ${link}
        </div>
      </div>
      <span class="item-arrow">▶</span>
    </div>`;
}

function render() {
  const root = document.getElementById("content");
  if (!root) return;
  const visible = filterItems(STATE.items, STATE.activeFilter, STATE.query);
  if (!STATE.items.length) {
    root.innerHTML = '<p class="empty">Лента пока пуста.</p>';
    return;
  }
  if (!visible.length) {
    root.innerHTML = '<p class="empty">Нет новостей по выбранному фильтру.</p>';
    return;
  }
  root.innerHTML = visible.map(renderItem).join("");
  root.querySelectorAll(".item").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      const body = row.querySelector(".item-body");
      if (!body) return;
      const expanded = !body.hasAttribute("hidden");
      if (expanded) {
        body.setAttribute("hidden", "");
        row.classList.remove("expanded");
      } else {
        body.removeAttribute("hidden");
        row.classList.add("expanded");
      }
    });
  });
}

function renderMeta() {
  const count = document.getElementById("meta-count");
  const last24 = document.getElementById("meta-24h");
  const updated = document.getElementById("meta-updated");
  if (count) count.textContent = STATE.items.length;
  if (last24) {
    const cutoff = Date.now() - 24 * 36e5;
    const recent = STATE.items.filter((item) => item.time && new Date(item.time).valueOf() >= cutoff).length;
    last24.textContent = recent;
  }
  if (updated) updated.textContent = formatTime(STATE.generatedAt);
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
    setStatus("ok", `Обновлено: ${formatTime(STATE.generatedAt)}`);
    renderMeta();
    renderFilters();
    render();
  } catch (error) {
    setStatus("err", `Не удалось загрузить ленту: ${error.message}`);
    const root = document.getElementById("content");
    if (root) root.innerHTML = `<p class="empty error">Лента недоступна. Откройте <a href="https://velo.xyz/news" target="_blank" rel="noopener">оригинал Velo</a>.</p>`;
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
