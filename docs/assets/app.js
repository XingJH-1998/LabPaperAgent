/* Lab Paper Digest — 前端渲染与筛选（纯原生 JS，无构建步骤） */
(() => {
  "use strict";

  const DATA_URL = "./data/digest.json";
  const META_URL = "./data/meta.json";
  const PAGE_SIZE = 24;
  const NEW_DAYS = 3;

  const TYPE_LABEL = {
    research: "研究",
    review: "综述",
    methods: "方法",
    dataset: "数据",
    news: "资讯",
    other: "",
  };

  const state = {
    items: [],
    meta: null,
    q: "",
    tag: "",
    group: "",
    minScore: 0,
    sort: "score",
    unreadOnly: false,
    starOnly: false,
    month: "",
    page: 1,
  };

  const $ = (id) => document.getElementById(id);
  const read = loadSet("lpa:read");
  const starred = loadSet("lpa:star");

  function loadSet(key) {
    try {
      return new Set(JSON.parse(localStorage.getItem(key) || "[]"));
    } catch {
      return new Set();
    }
  }
  function saveSet(key, set) {
    try {
      localStorage.setItem(key, JSON.stringify([...set]));
    } catch {
      /* 隐私模式下忽略 */
    }
  }

  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const plural = (n) => `${n} 篇`;
  const todayISO = () => new Date().toISOString().slice(0, 10);
  const daysAgo = (iso, n) => {
    const d = new Date(iso + "T00:00:00Z");
    return (Date.now() - d.getTime()) / 86400000 <= n;
  };

  // ------------------------------------------------------------------ 主题
  function initTheme() {
    const saved = localStorage.getItem("lpa:theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    // 支持 ?theme=dark 分享固定配色的链接（不写回 localStorage）
    const urlTheme = new URLSearchParams(location.search).get("theme");
    const initial =
      urlTheme === "dark" || urlTheme === "light" ? urlTheme : saved || (prefersDark ? "dark" : "light");
    document.documentElement.dataset.theme = initial;
    $("themeBtn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("lpa:theme", next);
    });
  }

  // ------------------------------------------------------------------ 数据
  async function boot() {
    initTheme();
    bindEvents();
    skeleton(true);
    try {
      const [meta, digest] = await Promise.all([
        fetch(META_URL, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)),
        fetch(DATA_URL, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)),
      ]);
      state.meta = meta;
      state.items = (digest && digest.items) || [];
    } catch (err) {
      skeleton(false);
      showEmpty("无法读取数据", "第一次运行 workflows 之后这里才会有内容。若已运行，检查 docs/data/ 是否已提交。");
      console.error(err);
      return;
    }
    skeleton(false);
    if (!state.items.length) {
      if (state.meta) renderMeta();
      showEmpty("还没有收录文献", "工作流已经跑通但今天没有达到阈值的文献，明天再来看看。");
      return;
    }
    renderMeta();
    renderTagChips();
    renderSources();
    renderMonths();
    render();
  }

  function renderMeta() {
    const meta = state.meta || {};
    const lab = meta.lab || {};
    const stats = meta.stats || {};

    document.title = `${lab.name || "实验室"} · 文献速览`;
    $("labName").textContent = lab.name || "文献速览";
    $("tagline").textContent = lab.tagline || lab.name_en || "";
    $("updatedAt").textContent = meta.generated_at_local
      ? `更新于 ${meta.generated_at_local}`
      : "更新时间未知";
    $("profileText").textContent = lab.profile || "（未配置研究方向）";
    $("minScoreNote").textContent = (meta.thresholds && meta.thresholds.min_score) || 55;
    $("thresholdNote").textContent = `收录标准：相关度 ≥ ${
      (meta.thresholds && meta.thresholds.min_score) || 55
    }，单日最多 ${(meta.thresholds && meta.thresholds.max_per_run) || 25} 篇。`;
    $("rssNote").innerHTML = '可以直接把这个站点加进 RSS 阅读器：<a href="./data/feed.xml">feed.xml</a>。';

    const groups = meta.by_group || {};
    $("statBar").hidden = false;
    $("statBar").innerHTML = [
      stat(stats.total || 0, "总收录"),
      stat(stats.this_month || 0, "本月"),
      stat(stats.high_relevance || 0, "≥80 分"),
      stat(Object.keys(groups).length, "来源分组"),
    ].join("");

    const mode = meta.mode === "llm" ? "LLM 精排" : "关键词打分";
    $("statBar").insertAdjacentHTML(
      "beforeend",
      `<span class="mode-pill ${meta.mode === "llm" ? "" : "warn"}" title="筛选引擎">${esc(mode)}</span>`
    );
    if (meta.mode !== "llm") {
      $("modeNotice").hidden = false;
      $("modeNotice").innerHTML =
        "当前为<strong>关键词打分</strong>模式：在 GitHub 仓库里添加 secret <code>LLM_API_KEY</code> 后会自动切换为 LLM 精排，筛选准确度明显更高。";
    }

    const interests = ((lab.interests || []).length ? lab.interests : []).map((g) => {
      const n = state.items.filter((it) => (it.tags || []).includes(g.label)).length;
      return `<li><span>${esc(g.label)}</span><span class="n">${n}</span></li>`;
    });
    $("interestList").innerHTML = interests.join("") || "<li>未配置细分方向</li>";

    $("footerMeta").textContent = `最后更新 ${meta.generated_at_local || "—"}`;
    const r = meta.run || {};
    if (r.fetched) {
      $("footerMeta").textContent += ` · 本轮抓取 ${r.fetched} 篇 / 新增 ${r.new} / 入选 ${r.kept}`;
    }
  }

  const stat = (n, label) => `<span class="stat"><b>${n}</b><span>${esc(label)}</span></span>`;

  function renderTagChips() {
    const counts = new Map();
    state.items.forEach((it) => (it.tags || []).forEach((t) => counts.set(t, (counts.get(t) || 0) + 1)));
    const tags = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 14);
    $("tagRow").innerHTML = tags
      .map(
        ([t, n]) =>
          `<button type="button" class="chip" data-tag="${esc(t)}" aria-pressed="${
            state.tag === t
          }">${esc(t)}<span class="n">${n}</span></button>`
      )
      .join("");
  }

  function renderSources() {
    const groups = (state.meta && state.meta.by_group) || {};
    const max = Math.max(1, ...Object.values(groups));
    $("sourceList").innerHTML = Object.entries(groups)
      .map(
        ([g, n]) => `<div class="source-item" data-group="${esc(g)}" role="button" tabindex="0">
          <div class="row"><span>${esc(g)}</span><span class="n">${n}</span></div>
          <div class="bar"><i style="width:${Math.round((n / max) * 100)}%"></i></div>
        </div>`
      )
      .join("");
  }

  function renderMonths() {
    const months = (state.meta && state.meta.months) || [];
    if (!months.length) {
      $("monthList").innerHTML = '<span class="notes-muted" style="font-size:12px">暂无归档</span>';
      return;
    }
    $("monthList").innerHTML = months
      .slice(0, 12)
      .map(
        (m) =>
          `<button type="button" class="month-btn" data-month="${esc(m)}" aria-pressed="${
            state.month === m
          }">${esc(m)}</button>`
      )
      .join("");
  }

  // ------------------------------------------------------------------ 筛选
  function filtered() {
    const q = state.q.trim().toLowerCase();
    let list = state.items.filter((it) => {
      if (state.minScore && Number(it.score || 0) < state.minScore) return false;
      if (state.tag && !(it.tags || []).includes(state.tag)) return false;
      if (state.group && (it.group || "其它") !== state.group) return false;
      if (state.unreadOnly && read.has(it.id)) return false;
      if (state.starOnly && !starred.has(it.id)) return false;
      if (q) {
        const hay = [it.title, it.abstract, it.journal, (it.authors || []).join(" "), (it.tags || []).join(" ")]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    if (state.sort === "date") {
      list.sort((a, b) => (b.date || "").localeCompare(a.date || "") || b.score - a.score);
    } else {
      list.sort((a, b) => b.score - a.score || (b.date || "").localeCompare(a.date || ""));
    }
    return list;
  }

  // ------------------------------------------------------------------ 渲染
  function render() {
    const list = filtered();
    const shown = list.slice(0, state.page * PAGE_SIZE);
    $("cards").innerHTML = shown.map(cardHTML).join("");

    const active = state.q || state.tag || state.group || state.minScore || state.unreadOnly || state.starOnly;
    $("resetBtn").hidden = !active;
    $("resultCount").textContent = list.length
      ? `显示 ${shown.length} / ${list.length} 篇${state.month ? `（${state.month} 归档）` : ""}`
      : "";

    $("moreBtn").hidden = shown.length >= list.length;
    $("moreBtn").textContent = `加载更多（还有 ${list.length - shown.length} 篇）`;

    if (!list.length) {
      let hint = "试试放宽相关度筛选，或清除搜索关键词。";
      if (state.starOnly) {
        hint = state.month
          ? `当前只看了 ${state.month} 的归档，这个月没有你收藏的文献。再点一次「归档」里的 ${state.month} 可以回到全部。`
          : "还没有收藏任何文献。点卡片右上角的 ☆ 收藏，再来按收藏筛选。";
      } else if (state.unreadOnly) {
        hint = "这一批都读完了。取消「未读」筛选可以回看已读文献。";
      } else if (state.month) {
        hint = `当前只看了 ${state.month} 的归档，里面没有符合这些条件的文献。再点一次月份按钮可回到全部。`;
      }
      showEmpty("没有符合条件的文献", hint);
    } else {
      $("emptyState").hidden = true;
    }
  }

  function cardHTML(it) {
    const score = Math.round(Number(it.score) || 0);
    const color = score >= 85 ? "var(--accent)" : score >= 70 ? "var(--gold)" : "var(--ink-3)";
    const isRead = read.has(it.id);
    const isStar = starred.has(it.id);
    const isNew = it.added && daysAgo(it.added, NEW_DAYS);
    const typeLabel = TYPE_LABEL[it.type] || "";

    const badges = [
      isNew ? '<span class="badge new">NEW</span>' : "",
      typeLabel && it.type !== "research" ? `<span class="badge">${esc(typeLabel)}</span>` : "",
      (it.sources || []).length > 1 ? `<span class="badge multi">多源命中</span>` : "",
      it.preprint ? '<span class="badge pre">预印本</span>' : "",
    ].join("");

    const tags = (it.tags || [])
      .slice(0, 4)
      .map((t) => `<span class="tag">${esc(t)}</span>`)
      .join("");

    const links = [];
    if (it.doi) {
      links.push(
        `<a class="action" href="https://doi.org/${esc(it.doi)}" target="_blank" rel="noopener">DOI ↗</a>`
      );
    }
    if (it.pmid) {
      links.push(
        `<a class="action" href="https://pubmed.ncbi.nlm.nih.gov/${esc(it.pmid)}/" target="_blank" rel="noopener">PubMed ↗</a>`
      );
    }
    if (it.url && !(it.doi && it.url.includes(it.doi))) {
      links.push(`<a class="action" href="${esc(it.url)}" target="_blank" rel="noopener">原文 ↗</a>`);
    }

    const authors = (it.authors || []).slice(0, 3).join(", ");
    const moreAuthors = (it.authors || []).length > 3 ? " 等" : "";

    return `<article class="card ${isRead ? "is-read" : ""}" data-id="${esc(it.id)}">
      <div class="score" style="--p:${score};--c:${color}" title="相关度 ${score} / 100">
        <span>${score}</span>
      </div>
      <div class="card-main">
        <div class="card-top">
          <h2><a href="${esc(it.url || (it.doi ? "https://doi.org/" + it.doi : "#"))}" target="_blank" rel="noopener" data-read>${esc(it.title)}</a></h2>
          <button type="button" class="star-btn" data-star aria-pressed="${isStar}" title="${
            isStar ? "取消收藏" : "收藏"
          }" aria-label="收藏">${isStar ? "★" : "☆"}</button>
        </div>
        <div class="meta">
          ${it.journal ? `<span class="journal">${esc(it.journal)}</span><span class="sep">·</span>` : ""}
          <time>${esc(it.date || "")}</time>
          ${authors ? `<span class="sep">·</span><span>${esc(authors)}${moreAuthors}</span>` : ""}
          ${badges}
        </div>
        ${tags ? `<div class="tags">${tags}</div>` : ""}
        ${
          it.reason
            ? `<div class="reason"><b>为什么推给你</b> · ${esc(it.reason)}</div>`
            : ""
        }
        ${
          it.abstract
            ? `<div class="abstract" data-abstract>${esc(it.abstract)}</div>`
            : '<div class="abstract" data-abstract style="color:var(--ink-3);font-style:italic">暂无摘要（点 DOI 查看原文）</div>'
        }
        <div class="card-actions">
          ${
            it.abstract
              ? '<button type="button" class="toggle-btn" data-toggle-abstract>展开摘要</button>'
              : ""
          }
          <span class="actions-right">
            ${links.join("")}
            <button type="button" class="toggle-btn" data-mark-read>${
              isRead ? "标为未读" : "标为已读"
            }</button>
          </span>
        </div>
      </div>
    </article>`;
  }

  function showEmpty(title, text) {
    $("emptyState").hidden = false;
    $("emptyState").innerHTML = `<strong>${esc(title)}</strong><p>${esc(text)}</p>`;
    $("cards").innerHTML = "";
    $("moreBtn").hidden = true;
    $("resultCount").textContent = "";
  }

  function skeleton(on) {
    if (!on) {
      $("skeleton").innerHTML = "";
      return;
    }
    $("skeleton").innerHTML = Array.from({ length: 3 })
      .map(
        () => `<div class="sk">
          <div class="sk-line w80"></div>
          <div class="sk-line w35"></div>
          <div class="sk-line w55"></div>
        </div>`
      )
      .join("");
  }

  // ------------------------------------------------------------------ 交互
  function bindEvents() {
    $("search").addEventListener("input", (e) => {
      state.q = e.target.value;
      state.page = 1;
      render();
    });

    $("sort").addEventListener("change", (e) => {
      state.sort = e.target.value;
      state.page = 1;
      render();
    });

    $("minScore").addEventListener("change", (e) => {
      state.minScore = Number(e.target.value) || 0;
      state.page = 1;
      render();
    });

    document.querySelectorAll("[data-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const kind = btn.dataset.toggle;
        if (kind === "unread") {
          state.unreadOnly = !state.unreadOnly;
          btn.setAttribute("aria-pressed", String(state.unreadOnly));
        } else if (kind === "star") {
          state.starOnly = !state.starOnly;
          btn.setAttribute("aria-pressed", String(state.starOnly));
        }
        state.page = 1;
        render();
      });
    });

    $("tagRow").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tag]");
      if (!btn) return;
      state.tag = state.tag === btn.dataset.tag ? "" : btn.dataset.tag;
      state.page = 1;
      renderTagChips();
      render();
    });

    $("sourceList").addEventListener("click", (e) => {
      const item = e.target.closest("[data-group]");
      if (!item) return;
      state.group = state.group === item.dataset.group ? "" : item.dataset.group;
      state.page = 1;
      render();
    });

    $("monthList").addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-month]");
      if (!btn) return;
      const month = btn.dataset.month;
      if (state.month === month) {
        state.month = "";
        state.items = await loadDigest();
      } else {
        const ok = await loadMonth(month);
        if (!ok) return;
      }
      renderMonths();
      renderTagChips();
      render();
    });

    $("resetBtn").addEventListener("click", async () => {
      state.q = "";
      state.tag = "";
      state.group = "";
      state.minScore = 0;
      state.unreadOnly = false;
      state.starOnly = false;
      state.sort = "score";
      state.page = 1;
      $("search").value = "";
      $("minScore").value = "0";
      $("sort").value = "score";
      document.querySelectorAll("[data-toggle]").forEach((b) => b.setAttribute("aria-pressed", "false"));
      if (state.month) {
        // 清除筛选时一并退出历史月份视图，否则会一直停留在归档里
        state.month = "";
        state.items = await loadDigest();
        renderMonths();
      }
      renderTagChips();
      render();
    });

    $("moreBtn").addEventListener("click", () => {
      state.page += 1;
      render();
    });

    $("cards").addEventListener("click", (e) => {
      const card = e.target.closest(".card");
      if (!card) return;
      const id = card.dataset.id;

      if (e.target.closest("[data-toggle-abstract]")) {
        const abs = card.querySelector("[data-abstract]");
        const btn = e.target.closest("[data-toggle-abstract]");
        const expanded = abs.classList.toggle("expanded");
        btn.textContent = expanded ? "收起摘要" : "展开摘要";
        if (expanded) markRead(id, true);
        return;
      }

      if (e.target.closest("[data-star]")) {
        if (starred.has(id)) starred.delete(id);
        else starred.add(id);
        saveSet("lpa:star", starred);
        render();
        return;
      }

      if (e.target.closest("[data-mark-read]")) {
        markRead(id, !read.has(id));
        render();
        return;
      }

      if (e.target.closest("[data-read]")) {
        markRead(id, true);
      }
    });

    $("exportBtn").addEventListener("click", (e) => {
      e.preventDefault();
      exportStarred();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && document.activeElement !== $("search")) {
        e.preventDefault();
        $("search").focus();
      } else if (e.key === "Escape" && document.activeElement === $("search")) {
        $("search").value = "";
        state.q = "";
        render();
      }
    });
  }

  function markRead(id, on) {
    if (on) read.add(id);
    else read.delete(id);
    saveSet("lpa:read", read);
  }

  async function loadDigest() {
    try {
      const d = await fetch(DATA_URL, { cache: "no-cache" }).then((r) => r.json());
      return d.items || [];
    } catch {
      return state.items;
    }
  }

  async function loadMonth(month) {
    try {
      const d = await fetch(`./data/archive/${month}.json`, { cache: "no-cache" }).then((r) => r.json());
      if (!d.items || !d.items.length) throw new Error("empty");
      state.items = d.items;
      state.month = month;
      return true;
    } catch {
      showEmpty(`${month} 没有归档数据`, "该月的归档文件不存在，或尚未生成。");
      return false;
    }
  }

  function exportStarred() {
    const picks = state.items.filter((it) => starred.has(it.id));
    if (!picks.length) {
      alert("还没有收藏任何文献。点卡片右上角的 ☆ 收藏后再导出。");
      return;
    }
    const md = [
      `# 收藏文献（${picks.length} 篇）`,
      `导出时间：${new Date().toLocaleString("zh-CN")}`,
      "",
      ...picks.map((it, i) => {
        const authors = (it.authors || []).join(", ");
        return [
          `${i + 1}. **${it.title}**`,
          `   ${authors ? authors + ". " : ""}${it.journal || ""}${it.date ? `, ${it.date}` : ""}`,
          it.doi ? `   DOI: [${it.doi}](https://doi.org/${it.doi})` : "",
          it.reason ? `   相关度 ${Math.round(it.score)}：${it.reason}` : "",
          "",
        ]
          .filter(Boolean)
          .join("\n");
      }),
    ].join("\n");

    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `收藏文献-${todayISO()}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  boot();
})();
