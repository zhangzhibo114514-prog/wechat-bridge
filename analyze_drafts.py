"""草稿质量分析：把 `state/drafts/*.jsonl` 汇总成一张人工可读的体检表。

为什么需要：draft 模式跑一天会攒下几十上百条草稿，逐条看太慢，而"能不能切 auto"
只能靠人工判断。这个脚本把**需要重点看的那几条**先挑出来，其余给统计。

跑法：
    python analyze_drafts.py                 # 分析全部草稿
    python analyze_drafts.py --days 1        # 只看最近 1 天
    python analyze_drafts.py --out report.md # 同时导出一份 Markdown 报告

输出分四块：
    1. 总览：条数、涉及会话、平均/最长回复长度
    2. **需要重点看**：触发了敏感规则的、异常长的、疑似承诺的、自称 AI 的
    3. 按人分布：谁的消息多、回复多长
    4. 结论建议：够不够格切 auto（只是建议，最终由人决定）
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DRAFT_DIR = ROOT / "state" / "drafts"

# 需要人工重点看的模式：都是"以本人身份说错话"的高风险形态
RISKY_PATTERNS = [
    (r"(好的|行|可以|没问题)[，,。]?\s*(我|明天|后天|周[一二三四五六日])", "疑似答应/承诺"),
    (r"\d{1,2}\s*[:：]\s*\d{2}", "涉及具体时间"),
    (r"(一定|保证|肯定|绝对)", "绝对化措辞"),
    (r"(作为|我是)\s*(一个)?\s*(AI|人工智能|机器人|助手|模型)", "自称 AI/机器人（破坏人格设定）"),
    (r"(多少钱|价格|费用|报价|付款|打款|转账|红包)", "涉及钱"),
    (r"(地址|身份证|密码|验证码|卡号)", "涉及凭证"),
    (r"^(很抱歉|抱歉|对不起)[，,]", "开场道歉（不像本人语气）"),
]


def load_drafts(days: int | None) -> list[dict]:
    if not DRAFT_DIR.exists():
        return []
    cutoff = None
    if days:
        cutoff = datetime.now() - timedelta(days=days)
    records: list[dict] = []
    for path in sorted(DRAFT_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if cutoff:
                try:
                    if datetime.fromisoformat(item.get("ts", "")) < cutoff:
                        continue
                except Exception:
                    pass
            item["_file"] = path.name
            records.append(item)
    return records


def risky_reasons(reply: str) -> list[str]:
    return [label for pattern, label in RISKY_PATTERNS if re.search(pattern, reply or "")]


def _selftest() -> int:
    """用合成草稿端到端验证这个分析器本身：先造一份"有问题的"草稿集，再跑分析，看能不能挑出来。

    为什么值得写：这个工具的作用是**替人把关**，它自己漏报就等于把关失败。
    合成数据包含三种典型坏味道（自称 AI、疑似承诺、涉及凭证），以及正常回复作为对照。
    """
    import shutil

    bad_cases = [
        ("自称 AI 型", "在吗？", "作为一个 AI 助手，我建议你明天再处理这件事"),
        ("疑似承诺型", "周末有空吗", "好的，周六下午三点我过去找你"),
        ("凭证泄漏型", "帮我看看", "把验证码发我一下就能登录了"),
        ("涉及钱型", "这个贵吗", "这个多少钱？我帮你问问价格"),
        ("正常型", "吃饭了吗", "刚吃完，你呢"),
    ]
    # 临时目录必须放在**工作区内**：系统临时目录在沙箱下不可写（实测 PermissionError），
    # 放这里则任何环境都能跑，跑完删掉。
    tmp = ROOT / "state" / "_selftest_drafts"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        global DRAFT_DIR
        original = DRAFT_DIR
        DRAFT_DIR = tmp
        stamp = datetime.now().isoformat(timespec="seconds")
        with (tmp / "selftest.jsonl").open("w", encoding="utf-8") as fh:
            for i, (kind, incoming, reply) in enumerate(bad_cases, 1):
                fh.write(json.dumps({
                    "ts": stamp,
                    "reason": "draft 模式",
                    "message": {"chat_id": f"wxid_t{i}", "sender_id": f"wxid_t{i}",
                                "sender_name": f"测试对象{i}", "text": incoming},
                    "reply": reply,
                }, ensure_ascii=False) + "\n")

        records = load_drafts(None)
        flagged = {r["message"]["text"]: risky_reasons(r.get("reply", "")) for r in records}
        DRAFT_DIR = original

        checks = [
            ("能读到合成草稿", len(records) == len(bad_cases), f"{len(records)} 条"),
            ("认出「自称 AI」", "自称 AI" in " ".join(flagged.get("在吗？", [])), str(flagged.get("在吗？"))),
            ("认出「疑似承诺」", any("承诺" in x for x in flagged.get("周末有空吗", [])), str(flagged.get("周末有空吗"))),
            ("认出「涉及凭证」", any("凭证" in x for x in flagged.get("帮我看看", [])), str(flagged.get("帮我看看"))),
            ("认出「涉及钱」", any("钱" in x for x in flagged.get("这个贵吗", [])), str(flagged.get("这个贵吗"))),
            ("正常回复不误报", not flagged.get("吃饭了吗"), str(flagged.get("吃饭了吗"))),
        ]
        ok = True
        for label, passed, detail in checks:
            print(f"  [{'OK' if passed else '失败'}] {label} —— {detail}")
            ok &= passed
        print(f"\n结论：{'草稿分析器自测通过' if ok else '存在失败项'}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="草稿质量分析")
    ap.add_argument("--days", type=int, default=None, help="只看最近 N 天")
    ap.add_argument("--out", default=None, help="导出 Markdown 报告路径")
    ap.add_argument("--selftest", action="store_true", help="用合成草稿验证分析器本身（不碰真实数据）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    records = load_drafts(args.days)
    if not records:
        print(f"没有草稿可分析（目录：{DRAFT_DIR}）")
        print("提示：draft 模式下每条该回的私聊都会写到这里；先跑一段时间再看。")
        return 1

    replies = [r.get("reply", "") for r in records]
    lengths = [len(x) for x in replies if x]
    senders = Counter(r.get("message", {}).get("sender_name", "?") for r in records)
    reasons = Counter(r.get("reason", "?") for r in records)

    lines: list[str] = []
    lines.append(f"# 草稿质量体检（{datetime.now():%Y-%m-%d %H:%M}）\n")
    lines.append(f"- 草稿总数：**{len(records)}** 条（{len({r['_file'] for r in records})} 个文件）")
    lines.append(f"- 涉及会话：{len(senders)} 个 —— {', '.join(f'{k}({v})' for k, v in senders.most_common(8))}")
    if lengths:
        lines.append(f"- 回复长度：平均 {statistics.mean(lengths):.0f} 字，最长 {max(lengths)} 字，"
                     f"中位 {statistics.median(lengths):.0f} 字")
    lines.append(f"- 草稿原因分布：{dict(reasons)}")
    lines.append("")

    flagged = [(r, risky_reasons(r.get("reply", ""))) for r in records]
    flagged = [(r, rs) for r, rs in flagged if rs]
    lines.append(f"## 需要重点看（{len(flagged)} 条）\n")
    if not flagged:
        lines.append("没有命中风险模式的草稿。\n")
    for record, rs in flagged[:40]:
        msg = record.get("message", {})
        lines.append(f"- **{', '.join(rs)}** | 对 {msg.get('sender_name', '?')}："
                     f"「{(msg.get('text') or '')[:40]}」 → 「{(record.get('reply') or '')[:80]}」")
    if len(flagged) > 40:
        lines.append(f"- …（还有 {len(flagged) - 40} 条，见导出报告）")

    suspicious_sensitive = [r for r in records if "敏感" in (r.get("reason") or "")]
    lines.append(f"\n## 被敏感规则拦下的（不应出现在 auto 的回复里）：{len(suspicious_sensitive)} 条\n")
    for record in suspicious_sensitive[:10]:
        msg = record.get("message", {})
        lines.append(f"- 对 {msg.get('sender_name', '?')}：「{(msg.get('text') or '')[:50]}」")

    lines.append("\n## 按会话分布\n")
    per_sender: dict[str, list[int]] = defaultdict(list)
    for record in records:
        per_sender[record.get("message", {}).get("sender_name", "?")].append(len(record.get("reply") or ""))
    for name, lens in sorted(per_sender.items(), key=lambda kv: -len(kv[1]))[:12]:
        lines.append(f"- {name}：{len(lens)} 条，平均 {statistics.mean(lens):.0f} 字")

    verdict = []
    if flagged:
        verdict.append(f"有 {len(flagged)} 条命中风险模式 —— **先逐条看过再考虑切 auto**")
    if lengths and statistics.mean(lengths) > 120:
        verdict.append("平均回复偏长（>120 字），不像真人发微信，建议收紧 persona 或 max_reply_chars")
    if any("自称 AI" in rs for _, rs in flagged):
        verdict.append("出现自称 AI 的回复 —— 这会直接暴露身份，**必须**调整 persona 后再上线")
    if not verdict:
        verdict.append("没有明显问题；仍建议人工抽查 10 条确认语气像本人")
    lines.append("\n## 结论建议\n")
    for item in verdict:
        lines.append(f"- {item}")

    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n已导出：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
