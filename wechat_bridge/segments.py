"""把模型的一次响应切成"真人连说几句"的多段。

**为什么需要它**：真人发微信不是把三句话压成一条长消息，而是分几次发。
模型一次就能把内容全生成出来，所以"节奏"只能在**发送侧**补——
这是拟人度上最划算的一笔（见 DESIGN.md 2.5）。

**约定**：模型用 `|||` 分隔多段；桥接切开、逐段发、段间随机 1.5–4 秒；
上限 3 段、每段 ≤80 字（防刷屏 + 防话痨）。

**一条硬规则：绝不静默丢内容。**
模型偶尔不守约定（多写几段、或某段特别长）。此时只有两种选择：
如实发出去并在审计里标明"越界了"，或者丢掉一部分。
丢内容 = "看起来回复正常、其实话说了一半"，正是这个项目最忌讳的"假装成功"。
所以这里选前者：**内容全发，越界写进 notes**（notes 会进审计，看得见才修得了）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# `|||` 是约定分隔符；同时容忍模型改用空行分段（它常见这么干）。
_SEP_RE = re.compile(r"\s*\|\|\|\s*|\n\s*\n+")

# 句末标点：段落超长时优先在这里断开，而不是硬砍。
_SENTENCE_END = "。！？!?…；;"


@dataclass
class SplitResult:
    """切分结果。``notes`` 里每条都是"模型没守约定"的如实记录。"""

    segments: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:                       # 方便 `if result:`
        return bool(self.segments)


def _split_long(text: str, max_chars: int) -> list[str]:
    """把一段长文本在**句末标点**处断成若干片段。

    找不到可断之处时**不硬砍**：硬砍会把词切成两半，读起来像程序出错。
    宁可让这一段超长（并在 notes 里说明），也不切坏它。
    """
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if len(buf) >= max_chars and ch in _SENTENCE_END:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts if len(parts) > 1 else [text]


# 自动分段的断点候选与"这一刀像不像人说话"的权重。
#   句末标点最强（真人就是这么断句的）；冒号次之（"那我讲一个："后面本来就是另起一句）；
#   逗号最弱——单独用它断出来像喘不上气，所以只有在没别的选择时才用。
_GAP_PATTERNS: tuple[tuple[int, "re.Pattern[str]"], ...] = (
    (3, re.compile(r"(?<=[。！？!?…；;])")),
    # 冒号只在**中文冒号**、或前面是汉字时才当断点。
    # 否则颜文字会被拦腰砍断：`_(:з」∠)_` 里的 `:` 后面跟着 `з`，
    # 用裸 `:` 做断点会切出 `_(:` + `з」∠)_` —— 真机上就是一条乱码消息。
    (2, re.compile(r"(?<=[\u4e00-\u9fff])[：:]|(?<=：)")),
    (1, re.compile(r"(?<=[，,、])")),
)


def _cut_candidates(text: str) -> list[tuple[int, int]]:
    """所有可断位置及其权重：``[(位置, 权重), ...]``（位置 = 断点后的下标）。"""
    out: dict[int, int] = {}
    for weight, pattern in _GAP_PATTERNS:
        for match in pattern.finditer(text):
            pos = match.end()
            if 0 < pos < len(text):
                out[pos] = max(out.get(pos, 0), weight)
    return sorted(out.items())


def auto_split(text: str, *, max_segments: int = 2, max_chars: int = 80,
               min_total_chars: int = 14, min_piece_chars: int = 4) -> list[str]:
    """模型没按约定给分隔符时，按标点把它拆成几条。只在"拆了更像人说话"时才拆。

    ## 为什么必须有它

    `|||` 是模型自愿遵守的约定。真机 112 条回复里 **0 条**真的用了它——"像真人连说几句"
    从来没发生过。约定式方案不能只有约定，还得有一条**不依赖模型配合**的路。

    ## 第一版为什么还是没生效（2026-10-18 20:46 真机）

    第一版只认**句末标点**且要求 ≥24 字。可群聊里的回复长这样：

        行，那我讲一个：番茄和土豆打架，西红柿去劝架，结果被做成阿甲了   （34 字，一个句号都没有）

    一句话没带句号 ⇒ 一个断点都找不到 ⇒ 永远不拆。**微信聊天本来就不爱打句号**，
    所以断点必须包括冒号和逗号。

    ## 现在的判据

    - 总长 ≥ `min_total_chars`（默认 14）才算"值得分两条"；
    - 断点按权重挑：句末标点 3 > 冒号 2 > 逗号 1，**优先用强的**；
    - 每段不短于 `min_piece_chars`（默认 4），否则并给前一段；
    - 任何一段超过 `max_chars` 就不拆（那本来就是一段长文，硬拆更难看）；
    - 逗号只在没有句末/冒号断点时才用，且总长要够。

    ## 保证

    返回两段以上时，`"".join(结果) == text`（**一个字都不丢、一个空格都不加**）。
    返回 `[]` 表示"不该拆，按一条发"。
    """
    raw = str(text or "")
    if not raw.strip() or "|||" in raw:
        return []                                   # 模型自己分了段 / 空串，不插手
    t = raw
    stripped_len = len(t.strip())
    if stripped_len < max(1, int(min_total_chars)):
        return []

    min_piece = max(1, int(min_piece_chars))
    limit = max(2, int(max_segments))
    # 逗号断点要求整体够长：短句用逗号断出来像喘不上气（「怕啥，我这不是在组织语言嘛」不拆）
    comma_ok = stripped_len >= max(18, int(min_total_chars))
    by_weight: dict[int, list[int]] = {}
    for pos, weight in _cut_candidates(t):
        if weight == 1 and not comma_ok:
            continue
        by_weight.setdefault(weight, []).append(pos)

    # **先用强断点，够了就不再往下掉**：句末标点拆得出来就绝不用逗号，
    # 否则会出现「番茄和土豆打架，」这种半截气口的消息。
    for weight in (3, 2, 1):
        positions = by_weight.get(weight) or []
        if not positions:
            continue
        # 逗号断点最多只切一刀（≤2 条）：真机 21:03 出现过三条都以「，」结尾的回复，
        # 「笑点就是…冷场，／冰箱一天…有点凉，／说明…」——每条都吊着半个气口，读着别扭。
        # 逗号本来就不是真人分条的断点，能少用就少用。
        cap = 1 if weight == 1 else limit - 1
        accepted: list[int] = []
        for pos in sorted(positions):
            if len(accepted) >= cap:
                break
            if pos < min_piece or len(t) - pos < min_piece:
                continue
            if any(abs(pos - other) < min_piece for other in accepted):
                continue
            accepted.append(pos)
        if not accepted:
            continue
        bounds = [0] + accepted + [len(t)]
        pieces = [t[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
        pieces = [p for p in pieces if p.strip()]
        if len(pieces) < 2:
            continue
        if any(len(p.strip()) > int(max_chars) for p in pieces):
            continue
        return pieces
    return []


def split_segments(text: str, *, max_segments: int = 3, max_chars: int = 80) -> SplitResult:
    """按约定把一次响应切成多段。**任何输入都不会抛异常，也绝不丢内容。**"""
    notes: list[str] = []
    if not text or not str(text).strip():
        return SplitResult([], notes)
    if max_segments < 1:
        max_segments = 1

    pieces = [p.strip() for p in _SEP_RE.split(str(text).strip())]
    pieces = [p for p in pieces if p]
    if not pieces:
        return SplitResult([], notes)

    # ① 段数超上限：多出来的**并进最后一段**（不丢内容），并记一笔。
    if len(pieces) > max_segments:
        merged = " ".join(pieces[max_segments - 1:])
        notes.append(f"模型给了 {len(pieces)} 段，超过上限 {max_segments}；多出的已并入最后一段")
        pieces = pieces[: max_segments - 1] + [merged]

    # ② 单段超长：优先在句末标点断开（真人遇到长内容也会分两条发），避免一堵文字墙。
    out: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            out.append(piece)
            continue
        parts = _split_long(piece, max_chars)
        if len(parts) > 1:
            notes.append(f"有一段超过 {max_chars} 字，已在句末标点处自动断成 {len(parts)} 条")
        else:
            notes.append(f"有一段超过 {max_chars} 字，但没有可断的句末标点，原样发出（不切坏它）")
        out.extend(parts)

    return SplitResult(out, notes)
