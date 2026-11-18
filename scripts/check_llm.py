#!/usr/bin/env python3
"""LLM 连通性检查：发一个极小的请求，验证 key / 地址 / 模型是否可用。

    python scripts/check_llm.py

依次报告：地址、请求的模型、实际返回的模型、耗时、token 用量。
失败时给出可操作的原因（key 无效 / 地址不对 / 模型名不存在 / 余额不足）。

只花 1 次极小请求（约 5 个 token），可以随便跑。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from common import USER_AGENT, load_config  # noqa: E402


def resolve() -> tuple[str, str, str]:
    """key 只从环境变量取；base_url / model 允许被环境变量覆盖，否则用 lab.yml。"""
    key = ""
    for env in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        key = os.environ.get(env, "").strip()
        if key:
            break
    lab, _ = load_config()
    llm = lab.get("llm") or {}
    base = (os.environ.get("LLM_BASE_URL", "").strip() or llm.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "").strip() or llm.get("model") or "deepseek-chat"
    return key, base, model


def main() -> int:
    key, base, model = resolve()
    print(f"地址     : {base}")
    print(f"请求模型 : {model}")

    if not key:
        print("\n❌ 没有找到 API key。")
        print("   本地：export LLM_API_KEY=sk-...")
        print("   Actions：Settings → Secrets and variables → Actions → 名字必须是 LLM_API_KEY")
        return 1
    print(f"Key      : 已注入（长度 {len(key)}，前缀 {key[:3]}…）\n")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：可用"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    t0 = time.time()
    try:
        r = requests.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT},
            timeout=60,
        )
    except requests.RequestException as exc:
        print(f"❌ 请求失败：{exc.__class__.__name__}: {exc}")
        print("   检查网络，或地址是否写成了 https://api.xxx.com（少了 /v1 之类）")
        return 1

    dt = time.time() - t0
    print(f"HTTP {r.status_code}  耗时 {dt:.1f}s")

    if r.status_code == 200:
        try:
            d = r.json()
            content = d["choices"][0]["message"]["content"].strip()
            used = d.get("usage") or {}
        except (ValueError, KeyError, IndexError):
            print(f"❌ 返回不是预期的 OpenAI 格式：{r.text[:200]}")
            return 1
        print(f"✅ 连通正常")
        print(f"   返回模型 : {d.get('model', '(未返回)')}")
        print(f"   回复内容 : {content[:40]}")
        print(f"   token    : 输入 {used.get('prompt_tokens', '?')} / 输出 {used.get('completion_tokens', '?')}")
        if d.get("model") and d["model"] != model:
            print(f"   ⚠️ 实际模型与请求的不一致，可能被网关重定向")
        return 0

    if r.status_code in (401, 403):
        print("❌ 鉴权失败：key 无效、已过期，或与这个 base_url 不匹配")
        print("   DeepSeek 的 key 不能配 OpenAI 的地址，反之亦然")
        print(f"   响应：{r.text[:200]}")
        return 1
    if r.status_code == 404:
        print(f"❌ 地址或模型名不存在。当前 base_url={base}，model={model}")
        print("   OpenAI 兼容地址一般以 /v1 结尾；模型名要写该平台的确切 ID")
        print(f"   响应：{r.text[:200]}")
        return 1
    if r.status_code == 402:
        print("❌ 余额不足，请到平台充值")
        return 1
    if r.status_code == 429:
        print("❌ 触发限流，稍后再试")
        return 1

    print(f"❌ 未预期状态码 {r.status_code}：{r.text[:300]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
