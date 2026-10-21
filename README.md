# 微信 AI 群友（wechat-bridge）

> 版本 **0.1.0** · 2026-10-20  ·  变更见 [CHANGELOG.md](CHANGELOG.md)

把一个微信号接到大模型上：**被 @ 才开口**，像群友一样接话、看得懂图、回复不抢你的焦点。

## 先说清楚三件事

1. **7×24 挂着的是这个脚本，不是 DSH。** DSH 是请求-响应式的，你不叫它它不动；它负责写、改、读留档。
2. **个人号自动化违反腾讯服务协议**，后果可能是短期/永久封号。**用小号，别用主力号。**
3. **默认保守。** `policy.mode="draft"` 只写草稿不发；改 `auto` 才真发，且仍受白名单 /
   敏感词 / 限速 / 静默时段 / 熔断 / 急停开关约束。

## 先解压，再运行

别在压缩包里直接双击 `.cmd` / `.ps1`——Windows 会把它们解到临时目录再执行，于是弹
「打开这些文件可能会对你的计算机有害」。**先解压到文件夹再跑**就不会遇到。

如果副本是从浏览器/聊天工具拿到的，还要解除“来自 Internet”标记：

```powershell
# ⽅式一：右键 zip → 属性 → 勾选“解除锁定” → 再解压
# ⽅式二：解压后一次性解除所有文件的标记
Get-ChildItem -Recurse | Unblock-File
```

直接 `python bridge.py` 运行不受影响（它不经过 `.cmd`）。

## 五分钟上手

```powershell
cd <本目录>

python make_config.py --allow <要代回的会话id>   # 生成 bridge_config.json（默认 draft）
python list_contacts.py                          # 不知道 id？列出「id ↔ 昵称」
python resolve_key.py                            # 确认模型密钥能取到（不打印密钥）
run_bridge.cmd --doctor                          # 就绪自检：阻塞项应为 0
run_bridge.cmd --dry-run                         # 空跑：能读到、只打印不发送
run_bridge.cmd                                   # 去掉 --dry-run，保持 draft 观察一天
```

白名单为空时**谁都不会被代回**，这是刻意的默认。`make_config.py` 会清掉模板里的演示 id。

不知道选哪个会话？群是 `xxxxx@chatroom`，个人是 `wxid_xxx`。

## 密钥从哪来

`"api_key": "env:DEEPSEEK_API_KEY"` 会依次找：**环境变量 → 项目内 `.env` → `~/.dsh/.credentials.yaml`**。
本机 DSH 已登记 `DEEPSEEK_API_KEY`，所以这台机器不用配就能跑。

`env:NAME` 是**钉死名字**：三处都没有就明确报解析失败，不会偷偷换成别的 key。
密钥建议放 `.env`（`copy .env.example .env`），别写进配置文件。

## ⚠ 必须脱离沙箱运行

桥接不能在 DSH 沙箱里真跑——沙箱屏蔽进程枚举，取微信数据库密钥要读 `Weixin.exe` 内存，物理做不到。
**在你自己的 cmd/PowerShell 窗口**里运行；沙箱内仍可跑离线自测、改代码、读留档。

更隐蔽的一种：**收消息能成，只有发送会坏**。收消息走本地数据库（沙箱允许），发送走 UIA（沙箱必坏）。
所以**不要用"能收消息"推断"能发消息"**。启动时会自检并打印：

```
[wechatauto] 发送能力自检：UIA 控件树可用（发送走主路径）        ← 正常
[wechatauto] ⚠ 发送能力自检未通过：…                              ← 别上 auto
```

## 无人值守：三条命令

```bat
python agent_ctl.py up            :: 幂等地把整套跑起来（已在跑只报告，不起第二个）
python agent_ctl.py status        :: 什么情况；--json 给 agent 读
python agent_ctl.py down          :: 停掉（沙箱里调不动，得在自己终端跑）
```

**没有服务器**：关机/睡眠时它不工作，这是物理限制。开机后让 agent 说一句「把微信机器人拉起来」即可
（`up` 幂等——多个桥接挂同一个号 = 同一条消息被回多次）。

用别的配置必须显式指定，否则「叫一句起来的是另一份配置」：

```bat
python agent_ctl.py up --config state/_grouptest.json
```

日志（出问题先看这三个）：

```
state/bridge.stdout.log    启动行、[视觉]、[取图]、[合并]、[已回复]
state/bridge.stderr.log    库内部的 WARNING/异常栈
state/heartbeat.log        每 30 秒一行，判断"还活着吗"看它的 mtime
```

## 配置面板（可选，本机网页）

不想手改 JSON 就用它——双击 `launch_panel.cmd`，浏览器会自动打开 `http://127.0.0.1:8765/`：

