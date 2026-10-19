"""用量账本 —— 按「日 × 账号 × 模型」聚合的持久化统计。

为什么要它：`reqlog` 只是最近 500 条的环形缓冲（重启可恢复，但会被截断），
回答不了「这个月烧了多少」「哪个号烧得最快」「缓存命中率在涨还是在跌」。
这里做一份极小的日聚合表，长期保留、体积可忽略。

计费口径（2026-10-19 受控实验反推，6/6 一致）：

    used_delta = input_tokens + output_tokens      ← 命中缓存也按 100% 计入

实验方法：单账号、固定 prompt，先冷后热各发一次，每次前后各刷一次 /quota，
用 used 的增量对比 monitoring 里记录的 in/cached/out。

    trial  case     in   cached   out  actual  rate=(actual-out)/in
    1      COLD  2,995        0    16   3,011   1.00
    1      WARM  2,995    2,944    16   3,011   1.00
    2      COLD  2,997        0    16   3,013   1.00
    2      WARM  2,997    2,944    16   3,013   1.00
    3      COLD  2,999        0    16   3,015   1.00
    3      WARM  2,999    2,944    16   3,015   1.00

结论：**缓存不减免额度**。cache_read 只省延迟（和真金白银的账单），
不省免费试用额度。每轮成本 = 完整上下文 + 输出。

早前误判为 5 折的原因：拿 monitoring.jsonl 里残留的 9 条请求去对上游 used 总量，
而 used 覆盖了当日**所有**请求（含已被环形缓冲淘汰的），分母不一致 → 巧合落在 50%。

存储：`data/usage_daily.json`
    { "2026-10-19": { "<account_id>": { "<model>": {in,cached,out,billed,reqs} } } }
写入用 tmp + os.replace 原子替换；保留最近 RETENTION_DAYS 天。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import settings

# 上游对命中缓存的输入同样全额计入额度（实测 6/6，见模块 docstring）
CACHE_BILL_RATE = 1.0

RETENTION_DAYS = 60

_lock = threading.Lock()
_cache: dict | None = None


def _path() -> Path:
    return Path(settings.DATA_DIR) / "usage_daily.json"


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        _cache = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        _cache = {}
    return _cache


def _save(data: dict) -> None:
    """原子落盘 + 按天数裁剪。失败静默（统计不该影响网关）。"""
    cutoff = time.time() - RETENTION_DAYS * 86400
    keep = {}
    for day, accounts in data.items():
        try:
            day_ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            continue
        if day_ts >= cutoff:
            keep[day] = accounts
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def record(account_id: str, account_name: str, model: str,
           input_tokens: int | None, cached_tokens: int | None,
           output_tokens: int | None, ts: float | None = None) -> None:
    """记一次成功请求。input_tokens 应为「完整提示」（未命中 + 命中）。

    额度消耗 = input_tokens + output_tokens（缓存不减免）。
    """
    if input_tokens is None and output_tokens is None:
        return
    inp = int(input_tokens or 0)
    cac = min(int(cached_tokens or 0), inp)
    out = int(output_tokens or 0)
    billed = int(round((inp - cac) + cac * CACHE_BILL_RATE + out))

    key = account_id or account_name or "unknown"
    day = _day(ts)
    with _lock:
        data = _load()
        accts = data.setdefault(day, {})
        models = accts.setdefault(key, {})
        cell = models.setdefault(model or "-", {"in": 0, "cached": 0, "out": 0,
                                                "billed": 0, "reqs": 0, "name": account_name})
        cell["in"] += inp
        cell["cached"] += cac
        cell["out"] += out
        cell["billed"] += billed
        cell["reqs"] += 1
        cell["name"] = account_name or cell.get("name") or ""
        _save(data)


def snapshot(days: int = 30) -> dict:
    """按日/按账号汇总，附带每日合计与缓存命中率。"""
    with _lock:
        data = _load()
    today = time.time()
    cutoff_day = _day(today - max(1, days) * 86400)
    keep_days = sorted(d for d in data if d >= cutoff_day)

    daily: dict[str, dict] = {}
    per_account: dict[str, dict] = {}
    per_model: dict[str, dict] = {}

    for day in keep_days:
        dsum = {"in": 0, "cached": 0, "out": 0, "billed": 0, "reqs": 0}
        for acct_id, models in (data.get(day) or {}).items():
            asum = per_account.setdefault(acct_id, {"name": "", "in": 0, "cached": 0,
                                                    "out": 0, "billed": 0, "reqs": 0,
                                                    "days": set()})
            for model, cell in (models or {}).items():
                msum = per_model.setdefault(model, {"in": 0, "cached": 0, "out": 0,
                                                    "billed": 0, "reqs": 0})
                # billed 一律按 in + out 重算，避免历史行沿用旧口径造成总数不自洽
                cell_billed = int(cell.get("in") or 0) + int(cell.get("out") or 0)
                for target in (dsum, asum, msum):
                    for k in ("in", "cached", "out", "reqs"):
                        target[k] += int(cell.get(k) or 0)
                    target["billed"] += cell_billed
                asum["name"] = cell.get("name") or asum["name"]
                asum["days"].add(day)
        daily[day] = dsum

    for a in per_account.values():
        a["days"] = len(a.pop("days"))
    for bucket in (daily, per_account, per_model):
        for v in bucket.values():
            v["hit_rate"] = round(v["cached"] / v["in"] * 100, 2) if v["in"] else 0.0

    total = {"in": 0, "cached": 0, "out": 0, "billed": 0, "reqs": 0}
    for v in daily.values():
        for k in total:
            total[k] += v[k]
    total["hit_rate"] = round(total["cached"] / total["in"] * 100, 2) if total["in"] else 0.0

    return {
        "days": keep_days,
        "daily": daily,
        "accounts": per_account,
        "models": per_model,
        "total": total,
        "cache_bill_rate": CACHE_BILL_RATE,
        "retention_days": RETENTION_DAYS,
        "generated_at": time.time(),
    }
