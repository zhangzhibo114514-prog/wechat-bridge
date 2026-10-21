"""当天情况一览：一条命令看清"它今天干了什么"。

给谁用：白天挂着 draft 或 auto 的人，想快速知道有没有出事、有没有需要人工处理的东西。
不需要懂日志格式——它把审计留档、草稿、限速状态、心跳汇总成人话。

用法：
    python digest.py                 # 今天
    python digest.py --date 2026-10-15
    python digest.py --hours 6       # 最近 6 小时
    python digest.py --audit <路径>   # 看别处的留档（自测用，别跟真实留档混在一起）
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"

# 留档路径，默认 state/audit.jsonl；由 --audit 覆盖（测试要读演示留档时用）
AUDIT_PATH: Path = STATE / "audit.jsonl"

# 哪些事件算"需要人看一眼"
NEEDS_ATTENTION = {
    "sensitive_block": "抓到敏感内容（涉及钱/凭证），已拦下未回复",
    "limiter_frozen": "触发熔断，暂停了发送",
    "kill_switch": "急停开关被打开",
    "send_failed": "发送失败",
    "send_error": "发送异常",
    "llm_error": "模型调用失败",
    "hook_connect_failed": "连接微信失败（会自动重试）",
    "hook_reconnect": "健康检查发现异常，已重连",
    "recv_error": "收消息异常",
    "pipeline_error": "处理链路异常",
    "config_error": "配置有问题（例如非法正则）",
}

LABELS = {
    "sent": "已回复",
    "draft": "写草稿（未发送）",
    "dry_run": "空跑（未发送）",
    "ignored": "忽略",
}


def load_audit(since: datetime, until: datetime) -> list[dict]:
    path = AUDIT_PATH
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            ts = datetime.fromisoformat(item.get("ts", ""))
        except Exception:
            continue
        if since <= ts <= until:
            out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="当天情况一览")
    ap.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--hours", type=int, default=None, help="改为看最近 N 小时")
    ap.add_argument("--audit", default=None, help="留档文件路径，默认 state/audit.jsonl")
    args = ap.parse_args()

    if args.audit:
        global AUDIT_PATH
        AUDIT_PATH = Path(args.audit)

    if args.hours:
        until = datetime.now()
        since = until - timedelta(hours=args.hours)
        title = f"最近 {args.hours} 小时"
    else:
        day = datetime.fromisoformat(args.date).date() if args.date else datetime.now().date()
        since = datetime.combine(day, datetime.min.time())
        until = since + timedelta(days=1)
        title = f"{day}"

    events = load_audit(since, until)
    print(f"===== 微信代理日报（{title}）=====")
    if not events:
        print("这段时间没有任何记录。")
        print("可能原因：代理没在跑（看 state/heartbeat.log 的最后一行时间）、")
        print("          或这台机器当时关着、或没有新消息。")
        return 1

    kinds = Counter(e.get("event", "?") for e in events)
    print(f"\n共 {len(events)} 条记录：")
    for event, count in kinds.most_common():
        label = LABELS.get(event, event)
        print(f"  {label:22} {count}")

    attention = [(e, NEEDS_ATTENTION[e["event"]]) for e in events if e.get("event") in NEEDS_ATTENTION]
    print(f"\n需要看一眼的：{len(attention)} 条")
    for event, why in attention[:15]:
        who = (event.get("message") or {}).get("sender_name") or event.get("reply", "")[:24]
        print(f"  [{event['ts'][11:16]}] {why} —— {event.get('reason') or who}")
    if len(attention) > 15:
        print(f"  …（还有 {len(attention) - 15} 条，看 state/audit.jsonl）")

    replied = [e for e in events if e.get("event") == "sent"]
    if replied:
        print(f"\n已发出的回复（{len(replied)} 条，最近几条）：")
        for e in replied[-5:]:
            who = (e.get("message") or {}).get("sender_name", "?")
            print(f"  [{e['ts'][11:16]}] → {who}：{(e.get('reply') or '')[:50]}")

    limiter_file = STATE / "state.json"
    if limiter_file.exists():
        try:
            data = json.loads(limiter_file.read_text(encoding="utf-8-sig"))
            lim = data.get("limiter") or {}
            print(f"\n限速/熔断：最近一小时发 {lim.get('sent_last_hour', '?')} 条，"
                  f"熔断={'是' if lim.get('frozen') else '否'}"
                  f"{'（' + str(lim.get('frozen_reason')) + '）' if lim.get('frozen') else ''}")
        except Exception:
            pass

    hb = STATE / "heartbeat.log"
    if hb.exists():
        last = hb.read_text(encoding="utf-8").strip().splitlines()[-1]
        stamp = last.split(" | ")[0]
        try:
            age = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 60
            state = "运行中" if age < 3 else f"最后心跳在 {age:.0f} 分钟前（可能已停）"
        except Exception:
            state = last[:40]
        print(f"心跳：{state}")

    drafts = sorted((STATE / "drafts").glob("*.jsonl")) if (STATE / "drafts").exists() else []
    total = sum(len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]) for p in drafts)
    print(f"草稿：{total} 条（在 state/drafts/）—— 要看质量跑：python analyze_drafts.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
