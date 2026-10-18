"""请求监控 —— 网关请求的内存环形日志（管理端「请求监控」页数据源）。

设计要点：
- 内存环形 500 条 + 追加式落盘（monitoring.jsonl，默认开启）：重启不清零，
  跨重启可看历史。写盘为单行 append（无 fsync），崩溃最多丢最后一行 ——
  观测数据不需要 fsync 级持久化保证。
- 落盘文件超过 MONITORING_MAX_BYTES 时按半数截断（保留最新一半），防止无限增长。
- 粒度：一次客户端请求一条；多账号重试不分裂，账号字段记录最终归宿
- ok 三态：None=在途/未知，True=成功，False=失败（含客户端断开 status=499）
- 纯观测：不触碰账号状态与调度逻辑
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from . import settings

KEEP = 500

_lock = threading.Lock()
_entries: deque = deque(maxlen=KEEP)
_inflight: dict[str, dict] = {}

# ── 落盘（monitoring.jsonl，追加式）────────────────────────────────────────────
def _persist_path() -> Path:
    return Path(settings.DATA_DIR) / "monitoring.jsonl"


def _persist_enabled() -> bool:
    raw = os.environ.get("ZCODE_MONITORING_PERSIST")
    if raw is None:
        return True          # 默认开启
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _load_history() -> None:
    """启动时把上次落盘的尾部读回内存（最多 KEEP 条），重启不清零。"""
    path = _persist_path()
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    loaded = 0
    for line in lines[-KEEP:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or "req_id" not in obj:
            continue
        _entries.append(obj)
        loaded += 1
    if loaded:
        print(f"[reqlog] restored {loaded} monitoring entries from {path}")


def _append_disk(entry: dict) -> None:
    """单行追加落盘（尽力而为；失败静默，观测数据不该干扰网关）。超限截断。"""
    path = _persist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        too_big = path.exists() and path.stat().st_size > settings.MONITORING_MAX_BYTES
        mode = "r+" if too_big else "a"
        if too_big:
            # 截断：保留最新一半行
            keep = path.read_text(encoding="utf-8", errors="replace").splitlines()
            keep = keep[max(0, len(keep) // 2):]
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            os.replace(tmp, path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        else:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


_lock = threading.Lock()
_entries: deque = deque(maxlen=KEEP)
_inflight: dict[str, dict] = {}


def begin(req_id: str, endpoint: str, model: str, stream: bool, preview: str = "") -> None:
    """请求进入网关（鉴权通过、body 解析成功后）。"""
    entry = {
        "req_id": req_id,
        "ts": time.time(),
        "endpoint": endpoint,
        "model": model,
        "stream": bool(stream),
        "preview": (preview or "")[:80],
        "account": "",
        "mode": "",
        "ok": None,
        "status": None,
        "error": "",
        "t_first": None,
        "t_total": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
    }
    with _lock:
        _entries.append(entry)
        _inflight[req_id] = entry


def mark_account(req_id: str, account_name: str, mode: str) -> None:
    """记录实际服务该请求的账号（多账号重试时最后一次生效）。"""
    with _lock:
        entry = _inflight.get(req_id)
        if entry is not None:
            entry["account"] = account_name
            entry["mode"] = mode


def finish_ok(req_id: str, t_first: float | None = None,
              input_tokens: int | None = None, output_tokens: int | None = None,
              status: int | None = None, cached_tokens: int | None = None) -> None:
    with _lock:
        entry = _inflight.pop(req_id, None)
        if entry is None:
            return
        entry["ok"] = True
        entry["status"] = status or 200
        entry["t_first"] = t_first
        entry["t_total"] = time.time() - entry["ts"]
        entry["input_tokens"] = input_tokens
        entry["output_tokens"] = output_tokens
        entry["cached_tokens"] = cached_tokens
        done = dict(entry)
    if _persist_enabled():
        _append_disk(done)


def finish_error(req_id: str, error: str, status: int | None = None,
                 t_first: float | None = None) -> None:
    with _lock:
        entry = _inflight.pop(req_id, None)
        if entry is None:
            return
        entry["ok"] = False
        entry["status"] = status
        entry["error"] = (error or "")[:200]
        entry["t_first"] = t_first
        entry["t_total"] = time.time() - entry["ts"]
        done = dict(entry)
    if _persist_enabled():
        _append_disk(done)


def snapshot() -> list[dict]:
    """全部条目，最新在前（含在途）。"""
    with _lock:
        return [dict(e) for e in reversed(_entries)]


def clear() -> None:
    with _lock:
        _entries.clear()
        _inflight.clear()
    if _persist_enabled():
        try:
            p = _persist_path()
            if p.exists():
                p.unlink()
        except OSError:
            pass


# 启动时读回上次的历史（放在模块尾部，DATA_DIR 已就绪）
if _persist_enabled():
    _load_history()
