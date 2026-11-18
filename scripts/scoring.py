"""相关度打分：关键词兜底打分 + LLM 精排。"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

from common import USER_AGENT, chunked, log, truncate, warn

# 打分权重（关键词兜底）
W_MUST_TITLE = 9.0
W_MUST_ABS = 3.5
W_SOFT_TITLE = 4.0
W_SOFT_ABS = 1.6
W_BOOST_TITLE = 5.0
W_BOOST_ABS = 1.8
W_EXCLUDE = -45.0
W_INTEREST = 5.0

# 概化词：单独命中不足以说明相关，用于抑制词表误命中
GENERIC_TERMS = {
    "climate change",
    "biodiversity",
    "conservation",
    "uncertainty",
    "ensemble",
    "remote sensing",
    "spatial scale",
    "occurrence records",
    "species richness",
    "model evaluation",
}


def _term_regex(term: str) -> re.Pattern:
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term.strip()) if p]
    body = r"[\s\-]+".join(parts)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


class KeywordScorer:
    """无需 API 的兜底打分器；同时用于挑选最值得送去 LLM 的文献。"""

    def __init__(self, lab_cfg: dict) -> None:
        lex = lab_cfg.get("lexicon") or {}
        self.must = [(t, _term_regex(t)) for t in lex.get("must") or []]
        self.soft = [(t, _term_regex(t)) for t in lex.get("soft") or []]
        self.boost = [(t, _term_regex(t)) for t in lex.get("boost") or []]
        self.exclude = [(t, _term_regex(t)) for t in lex.get("exclude") or []]

        must_terms = {t.lower() for t, _ in self.must} | {t.lower() for t, _ in self.soft}
        self.interest: list[tuple[str, str, re.Pattern]] = []
        for group in lab_cfg.get("lab", {}).get("interests") or []:
            label = group.get("label") or group.get("key") or ""
            for t in group.get("terms") or []:
                if t.lower() in must_terms:
                    continue
                self.interest.append((t, label, _term_regex(t)))

    def score(self, item: dict) -> tuple[float, list[str], list[str]]:
        title = item.get("title") or ""
        abstract = item.get("abstract") or ""
        hay_abs = f"{title} {abstract}"  # 标题也计入摘要层，避免标题命中漏加

        raw = 0.0
        matched: list[str] = []
        labels: list[str] = []

        for term, rx in self.must:
            w = 0.0
            if rx.search(title):
                w += W_MUST_TITLE
            if rx.search(abstract):
                w += W_MUST_ABS
            if w:
                if term.lower() in GENERIC_TERMS:
                    w *= 0.6
                raw += w
                matched.append(term)

        for term, rx in self.soft:
            w = 0.0
            if rx.search(title):
                w += W_SOFT_TITLE
            if rx.search(abstract):
                w += W_SOFT_ABS
            if w:
                raw += w
                matched.append(term)

        for term, label, rx in self.interest:
            if rx.search(hay_abs):
                raw += W_INTEREST * 0.8
                matched.append(term)
                if label:
                    labels.append(label)

        for term, rx in self.boost:
            if rx.search(title):
                raw += W_BOOST_TITLE
                matched.append(term)
            elif rx.search(abstract):
                raw += W_BOOST_ABS
                matched.append(term)

        for term, rx in self.exclude:
            if rx.search(hay_abs):
                raw += W_EXCLUDE

        raw = max(0.0, raw)
        score = 100.0 * raw / (raw + 14.0) if raw else 0.0
        # 摘要缺失的文献不该拿高分（信息不足）
        if len(abstract) < 120:
            score = min(score, 55.0)

        seen: list[str] = []
        for m in matched:
            if m not in seen:
                seen.append(m)
        return round(score, 1), seen[:6], list(dict.fromkeys(labels))[:3]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
SYSTEM_TEMPLATE = """你是一位严谨的生态学文献筛选专家，为一个具体实验室做文献筛选。

<实验室研究画像>
{profile}
</实验室研究画像>

<本实验室的结构化方向>
{interests}
</本实验室的结构化方向>

请对每篇文献给出 0-100 的相关度分数（整数），标准如下：
- 90-100：正是我们在做的工作，或能直接用于我们建模/风险分析的新方法、新数据、新框架
- 70-89：高度相关。方法、评估方式或结论能实质影响我们的物种分布建模、变量与背景点处理、模型迁移性判断或入侵风险评估
- 50-69：边缘相关。同一大领域（生态学/生物地理学），但主题离我们的工作较远，例如单纯的群落描述、与分布预测无关的生理实验
- 30-49：弱相关。只共享生态学背景
- 0-29：无关。其他学科、纯分子/细胞/临床研究、政策评论、勘误、会议通知、招聘、新闻简讯

