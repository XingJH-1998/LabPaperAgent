"""抓取层：把各订阅源（OpenAlex / PubMed / RSS / Crossref）统一成同一种 item。"""

from __future__ import annotations

import datetime as dt
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from common import (
    CONTACT,
    USER_AGENT,
    clean_abstract,
    clean_title,
    log,
    make_id,
    norm_space,
    normalize_doi,
    parse_date,
    today_utc,
    warn,
)

OPENALEX_URL = "https://api.openalex.org/works"
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
CROSSREF_URL = "https://api.crossref.org/works"

# OpenAlex 里明显不是研究论文的类型，直接丢弃
_SKIP_OPENALEX_TYPES = {"paratext", "peer-review", "editorial", "erratum", "other", "libguides"}
_PATH_TYPES = "article|review|preprint"

# 熔断器：OpenAlex 匿名搜索被限流时，后续源直接走 Crossref，避免每个源都重试
_OPENALEX_STATE = {"fails": 0, "disabled": False}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, */*"})
    return s


# ---------------------------------------------------------------------------
# 通用条目工厂
# ---------------------------------------------------------------------------
def build_item(
    *,
    title: str,
    abstract: str,
    doi: str,
    pmid: str,
    journal: str,
    date: dt.date | None,
    url: str,
    authors: list[str],
    source_id: str,
    source_name: str,
    source_group: str,
) -> dict[str, Any] | None:
    title = clean_title(title)
    if len(title) < 8:
        return None
    doi = normalize_doi(doi)
    abstract = clean_abstract(abstract)
    return {
        "id": make_id(doi, pmid, title),
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "pmid": pmid or "",
        "journal": norm_space(journal),
        "date": date.isoformat() if date else "",
        "url": url or (f"https://doi.org/{doi}" if doi else ""),
        "authors": [norm_space(a) for a in authors if norm_space(a)][:12],
        "source_id": source_id,
        "source": source_name,
        "group": source_group,
        "first_seen": today_utc().isoformat(),
    }


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------
def _openalex_abstract(work: dict) -> str:
    inv = work.get("abstract_inverted_index")
    if not inv:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return ""
    return " ".join(positions[k] for k in sorted(positions))


def _openalex_to_item(work: dict, feed: dict) -> dict | None:
    if work.get("type") in _SKIP_OPENALEX_TYPES:
        return None
    source = ((work.get("primary_location") or {}).get("source") or {}) or {}
    journal = source.get("display_name") or ""
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (work.get("authorships") or [])[:12]
    ]
    url = (
        (work.get("primary_location") or {}).get("landing_page_url")
        or (work.get("primary_location") or {}).get("pdf_url")
        or (work.get("doi") or "")
    )
    return build_item(
        title=work.get("title") or work.get("display_name") or "",
        abstract=_openalex_abstract(work),
        doi=work.get("doi") or "",
        pmid="",
        journal=journal,
        date=parse_date(work.get("publication_date")),
        url=url,
        authors=authors,
        source_id=feed["id"],
        source_name=feed.get("name") or feed["id"],
        source_group=feed.get("group", "其它"),
    )


def _request_json(session: requests.Session, url: str, params: dict, *, attempts: int = 4) -> dict | None:
    """带退避重试的 GET；尊重 Retry-After。返回 None 表示最终失败。"""
    last = ""
    for i in range(attempts):
        try:
            r = session.get(url, params=params, timeout=45)
        except requests.RequestException as exc:
            last = f"网络错误 {exc.__class__.__name__}"
            time.sleep(2 * (i + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                last = "响应不是 JSON"
                time.sleep(2)
                continue
        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 3 * (i + 1)
            except ValueError:
                wait = 3 * (i + 1)
            wait = min(wait, 25)
            last = f"HTTP {r.status_code}"
            time.sleep(wait)
            continue
        last = f"HTTP {r.status_code}"
        break
    warn(f"请求失败（{last}）: {url}")
    return None


def fetch_openalex(feed: dict, session: requests.Session, lookback: int) -> list[dict]:
    since = (today_utc() - dt.timedelta(days=lookback)).isoformat()
    base_filter = [f"from_publication_date:{since}", f"type:{_PATH_TYPES}"]
    if feed.get("issn"):
        base_filter.append(f"primary_location.source.issn:{feed['issn']}")

    queries = feed.get("queries") or ([feed["query"]] if feed.get("query") else [])
    per_query = max(10, (feed.get("max_items") or 40) // max(1, len(queries)) + 10)
    per_query = min(per_query, 200)

    items: list[dict] = []
    api_key = __import__("os").environ.get("OPENALEX_API_KEY", "").strip()

    if _OPENALEX_STATE["disabled"]:
        warn(f"[{feed['id']}] OpenAlex 已被限流熔断，直接用 Crossref")
        return fetch_crossref(feed, session, lookback)

    for q in queries or [None]:
        params: dict[str, Any] = {
            "filter": ",".join(base_filter),
            "per-page": per_query,
            "mailto": CONTACT,
        }
        if api_key:
            params["api_key"] = api_key
        if q:
            params["search"] = q
        else:
            params["sort"] = "publication_date:desc"

        data = _request_json(session, OPENALEX_URL, params, attempts=2)
        if data is None:
            _OPENALEX_STATE["fails"] += 1
            if _OPENALEX_STATE["fails"] >= 2 and not _OPENALEX_STATE["disabled"]:
                _OPENALEX_STATE["disabled"] = True
                warn(
                    "OpenAlex 连续失败，本轮剩余订阅源改走 Crossref。"
                    "配置 OPENALEX_API_KEY（免费）可显著提升稳定性。"
                )
            items.extend(fetch_crossref(feed, session, lookback))
            break
        for work in data.get("results", []):
            item = _openalex_to_item(work, feed)
            if item:
                items.append(item)
        time.sleep(0.4)

    # 期刊 TOC 如果 OpenAlex 恰好没有收录新文章，用 Crossref 补一次，避免整刊空白
    if not items and feed.get("issn") and not _OPENALEX_STATE["disabled"]:
        log(f"  [{feed['id']}] OpenAlex 无结果，尝试 Crossref 补位")
        items = fetch_crossref(feed, session, lookback)

    return items[: (feed.get("max_items") or 40)]


# ---------------------------------------------------------------------------
# Crossref（OpenAlex 兜底 / 独立使用）
# ---------------------------------------------------------------------------
def _crossref_to_item(it: dict, feed: dict) -> dict | None:
    title = (it.get("title") or [""])[0]
    journal = (it.get("container-title") or [""])[0]
    authors = []
    for a in (it.get("author") or [])[:12]:
        name = " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x).strip()
        if name:
            authors.append(name)
    date = parse_date(it.get("published") or it.get("issued") or it.get("created"))
    item = build_item(
        title=title,
        abstract=it.get("abstract") or "",
        doi=it.get("DOI") or "",
        pmid="",
        journal=journal,
        date=date,
        url=it.get("URL") or "",
        authors=authors,
        source_id=feed["id"],
        source_name=feed.get("name") or feed["id"],
        source_group=feed.get("group", "其它"),
    )
    if item:
        # DOI 注册时间 ≈ 网络见刊时间，比纸本期次日期新得多，用于时间窗判断
        created = parse_date(it.get("created"))
        if created:
            item["deposited"] = created.isoformat()
    return item


def fetch_crossref(feed: dict, session: requests.Session, lookback: int) -> list[dict]:
    since = (today_utc() - dt.timedelta(days=lookback)).isoformat()
    rows = min(feed.get("max_items") or 40, 100)
    params: dict[str, Any] = {
        "rows": rows,
        "mailto": CONTACT,
        "select": "DOI,title,abstract,container-title,published,author,URL,issued,created",
    }
    queries = feed.get("queries") or ([feed["query"]] if feed.get("query") else [])

    # 用 DOI 注册时间过滤：很多刊的 published 是纸本期次，会晚一个月，导致「看起来没有新文章」
    params["sort"] = "created"
    params["order"] = "desc"
    if feed.get("issn"):
        params["filter"] = f"issn:{feed['issn'].split('|')[0]},from-created-date:{since},type:journal-article"
        qlist = [None]
    else:
        params["filter"] = f"from-created-date:{since},type:journal-article"
        qlist = queries or [None]

    items: list[dict] = []
    for q in qlist:
        p = dict(params)
        if q:
            p["query.bibliographic"] = q
        data = _request_json(session, CROSSREF_URL, p, attempts=3)
        if not data:
            continue
        for it in data.get("message", {}).get("items", []):
            item = _crossref_to_item(it, feed)
            if item:
                items.append(item)
        time.sleep(0.3)
    return items[: (feed.get("max_items") or 40)]


# ---------------------------------------------------------------------------
# PubMed（E-utilities）
# ---------------------------------------------------------------------------
def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext())


def _pubmed_abstract(art: ET.Element) -> str:
    parts = []
    for at in art.findall(".//Abstract/AbstractText"):
        label = at.get("Label") or ""
        body = _text(at)
        if not body:
            continue
        parts.append(f"{label.upper()}: {body}" if label else body)
    return " ".join(parts)


def _pubmed_date(art: ET.Element) -> dt.date | None:
    pd = art.find(".//JournalIssue/PubDate")
    if pd is None:
        pd = art.find(".//ArticleDate")
    if pd is None:
        return None
    year = (pd.findtext("Year") or "").strip()
    month = (pd.findtext("Month") or "").strip()
    day = (pd.findtext("Day") or "").strip()
    if not year:
        medline = (pd.findtext("MedlineDate") or "").strip()
        m = __import__("re").match(r"(\d{4})", medline)
        return dt.date(int(m.group(1)), 1, 1) if m else None
    mnum = 1
    if month.isdigit():
        mnum = int(month)
    elif month:
        from common import _MONTHS  # noqa: PLC0415
        mnum = _MONTHS.get(month[:3].lower(), 1)
    try:
        return dt.date(int(year), max(1, min(12, mnum)), max(1, min(28, int(day or 1))))
    except ValueError:
        return None


def fetch_pubmed(feed: dict, session: requests.Session, lookback: int) -> list[dict]:
    term = norm_space(str(feed.get("term", "")))
    if not term:
        return []
    esearch_params = {
        "db": "pubmed",
        "term": term,
        "retmax": str(min(feed.get("max_items") or 30, 100)),
        "retmode": "json",
        "sort": "pub+date",
        "tool": "LabPaperAgent",
        "email": CONTACT,
    }
    reldate = int(feed.get("lookback_days") or lookback)
    if reldate > 0:
        esearch_params.update({"datetype": "pdat", "reldate": str(reldate)})

    data = _request_json(session, PUBMED_BASE + "esearch.fcgi", esearch_params)
    if not data:
        return []
    ids = (data.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []

    time.sleep(0.4)
    try:
        r = session.post(
            PUBMED_BASE + "efetch.fcgi",
            data={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
            timeout=60,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        warn(f"[{feed['id']}] efetch 失败: {exc}")
        return []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        warn(f"[{feed['id']}] PubMed XML 解析失败: {exc}")
        return []

    items: list[dict] = []
    for art in root.findall(".//PubmedArticle"):
        title_el = art.find(".//ArticleTitle")
        title = _text(title_el)
        if not title:
            continue
        journal = art.findtext(".//Journal/Title") or art.findtext(".//Journal/ISOAbbreviation") or ""
        pmid = art.findtext(".//MedlineCitation/PMID") or ""
        doi = ""
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            if (aid.get("IdType") or "").lower() == "doi":
                doi = _text(aid)
                break
        authors = []
        for a in art.findall(".//AuthorList/Author")[:12]:
            collective = a.findtext("CollectiveName")
            if collective:
                authors.append(collective)
                continue
            name = " ".join(
                x for x in [a.findtext("ForeName") or "", a.findtext("LastName") or ""] if x
            ).strip()
            if name:
                authors.append(name)
        pubtypes = [(_text(pt)).lower() for pt in art.findall(".//PublicationTypeList/PublicationType")]
        if any(t in ("retraction of publication", "retracted publication") for t in pubtypes):
            continue
        item = build_item(
            title=title,
            abstract=_pubmed_abstract(art),
            doi=doi,
            pmid=pmid,
            journal=journal,
            date=_pubmed_date(art),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            authors=authors,
            source_id=feed["id"],
            source_name=feed.get("name") or feed["id"],
            source_group=feed.get("group", "PubMed"),
        )
        if item:
            item["pubtype"] = pubtypes[:3]
            items.append(item)
    return items


# ---------------------------------------------------------------------------
# 通用 RSS / Atom
# ---------------------------------------------------------------------------
def fetch_rss(feed: dict, session: requests.Session, lookback: int) -> list[dict]:
    import feedparser

    url = feed.get("url", "")
    if not url:
        return []
    try:
        r = session.get(url, timeout=45, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as exc:
        warn(f"[{feed['id']}] RSS 拉取失败: {exc}")
        return []

    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        warn(f"[{feed['id']}] RSS 解析失败: {parsed.get('bozo_exception')}")
        return []

    items: list[dict] = []
    for e in parsed.entries:
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e["content"][0].get("value", "")
        link = e.get("link") or ""
        for l in e.get("links", []):
            if l.get("rel") == "alternate" and l.get("href"):
                link = l["href"]
                break
        doi = (
            e.get("prism_doi")
            or e.get("dc_identifier")
            or e.get("arxiv_doi")
            or ""
        )
        if not doi:
            for key in ("id", "link", "guid"):
                if e.get(key) and str(e.get(key)).startswith("10."):
                    doi = str(e.get(key))
                    break
        authors = []
        for a in e.get("authors", [])[:12]:
            name = a.get("name", "")
            if name:
                # bioRxiv 把整串作者塞进一个字段
                authors.extend([x.strip() for x in name.split(",") if x.strip()])
        date = parse_date(
            e.get("published_parsed")
            or e.get("updated_parsed")
            or e.get("prism_publicationdate")
            or e.get("published")
        )
        item = build_item(
            title=e.get("title", ""),
            abstract=summary,
            doi=doi,
            pmid="",
            journal=e.get("prism_publicationname") or e.get("dc_source") or feed.get("name", ""),
            date=date,
            url=link,
            authors=authors,
            source_id=feed["id"],
            source_name=feed.get("name") or feed["id"],
            source_group=feed.get("group", "预印本"),
        )
        if item:
            items.append(item)
    return items[: (feed.get("max_items") or 40)]


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
FETCHERS = {
    "openalex": fetch_openalex,
    "pubmed": fetch_pubmed,
    "rss": fetch_rss,
    "crossref": fetch_crossref,
}


def fetch_all(feeds_cfg: dict, lookback_days: int, only: list[str] | None = None) -> list[dict]:
    defaults = feeds_cfg.get("defaults") or {}
    session = _session()
    out: list[dict] = []
    ok = failed = skipped = 0

    for feed in feeds_cfg.get("feeds") or []:
        if only and feed["id"] not in only:
            skipped += 1
            continue
        if not feed.get("enabled", defaults.get("enabled", True)):
            skipped += 1
            continue
        ftype = feed.get("type", "rss")
        fn = FETCHERS.get(ftype)
        if not fn:
            warn(f"未知 feed 类型 {ftype}（{feed['id']}），跳过")
            continue
        lookback = int(feed.get("lookback_days") or defaults.get("lookback_days") or lookback_days)
        started = time.time()
        try:
            items = fn(feed, session, lookback)
        except Exception as exc:  # 单源失败不能拖垮整轮
            warn(f"[{feed['id']}] 抓取异常 {exc.__class__.__name__}: {exc}")
            failed += 1
            continue
        if items:
            ok += 1
        else:
            failed += 1
        out.extend(items)
        log(f"  {feed.get('name', feed['id'])[:38]:<40s} {len(items):>3d} 篇  ({time.time() - started:.1f}s)")
        time.sleep(0.3)  # 对 NCBI / OpenAlex 保持礼貌频率

    log(f"抓取完成：{ok} 个源有结果，{failed} 个源无结果/失败，跳过 {skipped} 个")
    return out
