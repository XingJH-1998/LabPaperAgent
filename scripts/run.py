#!/usr/bin/env python3
"""LabPaperAgent 主流程。

    python scripts/run.py                  # 正常抓取 + 打分 + 出网页数据
    python scripts/run.py --dry-run        # 只抓取和打分，不写任何文件
    python scripts/run.py --no-llm         # 强制只用关键词打分（不花钱）
    python scripts/run.py --rescore        # 用当前 lab.yml 重算已有文献
    python scripts/run.py --only oa-sdm,rss-biorxiv --lookback 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402
from common import load_config, log, today_utc, warn  # noqa: E402
from enrich import Enricher  # noqa: E402
from fetchers import fetch_all  # noqa: E402
from scoring import KeywordScorer, resolve_llm, score_all  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="实验室文献自动速览")
    p.add_argument("--lookback", type=int, default=None, help="回溯天数（覆盖各 feed 设置）")
    p.add_argument("--only", default=None, help="只跑指定 feed id，逗号分隔")
    p.add_argument("--no-llm", action="store_true", help="只用关键词打分")
    p.add_argument("--limit-llm", type=int, default=None, help="覆盖单次 LLM 打分条数上限")
    p.add_argument("--min-score", type=float, default=None, help="覆盖相关度阈值")
    p.add_argument("--rescore", action="store_true", help="按当前 lab.yml 重算已有文献")
    p.add_argument("--dry-run", action="store_true", help="不写文件")
    p.add_argument("--no-notify", action="store_true", help="不发通知")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    started = time.time()
    today = today_utc()
    lab_cfg, feeds_cfg = load_config()

    lab_name = (lab_cfg.get("lab") or {}).get("name", "Lab")
    scoring_cfg = lab_cfg.get("scoring") or {}
    min_score = float(args.min_score if args.min_score is not None else scoring_cfg.get("min_score", 55))
    max_per_run = int(scoring_cfg.get("max_per_run", 25))

    log("=" * 66)
    log(f"{lab_name} · 文献速览  {today.isoformat()}")
    log(f"阈值 {min_score} / 单次上限 {max_per_run} 篇")

    # ---------------------------------------------------------------- 重算模式
    if args.rescore:
        return run_rescore(args, lab_cfg, feeds_cfg, today, min_score, max_per_run)

    # ---------------------------------------------------------------- 正常抓取
    if args.no_llm:
        lab_cfg.setdefault("llm", {})["enabled"] = False
    if args.limit_llm:
        lab_cfg.setdefault("scoring", {})["max_llm_items"] = args.limit_llm

    feeds = feeds_cfg.get("feeds") or []
    enabled = [f for f in feeds if f.get("enabled", True)]
    if not enabled:
        warn("config/feeds.yml 里没有启用的订阅源")
        return 2

    max_lookback = args.lookback or max(
        int(f.get("lookback_days") or (feeds_cfg.get("defaults") or {}).get("lookback_days") or 14)
        for f in enabled
    )
    log(f"订阅源 {len(enabled)} 个，最长回溯 {max_lookback} 天")

    raw = fetch_all(feeds_cfg, max_lookback, _only_list(args.only))
    fetched = len(raw)
    if not fetched:
        warn("没有抓取到任何文献，检查网络或 feeds.yml")
        return 1

    # ---------------------------------------------------------------- 清洗去重
    items = store.dedupe(raw)
    log(f"去重：{fetched} → {len(items)} 篇")

    # 窗口过滤（防止兜底源返回很旧的文献）
    window_start = (today - dt.timedelta(days=max_lookback + 7)).isoformat()
    fresh = []
    for it in items:
        d = it.get("date") or ""
        dep = it.get("deposited") or ""
        if not d:
            it["date"] = dep or today.isoformat()
            it["date_estimated"] = True
            d = it["date"]
        # 网络见刊日（deposited）在窗口内也算新，避免纸本期次日期把新文章筛掉
        if d >= window_start or (dep and dep >= window_start):
            fresh.append(it)
    if len(fresh) != len(items):
        log(f"时间窗过滤：剔除 {len(items) - len(fresh)} 篇过旧文献")
    items = fresh

    # ---------------------------------------------------------------- 去重（历史）
    state = store.load_state()
    new_items = [it for it in items if not store.is_seen(state, it)]
    log(f"历史去重：新增 {len(new_items)} 篇 / 已看过 {len(items) - len(new_items)} 篇")
    if not new_items:
        log("今天没有新文献。")
        _finish_outputs(lab_cfg, feeds_cfg, today, [], {"fetched": fetched, "new": 0, "kept": 0}, args)
        return 0

    # ---------------------------------------------------------------- 字段回填
    # 回填有次数上限，先按标题关键词分排序，让最可能相关的文献优先拿到摘要
    kw = KeywordScorer(lab_cfg)
    new_items.sort(key=lambda it: -kw.raw_score(it)[0])
    enricher = Enricher(
        min_abstract_chars=int(scoring_cfg.get("min_abstract_chars", 120)),
        max_lookups=int(scoring_cfg.get("max_enrich", 400)),
    )
    new_items = enricher.enrich(new_items)

    # ---------------------------------------------------------------- 打分
    new_items = score_all(new_items, lab_cfg, vars(args))

    # ---------------------------------------------------------------- 筛选
    kept = [it for it in new_items if float(it.get("score") or 0) >= min_score]
    kept.sort(key=lambda x: (float(x.get("score") or 0), x.get("date") or ""), reverse=True)

    deferred: list[dict] = []
    if len(kept) > max_per_run:
        deferred = kept[max_per_run:]
        kept = kept[:max_per_run]
        log(f"超过单次上限 {max_per_run}，{len(deferred)} 篇顺延到下次（不标记为已读）")
    deferred_ids = {it["id"] for it in deferred}

    log("-" * 66)
    log(f"结果：{len(new_items)} 篇新文献 → {len(kept)} 篇入选（>={min_score} 分）")
    for it in kept[:12]:
        flag = "LLM" if it.get("llm") else "KW "
        log(f"  {it['score']:>5.1f} [{flag}] {(it.get('date') or '')[:10]} {it['title'][:62]}")
    if len(kept) > 12:
        log(f"  …还有 {len(kept) - 12} 篇")

    # ---------------------------------------------------------------- 落盘
    stats = {"fetched": fetched, "new": len(new_items), "kept": len(kept)}
    if not args.dry_run:
        store.append_archive(kept, today, today.isoformat())
        marked = [it for it in new_items if it["id"] not in deferred_ids]
        state = store.update_state(state, marked, today, min_score)
        store.save_state(state)
        _finish_outputs(lab_cfg, feeds_cfg, today, kept, stats, args)

    mode = "llm" if any(it.get("llm") for it in new_items) else "keyword"
    log(f"完成，用时 {time.time() - started:.1f}s（打分模式：{mode}）")
    return 0


def _mode(lab_cfg: dict) -> str:
    if not (lab_cfg.get("llm") or {}).get("enabled", True):
        return "keyword"
    has_key = any(os.environ.get(k) for k in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"))
    return "llm" if has_key else "keyword"


def _finish_outputs(
    lab_cfg: dict,
    feeds_cfg: dict,
    today: dt.date,
    kept: list[dict],
    stats: dict,
    args: argparse.Namespace,
) -> None:
    if args.dry_run:
        log("--dry-run：跳过写文件")
        return
    digest = store.build_digest(today)
    store.write_digest(digest, today)
    store.write_meta(
        lab_cfg,
        digest,
        feeds_cfg,
        mode=_mode(lab_cfg),
        run_stats=stats,
        today=today,
        llm_info=resolve_llm(lab_cfg),
    )
    store.write_rss(digest, lab_cfg)

    if kept and not args.no_notify:
        try:
            from notify import notify_digest  # noqa: PLC0415

            notify_digest(kept, lab_cfg)
        except Exception as exc:  # 通知失败不能影响主流程
            warn(f"通知发送失败：{exc}")


def run_rescore(
    args: argparse.Namespace,
    lab_cfg: dict,
    feeds_cfg: dict,
    today: dt.date,
    min_score: float,
    max_per_run: int,
) -> int:
    """用当前 lab.yml 重新给已有文献打分（改了研究方向后使用）。"""
    if args.limit_llm:
        lab_cfg.setdefault("scoring", {})["max_llm_items"] = args.limit_llm
    months = store.list_months()
    if not months:
        warn("没有归档数据可重算")
        return 1

    cutoff = (today - dt.timedelta(days=store.DIGEST_DAYS)).isoformat()
    total = 0
    for month in months:
        if month < cutoff[:7]:
            continue
        items = store.load_archive(month)
        if not items:
            continue
        log(f"重算 {month}：{len(items)} 篇")
        scored = score_all(items, lab_cfg, vars(args))
        above = [it for it in scored if float(it.get("score") or 0) >= min_score]
        if not args.dry_run:
            store.rewrite_archive(month, above)
        total += len(scored)

    if not args.dry_run:
        digest = store.build_digest(today)
        store.write_digest(digest, today)
        store.write_meta(
            lab_cfg,
            digest,
            feeds_cfg,
            mode="llm" if any(it.get("llm") for it in digest) else "keyword",
            run_stats={"fetched": 0, "new": 0, "kept": len(digest), "rescore": total},
            today=today,
            llm_info=resolve_llm(lab_cfg),
        )
        store.write_rss(digest, lab_cfg)
        log(f"重算完成：{total} 篇文献，当前通过阈值 {len(digest)} 篇")
    else:
        log(f"--dry-run：重算完成 {total} 篇，未写文件")
    return 0


def _only_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


if __name__ == "__main__":
    sys.exit(main())
