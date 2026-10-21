"""生成正式配置 bridge_config.json（从模板改，只动必要的几处）。

为什么用脚本而不是手改 JSON：手改容易漏字段、也容易写出语法错误；
另外本机 PowerShell 5.1 不支持 `-Encoding UTF8NoBOM`，用 Python 写才不会带上 BOM。

用法：
    python make_config.py                                  # 默认：wechatauto + 只监听文件传输助手 + draft
    python make_config.py --allow wxid_xxx                 # 追加白名单 id
    python make_config.py --mode auto                      # 改成自动回复（谨慎）
    python make_config.py --backend lmstudio               # 模型换成本机 LM Studio
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "bridge_config.template.json"
TARGET = ROOT / "bridge_config.json"

LMSTUDIO = {
    "base_url": "http://127.0.0.1:1234",
    "path": "/v1/chat/completions",
    "api_key": "lm-studio",
    "model": "local-model",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成正式配置")
    ap.add_argument("--allow", action="append", default=[], help="白名单里的会话/发送者 id，可重复")
    ap.add_argument("--group", action="append", default=[],
                    help="放行的**群** wxid（形如 xxx@chatroom），可重复；给了就自动打开 allow_groups。"
                         "群 id 用 python dump_raw_group.py 查")
    ap.add_argument("--listen-uid", action="append", default=None,
                    help="监听的会话 wxid（如 filehelper / wxid_xxx / xxx@chatroom），默认 filehelper")
    ap.add_argument("--listen", action="append", default=None, help="监听的会话显示名（次选，解析不可靠）")
    ap.add_argument("--mode", choices=["draft", "auto"], default="draft")
    ap.add_argument("--quiet", default=None,
                    help="静默时段，如 23:00-08:00（这两个时刻之间不回复，只留档）")
    ap.add_argument("--backend", choices=["deepseek", "lmstudio"], default="deepseek")
    ap.add_argument("--out", default=str(TARGET))
    args = ap.parse_args()

    cfg = json.loads(TEMPLATE.read_text(encoding="utf-8-sig"))

    # 1) hook：换成 wechatauto，并按 **wxid** 监听（显示名解析不可靠，实测会误判失败）
    cfg["hook"]["adapter"] = "wechat_bridge.adapters.hook_wechatauto:WechatautoHookAdapter"
    cfg["hook"]["options"] = {
        "listen_uids": args.listen_uid or ["filehelper"],   # 先用「文件传输助手」这个安全目标
        "listen_chats": args.listen or [],
        "gui": True,
        "send_timeout_sec": 90,
        "stale_after_sec": 900,
        # 发送后回读数据库确认落库：库报的「成功」是像素启发式，不核一次可能假成功
        "verify_send": True,
        "verify_window_sec": 20,
    }

    # 2) 策略：默认 draft（只写草稿），白名单只放显式给定的 id
    cfg["policy"]["mode"] = args.mode
    if args.quiet:
        start, _, end = args.quiet.partition("-")
        cfg["policy"]["quiet_hours"] = [start.strip(), end.strip()]
    rules = {k: v for k, v in cfg["policy"].get("contact_rules", {}).items() if not k.startswith("_")}
    rules.pop("wxid_alice", None)                      # 模板里的演示 id 一律清掉
    rules.pop("wxid_bob", None)
    # 模板里这两条是按**显示名**写的，而匹配只认 id（见 _match_contact_rule），等于永远不生效。
    # 换成真正的会话 id，并把本机这个账号实际存在的两个系统会话也显式标成 ignore。
    rules.pop("文件传输助手", None)
    rules.pop("微信团队", None)
    rules.setdefault("filehelper", "ignore")           # 文件传输助手：安全的自测目标，--allow filehelper 即可代回
    rules.setdefault("weixin", "ignore")               # 微信团队通知
    rules.setdefault("newsapp", "ignore")              # 新闻推送
    for wxid in args.allow:
        rules[wxid] = "allow"
    # 群聊：给了 --group 就自动放行群聊。
    # 否则 allow_groups 保持 false，群消息**在进状态机之前就被丢掉**——
    # 症状是"群聊参数配了一堆却完全没反应"，很难自己看出来。
    if args.group:
        cfg["policy"]["allow_groups"] = True
        for gid in args.group:
            rules[gid] = "allow"
    cfg["policy"]["contact_rules"] = rules

    # 3) 模型后端
    if args.backend == "lmstudio":
        cfg["llm"]["options"].update(LMSTUDIO)
    else:
        # DeepSeek：留空 api_key ⇒ 启动时会自动发现 DEEPSEEK_API_KEY / .env 里的密钥。
        # 写死 env:某个名字反而容易踩坑（用户设的变量名往往不是配置里写的那个）。
        cfg["llm"]["options"]["api_key"] = ""

    out = Path(args.out)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成 {out}")
    print(f"  hook      = {cfg['hook']['adapter']}")
    print(f"  监听       = {cfg['hook']['options']['listen_chats']}")
    print(f"  模式       = {cfg['policy']['mode']}（draft = 只写草稿不发送）")
    print(f"  静默时段   = {cfg['policy']['quiet_hours'][0]}-{cfg['policy']['quiet_hours'][1]}"
          f"（这段时间不回复，只留档；关机时段设在这里最合适）")
    print(f"  白名单     = {list(rules) or '（空：谁都不会被代回）'}")
    print(f"  模型       = {cfg['llm']['options']['base_url']} / {cfg['llm']['options']['model']}")
    if str(cfg["llm"]["options"].get("api_key", "")).startswith("env:"):
        print(f"  密钥       = 来自环境变量 {cfg['llm']['options']['api_key'][4:]}（启动前要先设置）")
    print("\n下一步：run_bridge.cmd --doctor（应在沙箱外的终端里跑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
