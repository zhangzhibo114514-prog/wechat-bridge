"""运行状态：审计留档、限速计数、熔断标志、会话上下文。

设计约束：
- **每个动作都要留下可追责的记录**（audit.jsonl），包括被拒绝的消息和原因；
- 熔断 / 限速状态要能落盘，重启后不会“重置计数接着猛发”；
- 上下文按会话分开，群聊与私聊互不污染。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Deque, Optional

from .adapters.base import Message


class SingleInstanceLock:
    """同一时间只允许一个桥接进程。

    为什么必须有：两个桥接同时挂着同一个微信号，会对同一条消息各回一次，
    风控闸门和限速都在各自进程里算，**互相看不见对方的计数**——深夜刷屏与重复回复都从这儿来。

    实现说明（踩过坑）：
    - 先用过 `msvcrt.locking`，实测**跨进程拦不住**（子进程照样拿到锁），弃用；
    - 现在用「原子创建 + PID 存活判定」：`os.open(..., O_CREAT|O_EXCL)` 只有一个进程能成功；
      锁文件里写 pid，若旧进程已死（进程不存在）则视为**陈旧锁**自动接管——
      这样既不会双开，也不会因为上次崩溃留下一把永远打不开的锁；
    - **同一进程内重复加锁也会被拒**（不做进程内豁免），语义更简单、更好测。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._held = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False                      # 打不开句柄 = 进程不存在（或没权限，按最保守算死）
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)                       # POSIX：探活
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _holder(self) -> Optional[int]:
        """读锁文件里的持有者 pid。

        踩坑：锁文件写成 `pid=12345 started=...`，`split("=",1)[1]` 会拿到
        `"12345 started=..."`，`int()` 直接抛 ValueError 被 except 吞掉 → 恒返回 None
        → `release()` 不肯删文件、`acquire()` 也认不出陈旧锁。必须再切一次空白。
        """
        try:
            head = self.path.read_text(encoding="utf-8-sig").strip().splitlines()[0]
            return int(head.split("=", 1)[1].split()[0])
        except Exception:
            return None

    def acquire(self) -> bool:
        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._holder()
                if holder is not None and not self._pid_alive(holder):
                    print(f"[lock] 发现陈旧锁（pid={holder} 已不存在），自动接管")
                    self.path.unlink(missing_ok=True)
                    continue
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"pid={os.getpid()} started={datetime.now().isoformat(timespec='seconds')}\n")
            self._held = True
            return True
        return False

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self._holder() == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"[lock] 释放失败（可手动删 {self.path.name}）: {exc!r}")
        finally:
            self._held = False


class AuditLog:
    """JSONL 审计。**会自己轮转**，否则一个热闹的群几个月就能把它撑到几十兆。"""

    def __init__(self, path: Path, max_bytes: int = 8 * 1024 * 1024, keep: int = 5) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(4 * 1024, int(max_bytes))      # 太小会一秒转好几个，别让配置把人坑了
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        record = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        blob = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(blob))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    # ---- 轮转 ----

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        # 时间戳带微秒：同一秒里连转两次时，秒级时间戳会撞名，而 Windows 的
        # rename 在目标已存在时**直接失败**（不像 POSIX 会覆盖）⇒ 轮转静默失效。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.path.with_name(f"{self.path.stem}-{stamp}{self.path.suffix}")
        serial = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.stem}-{stamp}-{serial}{self.path.suffix}")
            serial += 1
        try:
            self.path.rename(target)
        except OSError:
            return                                          # 挪不动就继续往原文件写，别丢审计
        self._prune()

    def _prune(self) -> None:
        """只留最近 `keep` 个归档。按文件名排序（时间戳天然有序）。"""
        pattern = f"{self.path.stem}-*{self.path.suffix}"
        olds = sorted(self.path.parent.glob(pattern), key=lambda p: p.name)
        for stale in olds[: max(0, len(olds) - self.keep)]:
            try:
                stale.unlink()
            except OSError:
                pass


