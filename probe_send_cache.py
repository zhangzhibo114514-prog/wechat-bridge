r"""上线前自检：确认**发送缓存**能把会话 id 解析成绑定该 wxid 的 Chat 对象。

跑法：python probe_send_cache.py        （不需要微信之外的任何东西，**不发送任何消息**）

**为什么需要它，以及它和 `send_check.py` 的分工**：
- `send_check.py` 证明的是"**消息真的发得出去**"（会真发一条，走 `prime_chat` 手动预热）；
- 本脚本证明的是"**真机监听路径能自己拿到会话对象**"（不发消息）。

这两件事不一样，而且都各自出过问题：`send()` 的硬规则是"缓存里没有这个会话就拒绝发送"，
缓存原本只由轮询路径填充；真机走的是监听回调路径（拿到的是原始行 dict，**天生没有 Chat 对象**），
所以即使 @ 判定修好了，切 auto 后仍可能表现为
「@ 醒了 → 回复生成了 → 发送环节『拒绝发送：会话不在缓存里』」。

`_ensure_chat()` 里那条 `resolved != chat_id` 的校验是**故意严格**的：
解析不出来、或解析成了别的会话，就拒绝缓存——宁可发不出去，也不发错人。
本脚本就是用来确认这条严格校验在真机上**站得住**（不会把所有正常会话都误拒）。

**切 auto 之前的顺序**：先跑本脚本（会话解析）→ 再跑 `send_check.py`（真发一条）→ 才切 auto。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_bridge.adapters.hook_wechatauto import WechatautoHookAdapter   # noqa: E402

# 默认只探这两个：群聊（测试群）与私聊（文件传输助手）。
# 换成你自己的：群 = xxx@chatroom，私聊 = 对方 wxid，文件传输助手 = filehelper。
TARGETS = ["11111111111@chatroom", "filehelper"]


def main() -> int:
    # 必须给至少一个监听项：`connect()` 在"一个会话都没注册上"时会直接报错退出
    # （那是防呆——静默启动但收不到任何消息是最难查的故障）。本脚本不依赖它收到消息。
    adapter = WechatautoHookAdapter(listen_uids=["filehelper"], gui=True)
    print("正在连接微信（找主窗口 + 解密数据库）…")
    adapter.connect()
    print(f"自身身份：wxid={adapter._self_wxid} 昵称={adapter._self_nick or '(无)'}")
    print(f"群里可能被叫到的名字：{adapter._nicknames()}")

    rc = 0
    for target in TARGETS:
        ok = adapter._ensure_chat(target)             # noqa: SLF001 —— 就是来测它的
        chat = adapter._chat_cache.get(target)        # noqa: SLF001
        print(f"[{'OK' if ok else '失败'}] {target}"
              f" → 已缓存={ok}"
              f" who={getattr(chat, 'who', None)!r}"
              f" 解析出的 wxid={getattr(chat, '_wxid', None)!r}")
        if not ok:
            rc = 1

    adapter.close()
    print("结论：" + ("发送缓存可正常解析，可以进入下一步（send_check.py 真发一条）"
                    if rc == 0 else
                    "有会话解析失败——切 auto 前必须先修，否则会出现『回复生成了但发不出去』"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
