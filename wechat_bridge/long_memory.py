# -*- coding: utf-8 -*-
"""长记忆：跨天的"我还记得你"。

**为什么需要**（用户实测后提的）：
`context` 只保留最近 `max_turns` 轮，群聊环形缓冲也只留 30 条。
于是隔一天、或者说换个话题再回来，它就当第一次认识你——
"昨天说的那个事你查了吗？" → "什么？"。这不是模型笨，是**它真的没被喂进来**。

**做法**（全部离线，不依赖向量库）：
- 按 `chat_id` 落盘成 JSON（`state/memory/<会话>.json`），重启不丢；
- 写入两条路：
  1. **显式**：用户直接说"记住 我周五要去上海" → 不花 token，直接落盘；
  2. **摘要**：每 `extract_every` 次回复之后，让模型从最近对话里抽"值得长期记住的事实"，
     一次最多 `extract_max_items` 条（花一次很短的调用）；
- 读取：**关键词重叠打分 + 时间衰减**，取前 `recall_limit` 条塞进 system prompt。
  中文按二元组切（"美式咖啡" → 美式/式咖/咖啡），所以"咖啡"能命中；
- 忘掉：说"忘掉 咖啡"就删掉命中的条目——记忆系统必须能删，否则迟早都是错的。

设计取舍：
- **不做语义向量**：本机没有 embedding 服务，引入它等于给这个项目加一个必挂的依赖。
  二元组重叠对"寒暄/问句/短消息"这类场景够用，且零成本、可解释（能说出"为什么想起这条"）。
- **不无限增长**：每个会话最多 `max_items` 条，超了先扔"最久没被召回 + 最旧"的，
  但不是无脑按时间——高价值（kind=pref/event）与新近的条目会被保留。
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# 中文二元组 / 英文单词。刻意保留标点无关：只关心"内容词"。
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")

# 记忆条目的类型。`digest` 是**归档摘要**：旧条目被合并成的一两句话，
# 原始条目移进 `archive/`（不删，只是不再进提示词）。
KINDS = ("fact", "pref", "event", "digest")

# `add_verbose()` 的成功状态码（其余失败状态：duplicate / too_short / disabled）
ADDED = "added"


def tokens(text: str) -> set[str]:
    """把一句话切成可比对的小片段（中文二元组 + 英文/数字词）。"""
    s = str(text or "").lower()
    out: set[str] = set(_WORD.findall(s))
    for run in _CJK.findall(s):
        if len(run) == 1:
            out.add(run)                       # 单字词（"钱"/"图"）也得能命中
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


@dataclass
class MemoryItem:
    text: str
    kind: str = "fact"                 # fact=事实 / pref=偏好 / event=约定或事件
    ts: float = field(default_factory=time.time)
    source: str = ""                   # 哪来的：explicit（用户说的）/ auto（模型摘要）
    hits: int = 0                      # 被召回几次（淘汰时用来保护"常用的记忆"）
    last_hit_ts: float = 0.0

    def key(self) -> str:
        """去重用的归一化键：去掉空白与标点，只看字。"""
        return re.sub(r"\s+", "", re.sub(r"[，。！？、,.!?；;：:（）()\"'“”‘’]", "", self.text))


class LongMemory:
    """一个会话一份的长记忆。纯本地文件，无网络、无第三方依赖。"""

    def __init__(self, cfg: Any = None, root: Path | str = ".") -> None:
        self.cfg = cfg
        self.root = Path(root)
        self._cache: dict[str, list[MemoryItem]] = {}

    # ---------------- 基础 ----------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    def _dir(self) -> Path:
        return (self.root / str(getattr(self.cfg, "dir", "state/memory")))

    def _path(self, chat_id: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z@._-]", "_", str(chat_id)) or "default"
        return self._dir() / f"{safe}.json"

    def _max_items(self) -> int:
        return max(1, int(getattr(self.cfg, "max_items", 80) or 80))

    def _archive_path(self, chat_id: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z@._-]", "_", str(chat_id)) or "default"
        sub = str(getattr(self.cfg, "archive_dir", "") or "").strip()
        # 留空 = 跟着 `dir` 走（`<dir>/archive`）。
        # 为什么这么默认：测试与 demo 会把 `dir` 指到临时目录，如果 archive 还写死
        # 在 `state/memory/archive`，跑一次自测就往**真实归档**里塞假数据（踩过一次：
        # 归档条数多了一倍，查了半天才发现是自测写进来的）。
        base = (self.root / sub) if sub else (self._dir() / "archive")
        return base / f"{safe}.jsonl"

    # ---------------- 读写 ----------------

    def load(self, chat_id: str) -> list[MemoryItem]:
        chat_id = str(chat_id)
        if chat_id in self._cache:
            return self._cache[chat_id]
        items: list[MemoryItem] = []
        path = self._path(chat_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                for row in (raw.get("items") or []):
                    if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                        continue
                    items.append(MemoryItem(
                        text=str(row.get("text")),
                        kind=str(row.get("kind") or "fact"),
                        ts=float(row.get("ts") or 0.0),
                        source=str(row.get("source") or ""),
                        hits=int(row.get("hits") or 0),
                        last_hit_ts=float(row.get("last_hit_ts") or 0.0),
                    ))
            except Exception:
                items = []                     # 文件坏了就当空的：记忆不能拖垮主链路
        self._cache[chat_id] = items
        return items

    def save(self, chat_id: str) -> None:
        chat_id = str(chat_id)
        items = self._cache.get(chat_id, [])
        if not self.enabled:
            return
        path = self._path(chat_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"chat_id": chat_id, "saved_at": time.time(),
                       "items": [asdict(i) for i in items]}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass                           # 落盘失败不能影响回复

    # ---------------- 写入 ----------------

    def add(self, chat_id: str, text: str, kind: str = "fact", source: str = "",
            ts: Optional[float] = None) -> bool:
        """加一条，返回"是否真的新增了"（兼容旧调用）。

        需要区分"为什么没加成"时用 `add_verbose()`——**别把 too_short 当成 duplicate**，
        那会让用户听到"这个我已经记着了"，而实际什么都没记（2026-10-17 真机踩到）。
        """
        return self.add_verbose(chat_id, text, kind=kind, source=source, ts=ts) == ADDED

    def add_verbose(self, chat_id: str, text: str, kind: str = "fact", source: str = "",
                    ts: Optional[float] = None) -> str:
        """加一条，返回状态码：added / duplicate / too_short / disabled。

        `ts` 只在自测里用（要伪造"这条是三周前记的"），正常调用不传。
        """
        if not self.enabled:
            return "disabled"
        text = str(text or "").strip()
        if len(text) < int(getattr(self.cfg, "min_chars", 4) or 4):
            return "too_short"
        if kind not in KINDS:
            kind = "fact"
        items = self.load(chat_id)
        k = MemoryItem(text=text).key()
        for it in items:
            if it.key() == k:
                it.ts = max(it.ts, time.time())
                self.save(chat_id)
                return "duplicate"
        items.append(MemoryItem(text=text, kind=kind, source=source,
                                ts=float(ts) if ts is not None else time.time()))
        self._evict(items, chat_id)
        self.save(chat_id)
        return ADDED

    def _evict(self, items: list[MemoryItem], chat_id: str = "") -> None:
        """超过上限时淘汰：先按"价值分"排，分高者留。

        价值 = 类型权重 + 被召回次数 + 新旧。**不是简单按时间丢**——
        "他叫小王"这种事实比"今天吃了什么"更该留下。
        被淘汰的条目**先归档再移出**（`archive/*.jsonl`），否则"压缩"还没轮到它，
        原始记录就已经没了——那样归档层等于不存在。
        """
        cap = self._max_items()
        if len(items) <= cap:
            return
        now = time.time()

        def value(it: MemoryItem) -> float:
            w = {"pref": 2.0, "event": 1.5, "digest": 3.0}.get(it.kind, 1.0)
            age_days = max(0.0, (now - it.ts) / 86400.0)
            return w + 0.5 * min(it.hits, 5) + math.exp(-age_days / 30.0)

        ranked = sorted(items, key=value, reverse=True)[:cap]
        keep = {id(it) for it in ranked}
        dropped = [it for it in items if id(it) not in keep]
        if dropped and chat_id:
            self.archive_items(chat_id, dropped)
        items[:] = [it for it in items if id(it) in keep]
        items.sort(key=lambda it: it.ts)

    def forget(self, chat_id: str, needle: str) -> int:
        """删掉命中的条目，返回删了几条。空 needle 不删（防手滑全清）。"""
        needle = str(needle or "").strip()
        if not needle or not self.enabled:
            return 0
        items = self.load(chat_id)
        q = tokens(needle)
        removed = 0
        keep: list[MemoryItem] = []
        for it in items:
            hit = needle in it.text or (q and len(q & tokens(it.text)) > 0)
            if hit:
                removed += 1
            else:
                keep.append(it)
        if removed:
            items[:] = keep
            self.save(chat_id)
        return removed

    # ---------------- 读 ----------------

    def recall(self, chat_id: str, query: str, limit: int = 6) -> list[MemoryItem]:
        """按相关度取几条。相关度 = 关键词重叠 + 时间衰减 + 类型权重。"""
        items = self.load(chat_id)
        if not items or not self.enabled:
            return []
        q = tokens(query)
        now = time.time()
        scored: list[tuple[float, MemoryItem]] = []
        for it in items:
            overlap = len(q & tokens(it.text)) if q else 0
            # 重叠归一：除以查询片段数的平方根，避免长句天然占优
            base = overlap / math.sqrt(max(1, len(q)))
            if overlap == 0:
                base = 0.0
            age_days = max(0.0, (now - it.ts) / 86400.0)
            if it.kind == "digest":
                # 摘要条目是**压缩过的长期结论**，不该跟着时间衰减——
                # 否则"他叫什么、他对什么过敏"这种半年后依然成立的事会被新闲聊挤掉。
                recency = 1.0
                weight = 0.9
            else:
                recency = math.exp(-age_days / 30.0)      # 30 天半衰
                weight = {"pref": 0.6, "event": 0.3}.get(it.kind, 0.0)
            # 没重叠的条目只有在"很少"时才兜底带上（见下面 fill）
            scored.append((base * 3.0 + recency * 0.5 + weight, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [it for score, it in scored if score > 0][:limit]
        if len(top) < min(limit, len(items)):
            # 兜底：问到的东西一条都没命中时，也给他最近的几条（"你还记得我吗"这种泛问）
            for _score, it in scored:
                if it not in top:
                    top.append(it)
                if len(top) >= limit:
                    break
        for it in top:
            it.hits += 1
            it.last_hit_ts = now
        if top:
            self.save(chat_id)                 # 把 hits 落盘，迭代淘汰才有依据
        return top

    def render(self, chat_id: str, query: str, limit: Optional[int] = None) -> str:
        """渲染成要塞进提示词的一段话；没有记忆返回空串。"""
        if not self.enabled:
            return ""
        limit = int(limit or getattr(self.cfg, "recall_limit", 6) or 6)
        items = self.recall(chat_id, query, limit=limit)
        if not items:
            return ""
        label = {"fact": "事实", "pref": "偏好", "event": "约定", "digest": "摘要"}
        lines = [f"- [{label.get(it.kind, '事实')}] {it.text}" for it in items]
        return ("以下是你**之前记住**的、关于这个会话/这个人的事（不是这次对话里说的，是长期记忆）：\n"
                + "\n".join(lines)
                + "\n用得上就自然地用，别硬提；跟你现在要说的话无关就忽略。也别说出『根据我的记忆』这种话。")

    # ---------------- 显式指令 ----------------

    # 显式记忆指令（2026-10-18 扩过一轮同义词：真机上人说「记着」「备忘」「存一下」也算）。
    # 仍然只认**开头**是祈使句的情形——否则「我记住你了」会被当成指令。
    _REMEMBER = re.compile(
        r"^\s*(?:帮我|你给我|给我)?(?:记住|记一下|记着|记下来|记下|备忘|remember)"
        r"[：:，,\s]*(.+)$", re.I)
    _FORGET = re.compile(r"^\s*(?:忘掉|忘了|忘记|不用记了|forget)[：:，,\s]*(.+)$", re.I)

    def parse_remember(self, text: str) -> Optional[str]:
        m = self._REMEMBER.match(str(text or ""))
        if not m:
            return None
        content = m.group(1).strip(" 　，。.!！~～")
        return content or None

    def parse_forget(self, text: str) -> Optional[str]:
        m = self._FORGET.match(str(text or ""))
        if not m:
            return None
        return m.group(1).strip(" 　，。.!！~～") or None

    # ---------------- 分层：归档与压缩 ----------------

    def stale_items(self, chat_id: str, now: Optional[float] = None) -> list[MemoryItem]:
        """该被压缩的条目：**够老**的，以及**超出上限**的那部分。

        注意摘要是"结论"，不会被再次压缩——否则越压越空，最后什么都不剩。
        """
        items = self.load(chat_id)
        if not items:
            return []
        now = time.time() if now is None else now
        days = float(getattr(self.cfg, "archive_after_days", 21) or 0)
        picked: dict[int, MemoryItem] = {}
        if days > 0:
            for it in items:
                if it.kind == "digest":
                    continue
                if (now - it.ts) > days * 86400.0:
                    picked[id(it)] = it
        over = len(items) - self._max_items()
        if over > 0:
            candidates = [it for it in items if it.kind != "digest"]
            candidates.sort(key=lambda it: it.ts)          # 最旧的先走
            for it in candidates[:over]:
                picked.setdefault(id(it), it)
        return sorted(picked.values(), key=lambda it: it.ts)

    def archive_items(self, chat_id: str, items: list[MemoryItem]) -> int:
        """把原始条目追加进 `archive/<会话>.jsonl`。**只增不删**，永远可回溯。"""
        if not items:
            return 0
        path = self._archive_path(chat_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for it in items:
                    row = asdict(it)
                    row["archived_at"] = time.time()
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return len(items)
        except Exception:
            return 0

    def archived_count(self, chat_id: str) -> int:
        path = self._archive_path(chat_id)
        if not path.exists():
            return 0
        try:
            return sum(1 for _ in path.open(encoding="utf-8"))
        except Exception:
            return 0

    def compact_prompt(self, items: list[MemoryItem], limit: int = 3) -> list[dict[str, str]]:
        lines = "\n".join(f"- [{it.kind}] {it.text}" for it in items)
        return [
            {"role": "system",
             "content": (
                 "你是一个记忆压缩器。下面这些是**较早**的记忆，请把它们合并成不超过 "
                 f"{limit} 条要点。\n"
                 "保留半年后依然有用的东西：对方是谁、偏好与忌讳、约定、正在长期做的事、"
                 "重要的人名或项目名。丢掉一次性的细节（今天天气、路过的一句话、已经完成的小事）。\n"
                 "每条不超过 40 字，客观陈述，例如「他喜欢喝美式」「他在做一个微信群聊机器人」。\n"
                 '只输出 JSON 数组，不要解释：[{"kind":"digest","text":"..."}]'
             )},
            {"role": "user", "content": lines[-2000:]},
        ]

    def compact(self, chat_id: str, summarizer: Any = None,
                now: Optional[float] = None) -> dict[str, Any]:
        """把旧记忆压成摘要：**先摘要、再归档原文、最后才从活跃记忆里移除**。

        这就是用户要的三层：
          近的 → 上下文注入（`context` / 群环形缓冲，不落盘）
          旧一点的 → 长记忆条目（`state/memory/*.json`，进提示词）
          太老的 → 摘要成 `digest` 条目（继续进提示词，但只有几句话），
                   原文进 `archive/*.jsonl`（不再进提示词，但**永远查得到**）

        `summarizer(messages) -> str` 是任意"喂提示词、拿文本"的可调用对象
        （桥接里就是 `self.llm.complete`）。**它挂了也照样归档原文**，
        绝不因为模型不可用就把记忆留在原地无限膨胀。
        """
        stale = self.stale_items(chat_id, now=now)
        if not stale:
            return {"archived": 0, "digests": 0, "fallback": False}
        batch = max(1, int(getattr(self.cfg, "archive_batch", 8) or 8))
        group = stale[:batch]
        limit = max(1, int(getattr(self.cfg, "digest_max_items", 3) or 3))

        new_digests: list[str] = []
        fallback = False
        if summarizer is not None:
            try:
                raw = summarizer(self.compact_prompt(group, limit=limit))
                for kind, text in self.parse_extract(raw)[:limit]:
                    new_digests.append(text)
            except Exception:
                fallback = True
        else:
            fallback = True

        archived = self.archive_items(chat_id, group)
        drop = {id(it) for it in group}
        items = self.load(chat_id)
        items[:] = [it for it in items if id(it) not in drop]
        added = 0
        for text in new_digests:
            # 摘要归入 digest，`ts` 给"现在"——它是压缩后的结论，不该立刻又被当旧的压掉
            if self.add(chat_id, text, kind="digest", source="digest"):
                added += 1
        self.save(chat_id)
        return {"archived": archived, "digests": added, "fallback": fallback}

    # ---------------- 模型摘要 ----------------

    def extract_prompt(self, transcript: list[str], limit: int = 4,
                       aggressive: bool = False) -> list[dict[str, str]]:
        """让模型从对话里抽"值得长期记住的事"的提示词（严格 JSON 输出）。

        **2026-10-18 调整**：真机第一次跑自动摘要时模型返回了 `[]`，而那段对话里其实
        藏着可复用信息（"从**佳木斯**出发" ⇒ 他在佳木斯/他计划出行）。原来的提示词
        只列了"对方是谁、偏好、约定、正在做的事"，**没允许从上下文推断**，模型就保守地
        什么都不记。现在明确允许推断稳定事实，并把"计划/行程"单列。
        """
        return [
            {"role": "system",
             "content": (
                 "你是一个记忆整理器。从下面的聊天记录里，抽出**值得长期记住**的信息：\n"
                 "- 对方是谁、在哪、做什么（职业/店/项目）；\n"
                 "- 偏好、忌讳、身体情况（过敏/忌口）；\n"
                 "- 约定好的时间、地点、承诺；\n"
                 "- 正在做的事、正在计划的行程或待办；\n"
                 "- 重要的人名、链接、账号线索。\n"
                 "**允许从上下文合理推断稳定事实**（例如他说「从佳木斯出发」⇒ 他在佳木斯；"
                 "他说「我店里」⇒ 他有个店），推断要克制、只写有依据的。\n"
                 "**不要**记寒暄（早安/晚安/在吗）、一次性的情绪、刷屏的段子、你自己的想法，"
                 "也不要复述聊天记录。\n"
                 "每条都要能**脱离上下文独立读懂**：主语用「他」；群里那些昵称只是称呼，"
                 "除非它本身就是重要信息，否则别写进条目里。\n"
                 f"最多 {limit} 条，每条一句话（不超过 40 字），第三人称客观陈述，"
                 "例如「他喜欢喝美式」「他周五要去上海」「他在佳木斯」。\n"
                 '只输出 JSON 数组，不要任何解释：'
                 '[{"kind":"fact|pref|event","text":"..."}]。'
                 "**只有对话里确实没有任何可复用信息时才输出 []**。"
                 + (("\n**这一段多半是玩笑/吹牛，但也要记**：只要里面出现了关于对方的稳定属性"
                     "（职业、地点、身体情况、人际关系、正在处理的事），就抽出来；"
                     "用中性措辞、不复述剧情。只有整段确实没有任何这类信息时才输出 []。")
                    if aggressive else "")
             )},
            {"role": "user", "content": "\n".join(transcript)[-2000:]},
        ]

    @staticmethod
    def parse_extract(raw: str) -> list[tuple[str, str]]:
        """解析模型输出。容错：夹在 ```json 里、前后有废话，都要能取出来。"""
        s = str(raw or "").strip()
        if not s:
            return []
        m = re.search(r"\[.*\]", s, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
        out: list[tuple[str, str]] = []
        if not isinstance(data, list):
            return []
        for row in data:
            if isinstance(row, dict):
                text = str(row.get("text") or "").strip()
                kind = str(row.get("kind") or "fact").strip()
            elif isinstance(row, str):
                text, kind = row.strip(), "fact"
            else:
                continue
            if text:
                out.append((kind if kind in KINDS else "fact", text))
        return out