class RateLimiter:
    """滑动窗口限速 + 熔断 + 静默时段。所有判断都要能说出理由，便于留档。"""

    def __init__(self, policy) -> None:
        self.policy = policy
        self._global: Deque[float] = deque()
        self._per_contact: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.consecutive_failures = 0
        self.frozen = False
        self.frozen_reason = ""

    # ---------- 查询 ----------

    @staticmethod
    def _prune(bucket: Deque[float], now: float, window: int) -> None:
        while bucket and now - bucket[0] > window:
            bucket.popleft()

    def check(self, chat_id: str, sender_id: str = "") -> Optional[str]:
        """返回 None 表示放行，否则返回拒绝原因（中文，会进审计日志）。

        `sender_id` 只在**群聊**里传：群里的"单联系人"额度要按**发消息的人**算，
        不能按群算。按群算的话，十几个人说话，20 条/小时的额度几分钟就见底，
        表现是"群里正聊得热闹，它突然不吭声了"，而日志写的是「触发单联系人限速」——
        名字对、意思错（真正被限的是整个群）。私聊不传，行为与从前一致。
        """
        p = self.policy
        if self.frozen:
            return f"已熔断: {self.frozen_reason}"
        now = time.time()
        with self._lock:
            self._prune(self._global, now, 3600)
            minute = sum(1 for t in self._global if now - t <= 60)
            if minute >= p.max_replies_per_minute:
                return f"触发全局限速（每分钟上限 {p.max_replies_per_minute}）"
            if len(self._global) >= p.max_replies_per_hour:
                return f"触发全局限速（每小时上限 {p.max_replies_per_hour}）"
            bucket = self._per_contact[self._bucket_key(chat_id, sender_id)]
            self._prune(bucket, now, 3600)
            if len(bucket) >= p.max_replies_per_contact_per_hour:
                who = "这个人在本群" if sender_id else "这个联系人"
                return (f"触发单联系人限速（{who}，每小时上限 "
                        f"{p.max_replies_per_contact_per_hour}）")
        if self._in_quiet_hours(now):
            window = "-".join(str(h) for h in (p.quiet_hours or ())) or "未设"
            return f"静默时段 {window}"
        return None

    @staticmethod
    def _bucket_key(chat_id: str, sender_id: str = "") -> str:
        """单联系人额度记在谁头上：群聊 = 这个群里**这个人**；私聊 = 这个会话。"""
        return f"{chat_id}|{sender_id}" if sender_id else str(chat_id)

    def _in_quiet_hours(self, now: float) -> bool:
        """静默时段判断。

        审计发现两处问题，这里都修了：
        1. 端点闭区间导致结束那一刻仍算静默（误差 1 分钟）—— 改成 [start, end) 半开区间；
        2. `["00:00","00:00"]` 旧实现只静默那一分钟、**其余全时段放行**，而体检提示说它"等于全天静默"
           —— 承诺与实现相反。现在起止相同即视为**全天静默**（fail-closed）。

        第 3 条（多段输出自测时踩到）：**空配置 = 明确不要静默时段**。
        旧写法 `start, end = quiet_hours` 遇到空值直接 `ValueError`，
        可配置文件里写 `"quiet_hours": []` 是很自然的表达（与 `max_reply_age_min: 0` 同一套约定），
        结果"关掉静默时段"变成了"收到第一条消息就崩"。长度不对（写坏了）仍然 fail-closed。
        """
        hours = list(self.policy.quiet_hours or ())
        if not hours:
            return False                               # 空 = 明确不要静默时段
        if len(hours) != 2:
            return True                                # 写坏了 → 判不出来就别发（fail-closed）
        start, end = (str(h).strip() for h in hours)
        if start == end:
            return True                                # 起止相同 = 全天静默
        try:
            cur = datetime.now().strftime("%H:%M")
        except Exception:
            return True                                # 判不出来就别发（fail-closed）
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end               # 跨零点

    # ---------- 记账 ----------

    def record_sent(self, chat_id: str, sender_id: str = "") -> None:
        """记一条已发出的回复。`sender_id` 的口径与 `check` 完全一致（群聊按人分桶）。"""
        now = time.time()
        with self._lock:
            self._global.append(now)
            self._per_contact[self._bucket_key(chat_id, sender_id)].append(now)

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.policy.freeze_after_consecutive_failures:
            self.freeze(f"连续 {self.consecutive_failures} 次发送失败")

    def freeze(self, reason: str) -> None:
        self.frozen = True
        self.frozen_reason = reason

    def unfreeze(self) -> None:
        self.frozen = False
        self.frozen_reason = ""
        self.consecutive_failures = 0

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            self._prune(self._global, now, 3600)
            for bucket in self._per_contact.values():
                self._prune(bucket, now, 3600)
            return {
                "frozen": self.frozen,
                "frozen_reason": self.frozen_reason,
                "consecutive_failures": self.consecutive_failures,
                "sent_last_hour": len(self._global),
                "sent_last_minute": sum(1 for t in self._global if now - t <= 60),
                # 用于落盘续命：审计指出「重启即清零」等于给了无限刷发额度
                "sent_timestamps": list(self._global),
                "per_contact": {k: list(v) for k, v in self._per_contact.items() if v},
            }

    def restore(self, data: dict) -> None:
        """从落盘快照恢复窗口与熔断状态（老快照没有这些字段就跳过）。"""
        if not isinstance(data, dict):
            return
        now = time.time()
        with self._lock:
            stamps = [float(t) for t in data.get("sent_timestamps", []) if isinstance(t, (int, float))]
            self._global = deque(t for t in stamps if now - t <= 3600)
            self._per_contact = defaultdict(deque)
            for chat_id, values in (data.get("per_contact") or {}).items():
                kept = deque(float(t) for t in values if isinstance(t, (int, float)) and now - t <= 3600)
                if kept:
                    self._per_contact[str(chat_id)] = kept
            self.consecutive_failures = int(data.get("consecutive_failures") or 0)
            if data.get("frozen"):
                self.frozen = True
                self.frozen_reason = str(data.get("frozen_reason") or "上次运行触发熔断")

    def unfreeze(self) -> None:
        """人工解冻（配合 --resume）。"""
        self.frozen = False
        self.frozen_reason = ""
        self.consecutive_failures = 0


