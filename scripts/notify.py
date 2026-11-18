"""可选：把当天入选文献推送到 IM / 邮件网关。

通过环境变量开启，未配置时静默跳过：
    NOTIFY_WEBHOOK   https://...              目标地址
    NOTIFY_TYPE      generic | slack | feishu | dingtalk | serverchan | bark
    NOTIFY_TOP_N     推送条数，默认 10

说明：网页本身已经是主要出口，这里是「顺带推一条消息到群里」的可选项。
"""

from __future__ import annotations

import os
from typing import Any

import requests

from common import USER_AGENT, log, truncate, warn


def _summary(items: list[dict], lab_name: str, top_n: int) -> str:
    lines = [f"**{lab_name} · 今日文献速览**（{len(items)} 篇入选）"]
    for it in items[:top_n]:
        score = int(float(it.get("score") or 0))
        title = it.get("title", "")
        journal = it.get("journal") or ""
        url = it.get("url") or (f"https://doi.org/{it['doi']}" if it.get("doi") else "")
        reason = truncate(it.get("reason") or "", 60)
        lines.append(f"- **[{score}]** {title}\n  {journal} · {reason}\n  {url}")
    if len(items) > top_n:
        lines.append(f"- …另 {len(items) - top_n} 篇，见网页")
    return "\n".join(lines)


def _plain(items: list[dict], lab_name: str, top_n: int) -> str:
    lines = [f"{lab_name} · 今日文献速览（{len(items)} 篇入选）"]
    for it in items[:top_n]:
        url = it.get("url") or (f"https://doi.org/{it['doi']}" if it.get("doi") else "")
        lines.append(f"[{int(float(it.get('score') or 0))}] {it.get('title', '')}\n{url}")
    return "\n\n".join(lines)


def notify_digest(items: list[dict], lab_cfg: dict) -> None:
    webhook = os.environ.get("NOTIFY_WEBHOOK", "").strip()
    if not webhook:
        return
    kind = (os.environ.get("NOTIFY_TYPE") or "generic").strip().lower()
    top_n = int(os.environ.get("NOTIFY_TOP_N") or 10)
    lab_name = (lab_cfg.get("lab") or {}).get("name") or "Lab"
    md = _summary(items, lab_name, top_n)
    plain = _plain(items, lab_name, top_n)

    payload: dict[str, Any]
    if kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": md}}
    elif kind == "dingtalk":
        payload = {"msgtype": "markdown", "markdown": {"title": "文献速览", "text": md}}
    elif kind == "slack":
        payload = {"text": md}
    elif kind == "bark":
        payload = {"title": f"{lab_name} 文献速览", "body": plain, "group": "lab-papers"}
    elif kind == "serverchan":
        r = requests.post(
            webhook,
            data={"title": f"{lab_name} 今日文献 {len(items)} 篇", "desp": md},
            timeout=30,
        )
        r.raise_for_status()
        log(f"通知已发送（serverchan，{len(items)} 篇）")
        return
    else:
        payload = {"text": md, "plain": plain}

    try:
        r = requests.post(webhook, json=payload, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        log(f"通知已发送（{kind}，{min(len(items), top_n)} 条）")
    except requests.RequestException as exc:
        warn(f"通知发送失败：{exc}")
