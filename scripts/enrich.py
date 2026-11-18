"""摘要回填：期刊 TOC / RSS 常常只有标题，这里按 DOI 逐级找回摘要。

回退顺序（都是从 DOI 出发，不需要额外的检索）：
  1. OpenAlex 单篇查询   生态刊覆盖最好，且不在搜索集群、不受匿名限流影响
  2. Crossref            出版社自己存的摘要（Springer 等常缺）
  3. Europe PMC          生物医学刊
  4. Semantic Scholar    兜底，有 1 req/s 限速
"""

from __future__ import annotations

import concurrent.futures as futures
import threading
from typing import Any

import requests

from common import CONTACT, USER_AGENT, clean_abstract, log, norm_space, parse_date, truncate, warn

OPENALEX_WORK = "https://api.openalex.org/works/https://doi.org/{doi}"
CROSSREF_WORK = "https://api.crossref.org/works/{doi}"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
S2_WORK = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"

_lock = threading.Lock()
_thread_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _thread_local.session = s
    return s


def _abstract_from_inverted(inv: dict | None) -> str:
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    if not pos:
        return ""
    return " ".join(pos[k] for k in sorted(pos))


class Enricher:
    def __init__(self, min_abstract_chars: int = 120, max_lookups: int = 200, workers: int = 5) -> None:
        self.min_chars = min_abstract_chars
        self.max_lookups = max_lookups
        self.workers = workers
        self._doi_cache: dict[str, dict] = {}
        self.lookups = 0
        self.filled = 0
        self.sources_used: dict[str, int] = {}

    # ------------------------------------------------------------------
    def needs(self, item: dict) -> bool:
        return len(item.get("abstract") or "") < self.min_chars

    def enrich(self, items: list[dict]) -> list[dict]:
        todo = [i for i in items if self.needs(i) and i.get("doi")]
        if not todo:
            return items
        todo.sort(key=lambda i: len(i.get("abstract") or ""))
        todo = todo[: self.max_lookups]
        log(f"摘要回填：{len(todo)} 篇缺摘要（共 {len(items)} 篇候选）")

        by_doi: dict[str, dict] = {}
        for it in todo:
            by_doi.setdefault(it["doi"].lower(), it)

        done = 0
        with futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            job_map = {pool.submit(self._lookup, doi): doi for doi in by_doi}
            for fut in futures.as_completed(job_map):
                doi = job_map[fut]
                done += 1
                try:
                    meta = fut.result()
                except Exception as exc:  # 单篇失败不影响整体
                    warn(f"回填异常 {doi}: {exc.__class__.__name__}")
                    continue
                with _lock:
                    self.lookups += 1
                    if meta and meta.get("abstract"):
                        self.sources_used[meta.get("via", "?")] = (
                            self.sources_used.get(meta.get("via", "?"), 0) + 1
                        )
                if not meta:
                    continue
                item = by_doi[doi]
                if meta.get("abstract") and self.needs(item):
                    item["abstract"] = meta["abstract"]
                    item["abstract_source"] = meta.get("via", "")
                    with _lock:
                        self.filled += 1
                for key in ("journal", "date"):
                    if not item.get(key) and meta.get(key):
                        item[key] = meta[key]
                if done % 25 == 0:
                    log(f"  回填进度 {done}/{len(by_doi)}，已补 {self.filled} 篇")

        detail = "、".join(f"{k} {v}" for k, v in sorted(self.sources_used.items(), key=lambda kv: -kv[1]))
        log(f"摘要回填完成：补齐 {self.filled} 篇/{len(todo)} 篇" + (f"（{detail}）" if detail else ""))
        return items

    # ------------------------------------------------------------------
    def _lookup(self, doi: str) -> dict | None:
        for fn, name in (
            (self._from_openalex, "openalex"),
            (self._from_crossref, "crossref"),
            (self._from_europepmc, "europepmc"),
            (self._from_s2, "semantic_scholar"),
        ):
            try:
                meta = fn(doi)
            except Exception:
                meta = None
            if meta and meta.get("abstract"):
                meta["via"] = name
                return meta
            if meta and not meta.get("abstract"):
                continue
        return None

    # ------------------------------------------------------------------
    def _from_openalex(self, doi: str) -> dict | None:
        try:
            r = _session().get(
                OPENALEX_WORK.format(doi=doi), params={"mailto": CONTACT}, timeout=25
            )
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            w = r.json()
        except ValueError:
            return None
        src = ((w.get("primary_location") or {}).get("source") or {}) or {}
        date = parse_date(w.get("publication_date"))
        return {
            "abstract": clean_abstract(_abstract_from_inverted(w.get("abstract_inverted_index"))),
            "journal": src.get("display_name") or "",
            "date": date.isoformat() if date else "",
        }

    def _from_crossref(self, doi: str) -> dict | None:
        try:
            r = _session().get(CROSSREF_WORK.format(doi=doi), params={"mailto": CONTACT}, timeout=25)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            msg = (r.json() or {}).get("message") or {}
        except ValueError:
            return None
        date = parse_date(msg.get("published") or msg.get("issued"))
        return {
            "abstract": clean_abstract(msg.get("abstract") or ""),
            "journal": (msg.get("container-title") or [""])[0] or "",
            "date": date.isoformat() if date else "",
        }

    def _from_europepmc(self, doi: str) -> dict | None:
        try:
            r = _session().get(
                EUROPEPMC,
                params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": 1},
                timeout=25,
            )
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            results = ((r.json() or {}).get("resultList") or {}).get("result") or []
        except ValueError:
            return None
        if not results:
            return None
        rec = results[0]
        year = rec.get("pubYear")
        return {
            "abstract": clean_abstract(rec.get("abstractText") or ""),
            "journal": norm_space(rec.get("journalTitle") or ""),
            "date": f"{year}-01-01" if str(year or "").isdigit() else "",
        }

    def _from_s2(self, doi: str) -> dict | None:
        try:
            r = _session().get(
                S2_WORK.format(doi=doi),
                params={"fields": "title,abstract,venue,year"},
                timeout=20,
            )
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            d = r.json()
        except ValueError:
            return None
        year = d.get("year")
        return {
            "abstract": clean_abstract(d.get("abstract") or ""),
            "journal": norm_space(d.get("venue") or ""),
            "date": f"{year}-01-01" if str(year or "").isdigit() else "",
        }