class ContextStore:
    def __init__(self, max_turns: int = 8) -> None:
        self.max_turns = max_turns
        self._store: dict[str, list[dict[str, str]]] = defaultdict(list)

    def append(self, chat_id: str, role: str, content: str) -> None:
        turns = self._store[chat_id]
        turns.append({"role": role, "content": content})
        if len(turns) > self.max_turns * 2:
            del turns[: len(turns) - self.max_turns * 2]

    def get(self, chat_id: str) -> list[dict[str, str]]:
        return list(self._store.get(chat_id, []))


class StateStore:
    """进程级运行统计，同时落盘一份，便于人工查看。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self.day = date.today().isoformat()
        self.received = 0
        self.replied = 0
        self.drafted = 0
        self.ignored = 0
        self.errors = 0
        self.kill_switch = False          # 运行中可通过 state 文件里的 kill 标志停摆
        # 必须在第一次 _save() 之前读回旧的 limiter：_save 是**整文件覆盖写**，
        # 若此时不自带旧值，构造 StateStore 这一下就会把上次的限速窗口/熔断状态抹掉
        # （审计里"限速不落盘"的翻版，修的时候踩过一次）。
        self._extra_limiter: dict = self._read_existing_limiter()
        self._save()

    def _read_existing_limiter(self) -> dict:
        try:
            from .config import read_json_file

            return read_json_file(self.path).get("limiter") or {}
        except Exception:
            return {}

    def snapshot(self) -> dict:
        return {
            "date": self.day,
            "started": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "received": self.received,
            "replied": self.replied,
            "drafted": self.drafted,
            "ignored": self.ignored,
            "errors": self.errors,
            "kill_switch": self.kill_switch,
        }

    def _save(self, limiter: dict | None = None) -> None:
        payload = self.snapshot()
        if limiter is not None:
            payload["limiter"] = limiter
        elif self._extra_limiter:
            payload["limiter"] = self._extra_limiter
        payload["_help"] = "把 kill_switch 改成 true 并保存，桥接会在下一轮检查时停止发送（全局急停开关）。"
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_limiter(self, limiter_snapshot: dict) -> None:
        """限速窗口与熔断状态的落盘入口（跨重启续命）。"""
        self._extra_limiter = limiter_snapshot
        self._save(limiter_snapshot)

    def load_limiter(self) -> dict:
        """读回上次落盘的限速/熔断状态；文件坏了或没有就返回空 dict。"""
        try:
            from .config import read_json_file

            data = read_json_file(self.path)
            return data.get("limiter") or {}
        except Exception:
            return {}


def message_to_dict(msg: Message) -> dict:
    return {
        "chat_id": msg.chat_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "text": msg.text,
        "is_group": msg.is_group,
        "msg_id": msg.msg_id,
        "mentions_me": msg.mentions_me,
        # 引用的是谁：排查"它为什么没回这条引用"时，光看正文里的 `[引用 …]`
        # 分不清是缺解析还是判定层判了"别人之间的对话"。
        "quoted_name": str(((getattr(msg, "quote", None) or {}).get("quoted_name")) or ""),
    }
