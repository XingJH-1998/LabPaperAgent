"""公共工具：路径、配置加载、文本清洗、日期解析、ID 归一化。"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DOCS_DATA = DOCS_DIR / "data"
ARCHIVE_DIR = DOCS_DATA / "archive"

# 统一 UA，符合 NCBI / Crossref 的 polite pool 要求
CONTACT = os.environ.get("CONTACT_EMAIL", "tangerine0w0@163.com")
USER_AGENT = f"LabPaperAgent/1.0 (mailto:{CONTACT})"


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def warn(msg: str) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] WARN {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_yaml(path: Path) -> dict:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> tuple[dict, dict]:
    lab = load_yaml(CONFIG_DIR / "lab.yml")
    feeds = load_yaml(CONFIG_DIR / "feeds.yml")
    return lab, feeds


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_JATS_RE = re.compile(r"</?(jats|mml|sub|sup|italic|bold|title|p|sec|fig|table|xref)[^>]*>")


def strip_html(text: str | None) -> str:
    """去掉 HTML / JATS 标签并解码实体。"""
    if not text:
        return ""
    t = str(text)
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = _TAG_RE.sub(" ", t)
    t = html.unescape(t)
    t = t.replace("\u00a0", " ")
    return norm_space(t)


def norm_space(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def clean_abstract(text: str | None) -> str:
    """摘要清洗：去标签、去 "Abstract" 前缀、去结尾的 DOI/版权尾巴。"""
    t = strip_html(text)
    if not t:
        return ""
    t = re.sub(r"^abstract[:\s\-—]*", "", t, flags=re.I)
    t = re.sub(r"^(summary|graphical abstract)[:\s]*", "", t, flags=re.I)
    # Nature 系 RSS 的 summary 常以 "Journal, Published online: ... doi:..." 开头
    t = re.sub(r"^[A-Z][A-Za-z&\s\-\.]{3,60},\s*Published online:[^\.]*\.\s*", "", t)
    t = re.sub(r"\s*doi:\s*10\.\d{4,9}/\S+\s*$", "", t, flags=re.I)
    t = re.sub(r"^\s*doi:\s*10\.\d{4,9}/\S+\s*", "", t, flags=re.I)
    t = norm_space(t)
    # 有的出版社（如 Frontiers）把结构式摘要的小标题和正文粘在一起：
    # "IntroductionHabitat degradation ... assessment.MethodsUsing ..."
    # 这里把常见小标题前后补回空格，纯为可读性。
    t = re.sub(
        r"(?:(?<=[a-z\.\)])|^)(Introduction|Background|Methods|Materials and Methods|"
        r"Results|Discussion|Conclusions?|Aims?|Objectives?|Significance|Highlights)(?=[A-Z])",
        r" \1 ",
        t,
    )
    t = norm_space(t)
    # Wiley 的 TOC description 只是「刊名, Volume 29, Issue 11, November 2026.」
    # 或「刊名, EarlyView.」，不是摘要。留着会污染摘要字段。
    if len(t) < 160 and re.match(r"^[^,]{2,80},\s*(Volume|Issue|EarlyView|Early View)\b", t, re.I):
        return ""
    return t


def clean_title(text: str | None) -> str:
    t = strip_html(text)
    t = re.sub(r"^title[:\s]*", "", t, flags=re.I)
    return norm_space(t).rstrip(".")


def truncate(text: str, n: int) -> str:
    text = norm_space(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"^(\d{4})/(\d{2})/(\d{2})"), "ymd"),
    (re.compile(r"^(\d{4})\s+(\d{4})"), "year_range"),
    (re.compile(r"^(\d{4})$"), "year_only"),
]


def parse_date(value: Any) -> dt.date | None:
    """尽力解析各种来源的日期，解析不了返回 None（而不是今天）。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (tuple, list)) and value:
        # feedparser 的 *_parsed / Crossref 的 date-parts
        if len(value) >= 3 and all(isinstance(v, int) for v in value[:3]):
            try:
                return dt.date(value[0], value[1], value[2])
            except ValueError:
                return None
        if len(value) >= 2 and all(isinstance(v, int) for v in value[:2]):
            try:
                return dt.date(value[0], value[1], 1)
            except ValueError:
                return None
        return None
    if isinstance(value, dict) and "date-parts" in value:
        parts = value["date-parts"]
        return parse_date(parts[0]) if parts else None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(value).date()
        except (ValueError, OSError):
            return None

    s = str(value).strip()
    if not s:
        return None
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.match(s)
        if not m:
            continue
        if kind == "ymd":
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        if kind == "year_range":
            return _safe_date(int(m.group(1)), 1, 1)
        if kind == "year_only":
            return _safe_date(int(m.group(1)), 1, 1)

    # "2024 Nov 3" / "Nov 2024" / "2024-11-03T00:00:00Z"
    m = re.match(r"^(\d{4})[-\s]", s)
    if m:
        try:
            iso = s.replace("Z", "+00:00")
            d = dt.datetime.fromisoformat(iso)
            return d.date()
        except ValueError:
            pass
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", s)
    if m and m.group(1).lower() in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b", s)
    if m and m.group(2).lower() in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b", s)
    if m and m.group(1).lower() in _MONTHS:
        return _safe_date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1)
    return None


def _safe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, max(1, min(12, m)), max(1, min(28, d)))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 标识符
# --------------------------------------------------------------------------
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>()\[\]]+", re.I)


def normalize_doi(value: Any) -> str:
    """把 doi / https://doi.org/10.x / doi:10.x 统一成裸 DOI（小写、去尾部标点）。"""
    if not value:
        return ""
    s = str(value).strip()
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    m = _DOI_RE.search(s)
    if not m:
        return ""
    doi = m.group(0).lower()
    return doi.rstrip(".,;:)]}\"'")


def normalize_title_key(title: str) -> str:
    """标题指纹：小写、只留字母数字，用于跨源去重（含预印本/正式版同标题）。"""
    t = ascii_fold(title or "").lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def make_id(doi: str, pmid: str, title: str) -> str:
    if doi:
        return "doi:" + doi
    if pmid:
        return "pmid:" + pmid
    return "t:" + hashlib.sha1(normalize_title_key(title).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# 其它
# --------------------------------------------------------------------------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
        f.write("\n")
    tmp.replace(path)


def chunked(seq: Iterable, size: int) -> Iterable[list]:
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