- **开关**：运行模式（auto/draft）、发送方式、群聊状态机、说话判定层、识图、多段输出、静默时段；
- **起停**：启动 / 停止 / 重启桥接（内部调 `agent_ctl.py up|down`）；
- **人设**：直接编辑，或点「人设卡」往末尾追加一段（爱吃白饭、更冷淡、更热情、不发表情、回复不带 @；已加过不会重复）；
- **审计**：最近事件（`speak_silent` / `sent` …），排查「它为什么不理我」先看这里。

安全边界（故意做窄）：只绑 `127.0.0.1`；写操作要带启动时随机生成的 token；**只认白名单里的键**
（密钥不在名单里，也永远不会被返回给页面）；每次写先留 `.bak`（保留最近 5 份）、用临时文件原子替换；
校验不过就不写。

> 面板本身要跑在**沙箱之外**（和桥接一样）：它要起停 `bridge.py`，在受限环境里起的进程会被回收。

## 上线顺序（第一次接真实微信）

```powershell
python bridge.py --doctor        # 环境/配置/适配器/账号，阻塞项应为 0
python bridge.py --probe-llm     # 真模型连通
python send_check.py             # 真发一条到文件传输助手，证明"发得出去"
python bridge.py --dry-run       # 空跑走完整链路
run_bridge.cmd                   # 正式挂 draft，观察一天
python analyze_drafts.py         # 看草稿质量，再决定是否切 auto
```

## 自测（不需要微信、不需要凭据）

```powershell
$src = Get-Content -Raw -Encoding UTF8 selftest.ps1
& ([scriptblock]::Create($src))          # 全量回归（末尾打印 passed N / N；**桥接必须先停**）
python bridge.py --demo                  # 进程内跑通决策链
python panel_selftest.py                 # 配置面板：真起一个本地服务、真发请求
python check_env.py                      # 环境卡在哪
python persona_probe.py                  # 改完人设先跑这个：四条底线对真实模型验一遍
```

## 它能做什么 / 不能做什么

| 能力 | 状态 |
|---|---|
| 群里被 @ 才回、静默自动休眠（休眠不调模型、不花钱） | ✅ 真机验证 |
| 三条闭嘴规则：只接刚说话的人 / @ 别人的不接 / 链条上限 10 | ✅ |
| 说话决策层：**接不接这一句由模型自己判断**（看着最近聊天记录决定），@我 / 引用我的走快路直接回 | ✅ 每条"不说"都留原因码 |
| 多段输出（像真人分几次发） | ✅ 会话开着时分段；**没开着就压成一条**（免得连弹三次窗口抢焦点） |
| 看得懂图（微信 4.x 的 wxgf/HEVC 用 ffmpeg 转码 + 视觉模型） | ✅ 真机验证，取不到会如实说看不到 |
| emoji / 颜文字、别人发表情包能接梗 | ✅ |
| 人设：鲸鱼娘，**被问是不是 AI 直接承认**（不装人、不冒充官方）；爱吃白饭，被叫「大肥鱼」会顶嘴 | ✅ |
| 真 @ 别人 | ❌ 库没有这个接口；它会直说「我艾特不了」 |
| 发图片 / 发表情包 | ❌ **主动不做**：能发图就能被群成员诱导投递任意内容，出口是你本人号 |
| 私聊 | ❌ 默认关闭（`allow_unknown_contacts=false`），要开自己改 |

限速（改 `policy`）：**6 条/分钟、60 条/小时、单人 20 条/小时**；静默时段默认 `23:00–08:00`。
几十人的群里十个人同时 @ 它，大半会被丢弃——那是限速，不是 bug。

## 配置要点

| 项 | 说明 |
|---|---|
| `hook.adapter` | 真机用 `hook_wechatauto`；`hook_mock` 演示，`hook_http` 接自写 shim |
| `llm.options.api_key` | 写 `env:DEEPSEEK_API_KEY`，别写明文 |
| `policy.mode` | `draft` 只留草稿 / `auto` 真发。**先 draft 观察一天** |
| `policy.contact_rules` | 白名单。只回 `allow` 的人；群里靠 `allow_any_group` |
| `policy.deny_patterns` / `deny_regex` | 钱、验证码、承诺一律不回 |
| `group.proactive` | 主动插话。大群第一天建议 `false` |
| `speak_gate.mode` | `model`（默认，模型自己判断接不接）｜`rules`（零成本确定性口径） |
| `speak_gate.ack_words` | 「只是应了一声」的词表（给模型当提示 / rules 模式当判据），留空用内置表 |
| `speak_gate.model_max_per_min` | 每分钟最多问模型几次，超了按「不说」处理（延迟保险丝） |
| `group.observe_first_msgs` | 新群观察期。几十人的群建议 10~20 |
| `segments.*` | 多段输出：最多 3 段、每段 ≤80 字、段间停 1.5–4 秒 |
| `state/state.json` 的 `kill_switch` | **人工急停**：改成 `true`，下一轮就停止发送 |

## 依赖装配（不用 pip）

