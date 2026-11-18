"""状态库与产物：去重索引、月度归档、滚动 digest、站内 RSS、站点 meta。"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

from common import (
    ARCHIVE_DIR,
    DATA_DIR,
    DOCS_DATA,
    log,
    normalize_title_key,
    read_json,
    truncate,
    write_json,
)

STATE_PATH = DATA_DIR / "state.json"
DIGEST_PATH = DOCS_DATA / "digest.json"
META_PATH = DOCS_DATA / "meta.json"
RSS_PATH = DOCS_DATA / "feed.xml"

STATE_RETAIN_DAYS = 240
STATE_MAX_ENTRIES = 40000
DIGEST_DAYS = 120
DIGEST_MAX_ITEMS = 800


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def dedupe(items: list[dict]) -> list[dict]:
    """跨源去重：DOI 优先，其次标题指纹（可合并预印本与正式版）。"""
    by_key: dict[str, dict] = {}
    by_title: dict[str, str] = {}

    for it in items:
        tkey = normalize_title_key(it.get("title", ""))
        it["_tkey"] = tkey
        key = it["id"]
        # 同标题不同 DOI（预印本→正式发表）视作同一文献
        if tkey and tkey in by_title:
            key = by_title[tkey]
        elif tkey:
            by_title[tkey] = key

        if key not in by_key:
            by_key[key] = it
            continue
        _merge(by_key[key], it)

    for it in by_key.values():
        it.pop("_tkey", None)
        it.setdefault("sources", [it.get("source", "")])
        it.setdefault("source_count", len(it["sources"]))
    return list(by_key.values())


def _merge(base: dict, extra: dict) -> None:
    """把两条同一文献的记录合并：缺的字段补上，摘要取更长的那条。"""
    if len(extra.get("abstract") or "") > len(base.get("abstract") or ""):
        base["abstract"] = extra["abstract"]
    for field in ("journal", "date", "url", "doi", "pmid"):
        if not base.get(field) and extra.get(field):
            base[field] = extra[field]
    if len(extra.get("authors") or []) > len(base.get("authors") or []):
        base["authors"] = extra["authors"]
    # 记录还出现在哪些源里，网页上可显示「多源命中」
    srcs = base.setdefault("sources", [base.get("source", "")])
    if extra.get("source") and extra["source"] not in srcs:
        srcs.append(extra["source"])
    base["source_count"] = len(srcs)
    if extra.get("type") == "preprint" or "biorxiv" in (extra.get("source_id") or ""):
        base["preprint"] = True


def is_seen(state: dict, item: dict) -> bool:
    return item["id"] in state["items"]


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def load_state() -> dict:
    state = read_json(STATE_PATH, None) or {}
    state.setdefault("version", 1)
    state.setdefault("items", {})
    return state


def update_state(state: dict, items: list[dict], today: dt.date, threshold: float) -> dict:
    today_s = today.isoformat()
    for it in items:
        entry = state["items"].get(it["id"]) or {}
        kept = float(it.get("score") or 0) >= threshold
        entry.update(
            {
                "seen": entry.get("seen") or today_s,
                "last_score": it.get("score"),
                "status": "kept" if kept else "dropped",
            }
        )
        state["items"][it["id"]] = entry

    # 修剪过期条目，避免 state.json 无限膨胀
    cutoff = (today - dt.timedelta(days=STATE_RETAIN_DAYS)).isoformat()
    pruned = {k: v for k, v in state["items"].items() if (v.get("seen") or "") >= cutoff}
    if len(pruned) > STATE_MAX_ENTRIES:
        newest = sorted(pruned.items(), key=lambda kv: kv[1].get("seen") or "", reverse=True)
        pruned = dict(newest[:STATE_MAX_ENTRIES])
    state["items"] = pruned
    state["updated"] = today_s
    return state


def save_state(state: dict) -> None:
    write_json(STATE_PATH, state)
    log(f"状态库已更新：{len(state['items'])} 条历史记录")


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def _archive_path(month: str):
    return ARCHIVE_DIR / f"{month}.json"


def load_archive(month: str) -> list[dict]:
    data = read_json(_archive_path(month), None) or {}
    return data.get("items") or []


def append_archive(items: list[dict], today: dt.date, today_s: str) -> dict[str, int]:
    """把新接收的文献写进当月归档（按 id 去重，已存在的保留首次记录）。"""
    months: dict[str, list[dict]] = {}
    for it in items:
        month = (it.get("date") or today_s)[:7]
        if not month or month < "1990-01":
            month = today_s[:7]
        months.setdefault(month, []).append(it)

    stats: dict[str, int] = {}
    for month, batch in months.items():
        existing = load_archive(month)
        index = {x["id"]: x for x in existing}
        added = 0
        for it in batch:
            if it["id"] in index:
                # 已存在则只刷新分数/理由（实验室方向调整后重算会走到这里）
                old = index[it["id"]]
                old.update(
                    {
                        "score": it["score"],
                        "reason": it.get("reason") or old.get("reason"),
                        "tags": it.get("tags") or old.get("tags") or [],
                        "llm": it.get("llm", old.get("llm")),
                    }
                )
                continue
            index[it["id"]] = _slim(it, today_s)
            added += 1
        merged = sorted(index.values(), key=lambda x: (x.get("date") or "", x.get("score") or 0), reverse=True)
        write_json(_archive_path(month), {"month": month, "count": len(merged), "items": merged})
        stats[month] = added
        if added:
            log(f"  归档 {month}: +{added} 篇（累计 {len(merged)}）")
    return stats


def _slim(it: dict, today_s: str) -> dict:
    """只保留网页需要的字段，控制归档体积。"""
    return {
        "id": it["id"],
        "title": it.get("title", ""),
        "abstract": truncate(it.get("abstract") or "", 4000),
        "doi": it.get("doi") or "",
        "pmid": it.get("pmid") or "",
        "journal": it.get("journal") or "",
        "date": it.get("date") or "",
        "url": it.get("url") or "",
        "authors": (it.get("authors") or [])[:8],
        "source": it.get("source") or "",
        "group": it.get("group") or "",
        "score": it.get("score"),
        "reason": truncate(it.get("reason") or "", 200),
        "tags": (it.get("tags") or [])[:3],
        "type": it.get("type") or "research",
        "llm": bool(it.get("llm")),
        "added": today_s,
        "sources": (it.get("sources") or [])[:4],
        "preprint": bool(it.get("preprint")),
    }


def rewrite_archive(month: str, items: list[dict]) -> None:
    """整月覆盖写入（重算分数后使用）。"""
    items = sorted(items, key=lambda x: (x.get("date") or "", x.get("score") or 0), reverse=True)
    write_json(_archive_path(month), {"month": month, "count": len(items), "items": items})


def list_months() -> list[str]:
    if not ARCHIVE_DIR.exists():
        return []
    return sorted((p.stem for p in ARCHIVE_DIR.glob("*.json")), reverse=True)


def build_digest(today: dt.date, days: int = DIGEST_DAYS) -> list[dict]:
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    pool: dict[str, dict] = {}
    for month in list_months():
        if month < cutoff[:7]:
            continue
        for it in load_archive(month):
            if (it.get("date") or "") >= cutoff or (it.get("added") or "") >= cutoff:
                pool[it["id"]] = it
    items = sorted(pool.values(), key=lambda x: (x.get("date") or "", x.get("score") or 0), reverse=True)
    return items[:DIGEST_MAX_ITEMS]


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
def write_digest(items: list[dict], today: dt.date) -> None:
    write_json(DIGEST_PATH, {"generated": today.isoformat(), "count": len(items), "items": items})
    log(f"digest.json 已写入：{len(items)} 篇（近 {DIGEST_DAYS} 天）")


def write_meta(
    lab_cfg: dict,
    items: list[dict],
    feed_cfg: dict,
    *,
    mode: str,
    run_stats: dict,
    today: dt.date,
) -> None:
    lab = lab_cfg.get("lab") or {}
    scoring = lab_cfg.get("scoring") or {}
    now = dt.datetime.now(dt.timezone.utc)

    by_group: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for it in items:
        g = it.get("group") or "其它"
        by_group[g] = by_group.get(g, 0) + 1
        for t in it.get("tags") or []:
            by_tag[t] = by_tag.get(t, 0) + 1

    month = today.isoformat()[:7]
    this_month = sum(1 for it in items if (it.get("date") or "").startswith(month))
    week_ago = (today - dt.timedelta(days=7)).isoformat()

    meta = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_local": now.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "mode": mode,
        "lab": {
            "name": lab.get("name") or "Lab",
            "name_en": lab.get("name_en") or "",
            "tagline": lab.get("tagline") or "",
            "profile": (lab.get("profile") or "").strip(),
            "interests": [
                {"key": g.get("key"), "label": g.get("label")}
                for g in lab.get("interests") or []
            ],
        },
        "thresholds": {
            "min_score": scoring.get("min_score", 55),
            "max_per_run": scoring.get("max_per_run", 25),
        },
        "stats": {
            "total": len(items),
            "this_month": this_month,
            "last_7_days": sum(1 for it in items if (it.get("added") or "") >= week_ago),
            "high_relevance": sum(1 for it in items if float(it.get("score") or 0) >= 80),
            "avg_score": round(sum(float(it.get("score") or 0) for it in items) / len(items), 1)
            if items
            else 0,
        },
        "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1])),
        "by_tag": dict(sorted(by_tag.items(), key=lambda kv: -kv[1])[:20]),
        "months": list_months(),
        "feeds": [
            {
                "id": f.get("id"),
                "name": f.get("name") or f.get("id"),
                "group": f.get("group", "其它"),
                "type": f.get("type"),
                "enabled": f.get("enabled", True),
            }
            for f in (feed_cfg.get("feeds") or [])
        ],
        "run": run_stats,
        "license": "仅用于实验室内部文献速览，摘要版权归原作者与出版方所有",
    }
    write_json(META_PATH, meta)
    log("meta.json 已写入")


def write_rss(items: list[dict], lab_cfg: dict, limit: int = 40) -> None:
    lab = (lab_cfg.get("lab") or {}).get("name") or "Lab"
    now = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    entries = []
    for it in items[:limit]:
        link = it.get("url") or (f"https://doi.org/{it['doi']}" if it.get("doi") else "")
        abstract = truncate(it.get("abstract") or "", 900)
        reason = it.get("reason") or ""
        desc = (
            f"<p><b>相关度 {it.get('score')}</b> · {html.escape(reason)}</p>"
            f"<p>{html.escape(abstract)}</p>"
            f'<p><a href="{html.escape(link)}">原文</a></p>'
        )
        pub = it.get("date") or ""
        if pub:
            try:
                pub = dt.datetime.strptime(pub, "%Y-%m-%d").strftime("%a, %d %b %Y 00:00:00 +0000")
            except ValueError:
                pub = ""
        entries.append(
            "<item>"
            f"<title>{html.escape(it.get('title', ''))}</title>"
            f"<link>{html.escape(link)}</link>"
            f'<guid isPermaLink="false">{html.escape(it["id"])}</guid>'
            f"<description><![CDATA[{desc}]]></description>"
            + (f"<pubDate>{pub}</pubDate>" if pub else "")
            + f"<category>{html.escape(it.get('journal') or '')}</category>"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>{html.escape(lab)} · 文献速览</title>"
        "<link>./</link>"
        f"<description>{html.escape(lab)} 相关文献自动速览（按相关度排序）</description>"
        "<language>zh-CN</language>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        + "".join(entries)
        + "</channel></rss>\n"
    )
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    RSS_PATH.write_text(xml, encoding="utf-8")
    log(f"feed.xml 已写入：{len(entries)} 条")