判断要点：
1. 唯一标准是「能否改变或支持我们的分布预测与入侵风险评估工作」，不是「是否属于生态学」。
2. 不要因为出现 species、climate change、biodiversity 这类泛词就给高分；要看它是否真的在预测/解释/评估空间分布或入侵风险。
3. 摘要缺失时按标题保守判断，分数上限 60。
4. 明确识别非研究内容：news/editorial/correction/erratum/book review 一律 <=25，并把 type 标成对应值。
5. reason 用中文，不超过 45 字，必须指出具体内容（做了什么/用了什么方法/结论是什么），不要空话。
6. tags 给 1-3 个短标签，优先使用上面列出的方向名称，也允许补充方法论标签（如 MaxEnt、BIOMOD、迁移性、不确定性）。
7. type 从 research / review / methods / dataset / news / other 中选一个。

只输出 JSON，不要任何解释文字，格式：
{{"results":[{{"i":1,"score":82,"type":"research","tags":["物种分布模型","模型评估"],"reason":"提出新的背景点采样方案，可直接用于我们的 MaxEnt 流程"}}]}}
其中 i 是下面列表里的编号。"""

_USER_TEMPLATE = """共 {n} 篇文献，请逐篇打分：

{item_blocks}"""

_ITEM_TEMPLATE = """[{i}]
标题：{title}
期刊：{journal}（{date}）
摘要：{abstract}
"""


class LLMScorer:
    def __init__(self, llm_cfg: dict, lab_cfg: dict) -> None:
        self.cfg = llm_cfg or {}
        self.batch_size = max(1, int(self.cfg.get("batch_size") or 8))
        self.timeout = int(self.cfg.get("timeout") or 90)
        self.max_retries = max(1, int(self.cfg.get("max_retries") or 3))
        # 环境变量优先于 lab.yml，方便在 Actions 里换供应商而不改代码
        self.base_url = (
            os.environ.get("LLM_BASE_URL", "").strip()
            or self.cfg.get("base_url")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.model = os.environ.get("LLM_MODEL", "").strip() or self.cfg.get("model") or "deepseek-chat"
        self.api_key = self._resolve_key()
        self.auth_failed = False
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        )
        self.system_prompt = SYSTEM_TEMPLATE.format(
            profile=(lab_cfg.get("lab", {}).get("profile") or "").strip(),
            interests="\n".join(
                f"- {g.get('label')}: {', '.join((g.get('terms') or [])[:8])}"
                for g in lab_cfg.get("lab", {}).get("interests") or []
            ),
        )
        self.calls = 0
        self.failures = 0

    @staticmethod
    def _resolve_key() -> str:
        for env in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            v = os.environ.get(env, "").strip()
            if v:
                return v
        return ""

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True) and self.api_key)

    # ------------------------------------------------------------------
    def score_items(self, items: list[dict], log_every: bool = True) -> dict[str, dict]:
        """返回 {item_id: {score, tags, reason, type, llm: True}}"""
        results: dict[str, dict] = {}
        if not items or not self.enabled:
            return results

        batches = list(chunked(items, self.batch_size))
        log(f"LLM 打分：{len(items)} 篇，分 {len(batches)} 批（{self.model}）")
        for n, batch in enumerate(batches, 1):
            if self.auth_failed:
                warn("鉴权失败，剩余批次直接跳过（改用关键词分）")
                break
            got = self._score_batch(batch)
            if got is None and len(batch) > 1:
                # 整批失败时拆半重试一次，避免一条坏数据毁掉整批
                half = len(batch) // 2
                for sub in (batch[:half], batch[half:]):
                    sub_res = self._score_batch(sub)
                    if sub_res:
                        got = {**(got or {}), **sub_res}
            if got:
                results.update(got)
            if log_every and (n % 5 == 0 or n == len(batches)):
                log(f"  LLM 进度 {n}/{len(batches)}，已打分 {len(results)} 篇")
            time.sleep(0.2)
        if self.failures:
            warn(f"LLM 有 {self.failures} 次请求最终失败，对应文献退回关键词打分")
        return results

    # ------------------------------------------------------------------
    def _score_batch(self, batch: list[dict]) -> dict[str, dict] | None:
        blocks = []
        for i, it in enumerate(batch, 1):
            blocks.append(
                _ITEM_TEMPLATE.format(
                    i=i,
                    title=truncate(it.get("title", ""), 300),
                    journal=it.get("journal") or "未知期刊",
                    date=it.get("date") or "日期未知",
                    abstract=truncate(it.get("abstract") or "（无摘要）", 1500),
                )
            )
        payload = {
            "model": self.model,
            "temperature": float(self.cfg.get("temperature") or 0.2),
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(n=len(batch), item_blocks="\n".join(blocks)),
                },
            ],
            "response_format": {"type": "json_object"},
        }

        data = self._post(payload)
        if data is None:
            payload.pop("response_format", None)  # 有些网关不支持 json_object
            data = self._post(payload)
        if data is None:
            self.failures += 1
            return None

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.failures += 1
            warn("LLM 响应结构异常")
            return None

        parsed = _parse_json(content)
        if not parsed:
            self.failures += 1
            warn(f"LLM 返回无法解析为 JSON：{truncate(str(content), 160)}")
            return None

        out: dict[str, dict] = {}
        rows = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            rows = parsed if isinstance(parsed, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx = row.get("i") or row.get("id") or row.get("index")
            try:
                pos = int(idx) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= pos < len(batch):
                continue
            item = batch[pos]
            try:
                score = float(row.get("score"))
            except (TypeError, ValueError):
                continue
            out[item["id"]] = {
                "score": max(0.0, min(100.0, round(score, 1))),
                "tags": [str(t)[:24] for t in (row.get("tags") or []) if str(t).strip()][:3],
                "reason": truncate(str(row.get("reason") or ""), 120),
                "type": str(row.get("type") or "research")[:16],
                "llm": True,
            }
        return out or None

    # ------------------------------------------------------------------
    def _post(self, payload: dict) -> dict | None:
        url = f"{self.base_url}/chat/completions"
        for attempt in range(self.max_retries):
            try:
                r = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                warn(f"LLM 请求异常 {exc.__class__.__name__}（第 {attempt + 1} 次）")
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                self.calls += 1
                try:
                    return r.json()
                except ValueError:
                    return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                warn(f"LLM 鉴权失败（HTTP {r.status_code}），请检查 LLM_API_KEY")
                self.auth_failed = True
                return None
            if r.status_code == 400:
                warn(f"LLM 400：{truncate(r.text, 200)}")
                return None
            warn(f"LLM HTTP {r.status_code}：{truncate(r.text, 160)}")
            return None
        return None


def _parse_json(text: str) -> Any:
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def score_all(items: list[dict], lab_cfg: dict, opts: dict) -> list[dict]:
    """给每篇文献打上 score / reason / tags；返回就地修改后的列表。"""
    kw = KeywordScorer(lab_cfg)
    for it in items:
        s, matched, labels = kw.score(it)
        it["kw_score"] = s
        it["score"] = s
        it["kw_terms"] = matched
        it["tags"] = labels
        it["reason"] = "关键词匹配：" + "、".join(matched[:3]) if matched else ""
        it["llm"] = False

    llm_cfg = lab_cfg.get("llm") or {}
    scorer = LLMScorer(llm_cfg, lab_cfg)
    limit = int((lab_cfg.get("scoring") or {}).get("max_llm_items") or 120)

    if not scorer.enabled:
        why = "LLM 未启用" if not llm_cfg.get("enabled", True) else "未找到 API key"
        warn(f"{why}，本次只用关键词打分（准确度会下降）")
        for it in items:
            it["mode"] = "keyword"
        return sorted(items, key=_rank_key, reverse=True)

    # 关键词分先排序，优先把最可能相关的送去 LLM，控制成本
    ranked = sorted(items, key=lambda x: (x["kw_score"], x.get("date") or ""), reverse=True)
    target, rest = ranked[:limit], ranked[limit:]
    if rest:
        log(f"超出 LLM 预算：{len(rest)} 篇仅用关键词打分")

    llm_results = scorer.score_items(target)
    filled = 0
    for it in target:
        r = llm_results.get(it["id"])
        if not r:
            continue
        it["score"] = r["score"]
        it["reason"] = r["reason"]
        it["type"] = r["type"]
        it["llm"] = True
        filled += 1
        if r["tags"]:
            merged = list(dict.fromkeys(r["tags"] + list(it.get("tags") or [])))
            it["tags"] = merged[:3]

    for it in rest:
        it["mode"] = "keyword"
    for it in target:
        it.setdefault("mode", "llm")

    log(f"打分完成：{filled}/{len(target)} 篇经 LLM 精排，其余用关键词分")

    # 非研究内容不推送
    for it in items:
        if it.get("type") in ("news", "editorial", "correction"):
            it["score"] = min(it["score"], 20.0)

    return sorted(items, key=_rank_key, reverse=True)


def _rank_key(it: dict) -> tuple[float, str]:
    return (float(it.get("score") or 0), it.get("date") or "")