本机 pip 会因沙箱禁止写系统临时目录而失败，`pkg_fetch.py` 直接把 wheel 解包到 `.pylibs\`：

```powershell
# 版本已钉在 .deps.txt 里（那份是本机跑过 23 项回归的版本组合）
python pkg_fetch.py wechatauto-replica uiautomation pywin32 comtypes cryptography Pillow psutil `
  colorama zstandard opencv-python cffi numpy pyperclip imageio-ffmpeg

# OCR 那一套必须单独装（不装的话「群里 @ 人」必失败）
python pkg_fetch.py winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging `
  winrt-Windows.Storage winrt-Windows.Foundation winrt-Windows.Globalization typing-extensions

python smoke_wechatauto.py     # 冒烟：能否 import
```

**依赖源**：全部来自 PyPI。`pkg_fetch.py` 默认**依次尝试三个索引**，谁先通用谁——
阿里云镜像（`mirrors.aliyun.com/pypi/simple`，实测最快）→ 清华镜像
（`pypi.tuna.tsinghua.edu.cn/simple`）→ 官方（`pypi.org/simple`，直连慢）。
想指定就加 `--index <url>`（可重复，按顺序试）；上面对应的 `-i` 是它的简写。

> 版本号不是随手写的：`.deps.txt` 里那组是**本机实际验证过的**。尤其是
> `wechatauto-replica`——它管群消息/引用/图片的解析，升级前先跑
> `python group_selftest.py` 和 `python adapter_selftest.py`，否则容易出现
> "单测全绿、真机装死"。

> **不要装 `winsdk`**：它最后一版是 cp39 轮子，本机 Python 3.14 加载必失败。
> 用现代 `winrt-*` + `.pylibs/winsdk/` 里的 import 别名 shim 即可（不改库一行代码）。

## 目录结构

```
bridge.py                 主程序：主循环 + 风控闸门 + 留档（--doctor/--validate/--dry-run）
agent_ctl.py              无人值守入口：up / status / down
panel/panel_server.py     配置面板后端（本机 HTTP，只用标准库）
panel/panel.html          配置面板页面（单文件）
bridge_config.template.json 配置模板（make_config.py 从它生成）
persona_probe.py          人设探针：四条底线对真实模型验一遍
dump_raw_group.py         群消息原始正文 dump（定标 @ 形态）
probe_image.py            图片链路探测：这张图卡在哪一步
check_env.py              环境自检
*.py + selftest.ps1       各模块离线自测；selftest.ps1 是一键回归
wechat_bridge/            核心包：config / state / at_detect / group_state / segments /
                          long_memory / console / adapters（hook_wechatauto、llm、vision）
state/                    运行时产物：audit.jsonl / state.json / drafts/ / heartbeat.log
```

> ⚠ 改 `wechat_bridge/adapters/hook_wechatauto.py` 时注意：那里有**两条**把原始消息转成
> `Message` 的路径——`_to_message()`（轮询）与 `_row_to_message()`（监听回调，**真机跑的是这条**）。
> 新能力两条都要加，否则「单测全绿、真机装死」。

## 安全清单（切 auto 之前逐条确认）

- [ ] 用小号，不绑重要支付与实名业务
- [ ] 先跑 `draft` 至少一天，人工看过草稿质量
- [ ] 白名单只放需要代回的人；群聊先只放测试群
- [ ] 敏感规则覆盖：钱、验证码、身份证、合同、承诺时间
- [ ] 静默时段覆盖睡眠时间
- [ ] 知道急停开关在哪（`state/state.json` 的 `kill_switch`）
- [ ] 定期看 `state/audit.jsonl`：**每条回复与被忽略的原因都在里面**

## 许可与免责

**MIT 许可证**（见 [LICENSE](LICENSE)）：可以随便用、改、再分发，保留版权声明即可。

三条免责，请当真：

1. 本项目**与腾讯、微信无关**，不是官方工具；个人号自动化**违反微信服务协议**，封号风险由使用者自负。
2. 本项目**与 DeepSeek / 幻方无关**。默认人设里的「鲸鱼娘」只是社区流行的拟人形象，不代表任何官方身份；
   它被要求**不冒充官方账号或客服**。
3. 软件按「现状」提供，不附带任何担保。作者不对封号、数据丢失、或它替你说错话造成的后果负责——
   **切 uto 之前请自己按上面的安全清单过一遍。**

## 其他文档

[README_FULL.md](README_FULL.md) 完整版（设计论述、踩坑细节、环境实测都在这里）·  
[STATUS.md](STATUS.md) 现在在哪 · [DESIGN.md](DESIGN.md) 设计取舍 ·
[OPEN-BUGS.md](OPEN-BUGS.md) 未修问题 · [KNOWN_ISSUES.md](KNOWN_ISSUES.md) 踩过的坑 ·
[DESIGN-BACKGROUND.md](DESIGN-BACKGROUND.md) 后台化方案
