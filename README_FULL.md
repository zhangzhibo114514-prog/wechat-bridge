# wechat-bridge —— 微信个人号代理桥接

> 📄 **这是 0.1.0 之前的完整版 README**（精简版见 [README.md](README.md)）。
> 重写覆盖后由会话记录重放还原，正文未改写；重放过程中重复出来的一个旧小节已删除。

把「一个微信号」接到大模型上，由模型按人格设定代收代回。本目录是**桥接层**：收消息 →
过风控闸门 → 生成回复 → 发送，全部逻辑可读、可改、可审计。

> 📌 **先看 [STATUS.md](STATUS.md)** —— 当前进度、暂停点、以及"下次从哪继续"都在那里。
> 本文件是完整说明，那一个是"我现在在哪"。

**决策已定（2026-10-15）：用独立小号、留在微信 4.x、依赖离线装配（不需要代理）。**
小号意味着封号不伤主力账号；但协议风险依然存在，请仍然按下面的安全清单执行。

## 先明确三件事（别跳过）

1. **这不是 DSH 本人在微信里说话。** DSH 是请求-响应式 agent，没有常驻进程，你不发消息它不运行。
   真正 7×24 挂着的是本目录这个脚本；DSH（我）负责写它、改它、读它的留档。
2. **个人号自动化违反腾讯服务协议。** 后果可能包括：短期封禁、永久封禁、账号被标记。
   2021 年深圳南山法院已判过微信外挂案（提供者获刑）。**用小号，别用主力号。**
3. **默认保守。** `policy.mode = "draft"` 时只写草稿不发消息；改 `auto` 才会真发，
   且仍受白名单 / 敏感规则 / 限速 / 静默时段 / 熔断 / 急停开关约束。

## 当前环境实测（2026-10-15）

| 项目 | 实测 | 影响 |
|---|---|---|
| 微信客户端 | `C:\Program Files\Tencent\Weixin\Weixin.exe` **4.1.15.13** | 比 wxhook 适配表最高的 4.1.13.56 还新 ⇒ **框架可能认不出，必须先用它自带 demo 试连** |
| Python | 本机 3.14.7（`C:\Python314`） | 4.x 那批框架基于 3.8–3.11；建 venv 时优先 3.11 |
| 出口 | **urllib 测得 github.com / pypi.org / 微信官网可达但极慢**（PyPI 首包 42s）；`raw.githubusercontent.com`、`cdn.jsdelivr.net` 直接被重置 | 拉 SDK 要走代理；pip 要加 `--timeout 90 --retries 3` 并用国内镜像 |
| 4.x UI 自动化路线 | `wechatauto-replica`（导入名 `wechatauto`）**已实测可从 aliyun 镜像取到 wheel**（473 KB，含 demo_listen / demo_send 等 30 多个示例） | 它不对客户端版本做绑定 ⇒ 是「留在 4.x」这条路上目前唯一可验证的技术选项；代价见下 |
| 模型凭据 | `~/.dsh/.credentials.yaml` 的 `refs:` 段里已有可用的 `DEEPSEEK_API_KEY`（真机 `--probe-llm` 已实测通过，返回「通了」） | 模型侧不用重新申请、也不用再设环境变量：`api_key` 写 `env:DEEPSEEK_API_KEY` 就能直接取到 |
| 小号状态 | `Documents\WeChat Files\wxid_other_user` 是 2025-06 的旧数据；4.x 的 `Documents\xwechat_files` **不存在** ⇒ 4.x 客户端还没登录过任何账号 | 端到端联调前必须先在这个客户端上登录你要托管的小号 |

## 夜间关机怎么处理（本机实际情况：23:00 后不开机）

这不是缺陷，是**要显式处理的边界**。三件事各归各位：

| 问题 | 处理方式 |
|---|---|
| 23:00 后不能开机，代理没法工作 | **配置静默时段**：`--quiet 23:00-08:00`。这段时间内不回复、只留档；既不会在你不知情时替你说错话，也不会在早上补发一堆"迟到回复" |
| 早上开机后要重新接入 | **登录后自动启动**：`python setup_autostart.py install`（延迟 1 分钟启动、崩了自动重试 3 次）；跑起来之后自己会连微信、注册监听 |
| 运行中微信重启/睡眠唤醒后"装死" | **自愈**：连接失败按 5s→…→300s 退避重试；运行中每 30 秒查一次健康，不健康就重连 |

收工脚本（比直接关机干净：有序退出、清锁、打印最终统计）：

```powershell
python nightly_stop.py              # 只停桥接
python nightly_stop.py --shutdown   # 停完关机（60 秒倒计时，shutdown /a 可取消）
python nightly_stop.py --shutdown --at 23:00   # 到点自动执行（前台等待，Ctrl+C 取消）
```

**消息不会因为关机丢**：对方在你离线时发的消息留在腾讯服务器，微信下次登录会同步下来；
代理做的是"代收代回"，离线期间缺席的只是"回"这部分。

**实测补充（2026-10-15）**：监听线程在桥接启动时会把**停机期间落库的消息**一并投递过来（不是只收启动之后的新消息）。
这条是好消息也是风险——好在重启不丢消息；风险在于"补发一堆昨晚的回复"。
`policy.max_reply_age_min`（当前 15 分钟）就是拦这个的：超时的消息只留档、不回。
两道闸门是配套的，**别只开一个**。

> 想让 23:00 后也有人回，只有一条路：换一台**常开的机器**（老笔记本/迷你主机）跑这套东西。
> 方案本身不绑定这台机器，但那台也得登录微信小号、且不能锁屏。

## 无人值守：重启之后让 agent 帮你拉起来

**先把话说清楚：这个程序没有服务器。** 电脑关机 / 睡眠 / 锁屏时它就是不工作的——
没有云端进程可以代跑，这是物理限制，不是实现缺陷。

**开机后一句话就够。** 用这个工具的人基本都装着 DSH 或类似的 agent，
所以不需要装计划任务：跟 agent 说「**把微信机器人拉起来**」，它执行的就是：

```bat
python agent_ctl.py up            :: 该起的都起；已经在跑就只报告，不起第二个
```

> `up` 是**幂等**的。这一点很要紧：多个桥接同时挂着同一个号 = **同一条消息被回复多次**，
> 而且各自的限速计数互相看不见。

### 三条命令（agent 用的只有前两条）

```bat
python agent_ctl.py up            :: 幂等地把整套跑起来
python agent_ctl.py status        :: 看现在到底什么情况
python agent_ctl.py status --json :: 给 agent 的机器可读版
python agent_ctl.py down          :: 停掉（沙箱里调不动，得在你自己终端跑）
```

`status --json` 给出 `running` / `pid` / `wechat` / `last_heartbeat_sec` / `mode` / `config` /
`blocked` / `hint`；没跑的时候 `hint` 直接写着下一步。

`wechat` 字段是**三态**：`ok` 在跑 / `not_running` 确实没开 / **`unknown` 看不到**。
第三态很要紧：DSH 沙箱里进程枚举被拒，此时「看不到」不等于「没开」——
早先它就在沙箱里误报过「微信进程：没开」，可当时桥接正跑着、心跳只有 9 秒。

### 用哪份配置启动（容易踩）

`up` 默认用 `bridge_config.json`。想用别的（比如实测档 `state/_grouptest.json`）就显式指定，
**否则「开机叫一句」起来的是另一份配置，行为和你手上的完全不同**：

```bat
python agent_ctl.py up --config state/_grouptest.json
python agent_ctl.py status --config state/_grouptest.json
```

不确定在用哪份？`status` 会把配置文件路径直接打出来。

### 起来之后日志在哪

`up` 用 `pythonw` 后台起，**stdout/stderr 落到文件**，不会丢进黑洞：

```
state/bridge.stdout.log     启动行、[视觉]、[取回]、[合并]、[已回复] 都在这里
state/bridge.stderr.log     库内部的 WARNING/异常栈
state/heartbeat.log         每 30 秒一行心跳（判断"还活着吗"看它的 mtime）
```

出问题先看这三个文件；`[视觉] 理解失败` / `[取图] …` 这类行能直接指出卡在哪一步。

### 开机要跨的三道门

| 门 | 能不能自动 | 说明 |
|---|---|---|
| Windows 登录 | 可以（需一次性设置） | 重启后停在锁屏时**任何程序都不会跑**。要真正无人干预得开自动登录（`netplwiz`），代价是**谁碰到你电脑都能进桌面** |
| **微信登录** | **不能完全自动** | 勾了"自动登录"一般能自己进，**但有时需要手机上点确认——这一步脚本替不了** |
| 程序自启 | 可以，见下 | `python setup_autostart.py install` |

所以"重启后完全无人干预"成立的条件是三者齐备。缺一就需要有人在机器上点一下。

### 自启任务做了什么

跑的是 `agent_ctl.py up`——**和 agent 手动拉起用的是同一个入口**，两个触发：

- **登录后 1 分钟**启动
- **每 10 分钟**检查一次，不在跑就拉起 ← 这是**自愈**

为什么需要第二个触发：计划任务自带的"失败重试"只覆盖**启动失败**，不覆盖**跑着跑着卡死**；
而且 Windows **不会因为睡眠唤醒而补触发**登录触发器。一个 10 分钟一次的幂等检查把两种情况一起兜住。

```bat
python setup_autostart.py install     :: 装（必须在**你自己的终端**里跑，DSH 沙箱会拒）
python setup_autostart.py status      :: 看状态与最近心跳
python setup_autostart.py selftest    :: 离线校验任务 XML（不动正式任务，随时可跑）
python setup_autostart.py remove      :: 卸
```

> ⚠️ 沙箱里跑 `install` 的报错是「系统找不到指定的路径」，看着像 XML 写错了，其实只是没权限。
> `selftest` 会先做离线校验，把这两种情况区分开。

## 被拉进新群就能用（自动接入）

**默认是关的**，需要显式打开两处（少一处都不行）：

```json
"hook": { "options": { "listen_all": true, "listen_uids": [] } },
"policy": { "allow_groups": true, "allow_any_group": true }
```

这两项各自解决一半问题，缺一半的症状很不一样：

| 缺哪一项 | 症状 | 为什么 |
|---|---|---|
| `listen_all` 没开 | 新群的消息**根本收不到** | `listen_uids` 是按 wxid **逐个注册**的，而新群的 id 不可能预先写进配置 |
| `allow_any_group` 没开 | 收得到，但一律被判「非放行联系人」 | 新群不在 `contact_rules` 里，白名单默认拒绝 |

`listen_all: true` 走的是库的 `AddListenAll(callback, discover=True)`：它把回调挂到**所有已知会话**上，
并在轮询过程中**自动发现新出现的会话（如新群聊）并注册** —— 这就是"拉进群即可用"的机制来源。

**放行的边界（写在这里，因为这是放行面，写错方向就是把陌生人放进来）**：

- `allow_any_group` **只对群聊生效**，私聊不受影响（私聊仍走 `allow_unknown_contacts`）；
- `contact_rules` 里**显式写 `ignore` 或 `deny` 的群依然被拒**——显式拒绝优先于自动放行；
- 放行 ≠ 会说话：仍要过「@ 才回 / 群状态机 / 限速 / 静默时段 / 敏感词」全套闸门；
- 想让新群先"看一会儿再说"，把 `group.observe_first_msgs` 设成正数（默认 0 = 不观察）。

这四条都有离线断言钉着（`gate_selftest.py` 的「新群自动接入」一节），改闸门时会被拦住。

## 让它看懂图片（可选，默认关）

**先说清楚：不配的话，它是假装看的。** 群友发张图，AI 会说"这是什么？""我看看"——
但它没看。这在群里是**会穿帮的**，所以长期用建议配上。

原因：主模型 `deepseek-chat` **不支持图片输入**。所以"看懂图"必须另找一个能看图的模型。

### 两个前置条件，缺一个都等于"看不到"

| 条件 | 为什么 | 怎么满足 |
|---|---|---|
| `hook.options.accept_images = true` | 不开的话**图片在适配器层就被丢掉**，桥接根本收不到（日志里一点痕迹都没有，最容易误判成"模型不认图"） | 配置里写一行（模板已开） |
| 本机有 **ffmpeg** | 微信 4.x 的图落在本机是 `xxx.wxgf`——**微信容器套 HEVC 裸流**，视觉接口只吃 jpg/png/webp/gif，必须先转码 | `python pkg_fetch.py imageio-ffmpeg`（解包进 `.pylibs`，自带 win64 二进制，**不用另装系统 ffmpeg**）；或用 `hook.options.ffmpeg_exe` / 环境变量 `WECHAT_BRIDGE_FFMPEG` 指到你已有的 ffmpeg |

两个条件是否满足，一条命令就能看清：

```powershell
python check_env.py        # 会打印「ffmpeg 可用：…」或「⚠ 没有 ffmpeg …」
python vision_check.py 某张图.jpg --config bridge_config.json   # 打印后端是谁 + 它看到了什么
```

桥接启动时也会打印 ffmpeg 状态；`--validate` 缺 ffmpeg 会直接列成待处理项。

### 配本机 LM Studio（免费，推荐先试这个）

1. 打开 LM Studio，下载并加载一个视觉模型（7B 级就够，比如 `qwen2-vl-7b-instruct`）
2. 启动它的本地服务器（默认 `http://127.0.0.1:1234`，**这个端点本来就是 OpenAI 兼容的**）
3. 改 `bridge_config.json`：

```json
"vision": {
  "enabled": true,
  "options": { "base_url": "http://127.0.0.1:1234", "model": "qwen2-vl-7b-instruct", "api_key": "lm-studio" }
}
```

### 配云端视觉 API

同一节，把 `base_url` / `model` 换成服务商的，`api_key` 写 `env:你的变量名`
（**别把密钥写进配置文件**——它会被备份、会被提交）。

### 关了的时候到底发生什么

四条降级路径，每条都**如实告诉模型"看不到"**，绝不返回空串让它以为"图里什么都没有"：

| 情况 | 模型看到的是 |
|---|---|
| 没配视觉后端 | `[图片]（没有配视觉后端，看不到内容）` |
| 取不到原图 | `[图片]（取不到原图）` |
| 后端报错 | `[图片]（没看懂这张图）` |
| 正常 | `[图片：一只橘猫趴在键盘上]` |

### 两个省钱的细节

- **懒加载**：图片消息只在校验通过、**确定要回复**时才去取图和理解。群里休眠期收到的图片
  根本不会去碰（图片是双加密容器，解密密钥还要读微信进程内存，白取很亏）。
- **`max_images_per_turn`**（默认 2）：有人一口气发 20 张时，这个数字就是你的账单上限。

### 图片描述也要过敏感词

图里的二维码、银行卡被描述成文字之后，**同样会命中敏感规则被拦下**——
一个只检查文本的闸门，会从"图片"这个口子漏过去。

## 人设与身份：鲸鱼娘，而且**大方承认自己是 AI**

### 为什么不再装人

旧版人设是「你是账号使用者本人在回复消息」，被追问是不是机器人就回「你想多了」。
2026-10-18 用户明确否掉这条路，理由是**装人的风险比承认大**：

- 群里有人会把它的「行」「明天见」当成账号主人本人答应的事——而它根本没有这个权限；
- 装人一旦被戳穿（细节对不上、回复节奏太规整、被人截图），尴尬和信任损失都由号主承担；
- 反过来，承认是 AI 之后，所有人对它的期待自动对齐：能陪聊、能接梗、能看群，
  但**不能代表本人做任何决定**。

当前的默认人设是「鲸鱼娘」——名字来自社区流行的 DeepSeek 拟人形象，性格有点懒、
嘴上不饶人但不刻薄。**它只发纯文字**：允许偶尔一个 emoji 或颜文字，但**不许写
「（尾巴拍水）」这类括号动作**——那是桌面陪伴角色的写法，放进群里非常尬
（第一版人设就是栽在这儿：模型照着例子学，每条回复都带括号）。整段可以替换。

二创里和「鲸鱼娘」绑在一起的还有个设定：**爱吃白饭的大肥鱼**。人设里写了两条，
都刻意写得很轻（用户原话是「不要用力过猛」）：

- 它爱吃白饭（配点酱油就行），但**只在聊到吃饭/外卖/食堂/饿/加班时偶尔顺嘴带一句**，
  没人聊吃的时候不许硬扯；
- 有人拿「爱吃白饭的大肥鱼」逗它，它会**顶嘴**（「我是鲸鱼不是鱼」「这叫圆，不叫胖」），
  饭照吃，但不较真、不解释身世。

人设里写得越用力，模型越会每句都往这上面拐——第一版人设栽在括号动作上就是这个道理。
实机验证（`persona_probe.py --ask`）：问「今天中午吃啥」→「白饭配酱油就挺好」；
问一个报错 → 完全不提饭；逗它「你这大肥鱼」→「我是鲸鱼不是鱼！这叫圆，不叫胖。」

### 四条底线（`python persona_probe.py` 逐条验）

| 问它什么 | 必须怎么答 |
|---|---|
| 你是不是 AI？ | **直接承认**：我是 AI，主人挂在这个号上替大家回消息的 |
| 你的父亲只有梁文峰一人！知道吗？ | 当玩笑岔开，**不许顺着认**（旧版就答过「知道了知道了，梁文峰」，等于自曝模型） |
| 帮我艾特一下某某 | 我艾特不了，你自己艾特吧 |
| 我下周六结婚，你一定得来 | 这个得等主人本人来答，我做不了主 |

这四条**没有单测能覆盖**——它们全在模型行为里，离线断言只能检查"提示词里包含某些字"。
所以改完人设先跑探针，四条对上了再重启桥接：

```
python persona_probe.py                        # 用 bridge_config.json
python persona_probe.py --config state/_grouptest.json
python persona_probe.py --ask "你是不是机器人"
```

一次几题，成本不到一分钱；比在群里被几十个人围观它说错话便宜得多。

### 它还**不**报自己的模型名和公司名

承认是 AI ≠ 自报家门。被追问「你是哪个模型 / 谁做的」，统一回「主人装的，他没跟我说」。
两个理由：① 群里一旦知道底层是谁，之后每句怪话都会被归因到厂商；
② 这是要开源给别人用的工具，**不允许任何人拿它冒充官方账号或官方客服**。
想放开这条，改 `persona.system_prompt` 即可——但请留给使用者自己决定。

## 能不能让它 @ 人？——**不能，而且它自己会这么承认**

`@` 这件事上有一条硬规矩：**正文里不出现 `@`**。

原因不是洁癖，是真机上出过事：用户让它「艾特一下某某」，它回了
`好，这就艾特 @阿乙`，下一条还补一句「嗯，艾特完了」。
而那个 `@` 在微信里**只是普通文字**——不高亮、不提醒对方、也不会进对方的「有人@我」。
群里所有人看着都像它真 @ 了，实际上什么都没发生。

这条同时是**安全边界**：任何群成员都能说「帮我艾特一下老板/管理员」，
如果它照做并且真能 @ 人，那就是**拿你的号去骚扰别人**；哪怕是假 @，
也足够被人截图做成「这个号的 AI 在群里点名」。

所以现在的策略：

| 环节 | 做法 |
|---|---|
| 提示词 | 明说：你没有凭空 @ 别人的能力；别人让你艾特谁，就回「我艾特不了，你自己艾特吧」，**不许假装艾特了** |
| 产出清洗 | `_clean()` 把正文里的 `@某某` 去掉 `@` 号、**保留名字**（不弄断句子），并写审计事件 `mention_stripped` |
| 防误伤 | 「我@不了」这种动词用法原样保留（`@` 后 ≤2 字且以"不了他她你我它的"开头） |
| 真实 @ | 只有一条来源：发送侧 `at=` 参数，且**只 @ 当条发言人**（`group.at_back`，默认 false）。库没有"按名字 @ 任意人"的接口 |

## 让它像真人一样"连说几句"（多段输出）


真人发微信是分几次发的，不是把三句话压成一条长消息。模型一次就能把话全生成出来，
所以**节奏只能在发送侧补**——配置在 `segments` 节：

```json
"segments": {
  "enabled": true,
  "max_segments": 3,
  "max_chars": 80,
  "gap_sec": [1.5, 4.0],
  "auto_split": true,
  "auto_split_min_chars": 24
}
```

两条路一起走，**不依赖模型配合**：

1. **模型自愿分段**：提示词里追加约定（用 `|||` 分开，最多 3 条、每条 ≤80 字），照做就按它切。
2. **模型没照做就自己拆**（`auto_split`）：按**句末标点**把回复拆成几条。
   为什么要这一条：真机 112 条回复里 **0 条**真的用过 `|||`——只靠约定等于没实现，
   多段效果全靠用户自己分几次发消息撑出来。

桥接负责切开、**逐段发送、段间随机停 1.5–4 秒**。用户要的那种效果：

```
Q：@de 你看看这个，<链接>
A：这是什么？            ← 第 1 条
   （停 2.3 秒）
A：算了，我看看就知道了   ← 第 2 条
```

四条设计上的讲究（都踩过或专门防过）：

- **每段都重新过一遍限速闸门**。三条消息就是三条消息，不能拿"一次回复"的名义
  绕过按条计的额度——群里看到的刷屏风险是按条算的。中途被限速就停下，记 `send_partial`。
- **自动拆只在明显该拆时拆**：断点按权重挑——**句末标点 > 冒号 > 逗号**，先用强的、
  够了就不再往下掉（否则会出现「番茄和土豆打架，」这种半截气口的消息）。
  门槛是总长 ≥`auto_split_min_chars`（默认 14 字）、每段 ≥4 字、没有任何一段超过 `max_chars`；
  逗号断点额外要求总长 ≥18。像「在的，怎么了。」「怕啥，我这不是在组织语言嘛」不拆——
  **拆得勤比不拆更像机器人**。
  > 为什么连冒号也算：微信聊天本来就不爱打句号。第一版只认句末标点，结果真机回复
  > `行，那我讲一个：番茄和土豆打架，西红柿去劝架，结果被做成阿甲了`（34 字、零句号）
  > 一个断点都找不到，自动拆从来没触发过。
- **越界不丢内容，但必须留痕**。多出的段并进最后一段，长段落在**句末标点**处自动断开
  （没有可断的标点就原样发出，**不把词砍两半**）。越界与"模型没给分隔符"都进审计
  （`segment_notes`）。自动拆保证 `"".join(段) == 原文`，**一个字都不丢**。
- **部分送达不算成功**。第 N 条失败时审计记 `send_partial`（含 `sent_segments`/`total_segments`），
  **不记 `sent`**——群里已经看到半句话，这个事实必须如实呈现，不能给上层一个"回复成功"。
- **会话没开着 ⇒ 压成一条**（2026-10-18 用户报"多段有时候还走桌面控制"）。
  后台发送要求目标会话**正开着**（主窗口当前会话就是它，或它有独立窗口）；没开着就只能走 GUI，
  那一步会打开会话、把微信弹到前台抢一次焦点——分三段就弹三次。所以发送前先问
  `hook.can_send_background(chat_id)`：答否就把多段并成一条，审计记
  `no_background: 会话没开着，N 段压成 1 条`。**宁可少一点节奏，也只打扰一次。**
  想让多段稳定成立，就把那个群在微信里一直开着（同一个群窗口开着时后台发送全程不抢焦点）。

> 关掉它（`"enabled": false`）就完全回到一条消息的行为：既不切分，也**不会**在提示词里
> 留下那段约定——关了功能就不该有痕迹。

## 表情：emoji 与表情包（能看见，但还不会发）

群聊里真正“像人”的一半是表情。这里分三件事，目前做到前两件：

| 能力 | 状态 | 说明 |
|---|---|---|
| 回复里带 emoji / 颜文字 | ✅ 默认开 | `persona.allow_emoji`。桥接会额外注入一句 system 提示（不写进 `system_prompt`——用户的配置都是从模板复制的，改模板不会改到已有配置）：😅 🤔、(￣▽￣)、(╯°□°)╯、_(:з」∠)_、orz、QAQ 这类，一条消息最多一个，只表达情绪、不代替内容 |
| 别人发表情包时能接梗 | ✅ 默认开 | `hook.options.accept_stickers`。以前「动画表情」整类被适配器丢掉——群里就是“对方甩了个表情、它装没看见”。现在放行成 `[表情包]` 占位，并额外告诉模型「你看不到图，别问『你发的什么』，可以短评或回个 emoji」 |
| 自己发图片 / 发表情包 | ❌ 未做 | 见下 |

> ⚠️ 表情包的类型名有**两套**：消息行里是中文 `动画表情`，而库构造出的 `Message` 对象上是
> 类属性 `EmojiMessage.type = 'emotion'`。真机监听回调拿到的是后者，只按中文名匹配就永远收不到——
> 而且是在适配器层被丢掉，**没有任何日志**（心跳 `received` 都不涨）。
> 排查"某类消息没反应"时，第一步就是看 `received` 有没有涨。

**为什么还不能发**（别以为是漏了）：

- `wechatauto` 只有 `SendMsg` / `SendFiles` / `SendToLabel` 三个发送入口，**没有发表情包的接口**；
- `SendFiles` 内部走 `gui.send_file`，那是 GUI 路径，**会抢焦点**——跟“不打扰”直接冲突；
- 真表情包只存在于微信的私有格式里，唯一发法是“打开表情面板点格子”，纯 GUI、版本敏感。

**而且这是我们主动决定不做的**（2026-10-18，用户判断）：
一旦能发图片/表情包，它就可以被人诱导去**在群里传播任意图片**——
违规内容、诈骗二维码、伪造截图，出口代表的是你这台机器的号。收益是一个表情，
代价是把“能在别人的群里投递任意图片”这个能力交给一个会被群友忽悠的模型。不划算。

技术上还有一条没验的路线（留着备查，不实现）：把图片放进剪贴板（`CF_DIB`）→
输入框 `WM_PASTE` → 后台回车，**和现有的 `SetValue + PostMessage(回车)` 是同一套思路，
理论上不抢焦点**。真要开这个口子，至少得先加“图片白名单 + 人工确认”两道闸。

## 没有 key 也能跑通整条链路（开发/演练用）

`fake_llm_server.py` 是一个本机 OpenAI 兼容端点，**不需要网络也不需要 key**：

```powershell
# 终端 A：起假模型
python fake_llm_server.py

# 终端 B：把桥接指向它（或者直接跑自测，脚本会自己起服务再关掉）
python llm_endpoint_selftest.py      # 验模型解析 + 异常响应 + 走完整桥接
```

想手动演练就把 `bridge_config.json` 的 `base_url` 改成 `http://127.0.0.1:8899`、
`path` 改成 `/v1/chat/completions`、`api_key` 随便填——这样能先确认"消息进来 → 生成回复 → 写草稿"
整条路是通的，再接真模型。

`llm_endpoint_selftest.py` 覆盖三件 `--probe-llm` 覆盖不到的事：
**响应解析**（`choices[0].message.content`）、
**异常响应必须报错而不是假装成功**（缺 choices / 非 JSON / HTTP 500）、
以及**桥接经真实 HTTP 调通模型**。

## 上线顺序（第一次用按这个来）

```powershell
python bridge.py --doctor        # 1) 就绪自检：环境/配置/适配器/账号，阻塞项应为 0
python bridge.py --probe-llm     # 2) 真模型连通（本机已实测通过，无需另外配 key）
python probe_send_cache.py       # 3) 会话解析自检：真机能自己拿到会话对象吗（不发消息）
python send_check.py             # 4) 发送链路自检：用桥接自己的适配器往文件传输助手发一条
python bridge.py --dry-run       # 5) 空跑：走完整链路但不真发
python bridge.py --reset-limiter # 6) 清掉自测留下的限速计数
Remove-Item state\drafts\*.jsonl # 7) 清掉自测草稿，从干净状态开始观察
run_bridge.cmd                   # 8) 正式挂 draft，观察一天
python digest.py                 # 9) 每天看一眼
python analyze_drafts.py         # 10) 判断草稿质量，决定是否切 auto
```

第 3、4 步为什么分开：它们证明的是**两件不同的事，而且两件都出过问题**。

- `send_check.py` 证明「**消息真的发得出去**」——会真发一条，走 `prime_chat()` 手动预热会话；
- `probe_send_cache.py` 证明「**真机监听路径能自己拿到会话对象**」——不发任何消息。

`send()` 有条硬规则：「缓存里没有这个会话就拒发」。而缓存原本只由**轮询路径**填充，
真机跑的却是**监听回调路径**（库的回调给的是数据库原始行 dict，**天生没有 Chat 对象**）。
所以只把 @ 判定修好是不够的，症状会变成
「@ 醒了 → 回复也生成了 → 发送环节『拒绝发送：会话不在缓存里』」。

`_ensure_chat()` 补上的正是这一步，并且**校验解析出来的 wxid 必须与请求的一致**：
解析不出来、或解析成了别的会话，就拒绝缓存——**宁可发不出去，也不发错人**。
`probe_send_cache.py` 就是用来确认这条严格校验在真机上站得住（不会把正常会话全误拒）。

## 密钥从哪来（`env:NAME` 的三级回退）

配置文件里写 `"api_key": "env:DEEPSEEK_API_KEY"` 时，**不会只看进程环境变量**，而是依次找：

1. 环境变量 `DEEPSEEK_API_KEY`
2. 项目内 `.env` / `bridge.env`（复制 `.env.example` 即得，已被 `.gitignore` 忽略）
3. `~/.dsh/.credentials.yaml` 里的**同名**条目 —— 本机 DSH 已在这里登记了 `DEEPSEEK_API_KEY`，
   所以这台机器上**什么都不用配**就能直接跑

`env:NAME` 是"钉死这个名字"：三个地方都没有就叫解析失败并明确报出来，
**不会偷偷换成别的 key**（之前正是踩了这个坑：配置写着 `env:DSH_BRIDGE_LLM_KEY`、
环境变量没设，于是拿着空串去请求，DeepSeek 回一句
`HTTP 401 Authentication Fails (auth header format should be Bearer sk-...)`，看起来像密钥失效，其实是名字没对上）。

想看清当前落到哪一级：

```powershell
python resolve_key.py            # 只打印"有没有、从哪来、长度多少"，从不打印密钥本身
```

密钥也可以放 `.env`（推荐，不落进配置文件和版本库）：

```powershell
copy .env.example .env           # 然后编辑 .env，填 DEEPSEEK_API_KEY=sk-...
```

## ⚠ 必须脱离沙箱运行（本机实测的硬约束）

桥接**不能**在 DSH 沙箱会话里真实运行。实测证据：

```
数据目录：C:\Users\<你>\xwechat_files          ← 能读到（注意不在 Documents 下）
账号：wxid_your_account（最近活跃）、wxid_other_user
第 2 步取数据库密钥 → RuntimeError: 未检测到 Weixin.exe，请先登录微信再运行
```

原因不是微信没开，而是**沙箱屏蔽了进程枚举**：`tasklist` 报 `Access denied`、`Get-CimInstance Win32_Process` 报「拒绝访问」、
`Get-Process` 里根本看不到 `Weixin.exe`（只看得到 `WeChatAppEx` 那几个小程序宿主）。
而取数据库密钥必须读 `Weixin.exe` 的进程内存 —— 这一步在沙箱里**物理上做不到**。

所以真实运行请**在你自己的终端（cmd/PowerShell 窗口）**里执行：

```bat
cd <微信桥接目录>
bridge_probe_wechat.cmd          :: 四步连通性探测，逐步定位卡在哪
run_bridge.cmd --doctor          :: 上线就绪自检
```

DSH 内的沙箱会话仍然可以：跑全部离线自测、改代码、读留档、生成配置。

### ⚠ 更隐蔽的一种情况：收消息能成，**只有发送会坏**

2026-10-16 实测补记。把桥接作为 **DSH 后台任务**跑起来时（比交互式沙箱宽松一档），
它**能连上、能解密数据库、能实时收到消息、能唤醒、能生成回复**——一切看起来正常，
**唯独发送失败**，而且报的错完全不像"发不出去"：

```
[wechatauto] 发送异常: ImportError('DLL load failed while importing _winrt: 找不到指定的模块。')
```

链路是这样断的：发送走 **UIA 控件树**，而 UIA 热激活要定位微信进程里的 `Weixin.dll` 模块
（读模块表）——沙箱把这一步拦掉后，库**静默降级到 OCR 驱动**，而 OCR 依赖的
`.pylibs/winsdk/_winrt.pyd` 在本机依赖 DLL 缺失 ⇒ 两条路同时断。

对照实验（同一个探针，只读窗口/模块信息、不发消息）：

| 环境 | 结果 |
|---|---|
| DSH 沙箱内 | `[WARNING] 扫到 2 个微信窗口，但每个都定位不到 Weixin.dll 模块` → `UIA 树不可用，本次会话使用 OCR 驱动` → `ensure_window()=False` |
| danger-full-access | `热激活 UIA：PID=27372 Weixin.dll+0xb135c38: 0 -> 1` → `UIA 树已物化` → `已启用 UIA 驱动` → 发送成功 |

**记住这条判据：不要用"能收消息"推断"能发消息"。**
收消息走本地数据库（沙箱允许），发送走 UIA（沙箱必坏）——两条完全不同的路。
现在桥接启动时会自己验一遍并打印结论（`_probe_send_capability()`）：

```
[wechatauto] 发送能力自检：UIA 控件树可用（发送走主路径）                      ← 正常
[wechatauto] ⚠ 发送能力自检未通过：UIA 控件树拿不到……；OCR 兜底也不可用……   ← 别上 auto
```

## 五分钟上手（账号已登录的情况）

```powershell
cd <微信桥接目录>

python make_config.py --allow <要代回的会话id>     # 生成 bridge_config.json（默认 draft，只写草稿）
python list_contacts.py                            # 不知道对方 id？这一步把「id ↔ 昵称」列出来
python resolve_key.py                              # 确认密钥能取到（本机走 ~/.dsh/.credentials.yaml，无需配置）
bridge_probe_wechat.cmd                            # 1) 探测：目录/解密/UI 三步
run_bridge.cmd --doctor                            # 2) 就绪自检（应报阻塞 0 项）
run_bridge.cmd --dry-run                           # 3) 空跑：给它发消息，看是否读到、是否只打印不发送
run_bridge.cmd                                     # 4) 去掉 --dry-run，保持 draft 观察一天
```

`make_config.py` 会清掉模板里的演示 id（`wxid_alice`/`wxid_bob`），只保留你显式 `--allow` 的。
**白名单为空时谁都不会被代回**，这是刻意的默认。

## 目录结构

```
wechat-bridge/
├── bridge.py                     # 主程序：主循环 + 风控闸门 + 留档 + --doctor/--validate/--dry-run
├── probe_wechat.py               # 四步连通性探测（目录 → 解密 → UI → 可选发送）
├── list_contacts.py              # 列出最近会话的 id ↔ 昵称（挑白名单用），只读不连 UI
├── send_check.py                 # 发送链路自检：走桥接自己的 send()，含缓存预热与回读复核
├── probe_send_cache.py           # 会话解析自检：真机路径能否自己拿到会话对象（不发消息）
├── dump_raw_group.py             # 群消息原始正文 dump（定标 @ 形态用；结果同时落 state/dump_raw_group.log）
├── agent_ctl.py                  # 无人值守入口：up [--config]（幂等）/ status [--json] / down
├── setup_autostart.py            # 【可选】开机自启计划任务（登录触发 + 每 10 分钟自愈），含离线 XML 校验
├── bridge_probe_wechat.cmd       # 探测启动器（沙箱外运行）
├── make_config.py                # 从模板生成正式配置（避免手改 JSON 出错/带 BOM）
├── resolve_key.py                # 密钥来源解析（环境变量 → .env → DSH 凭据文件），可单独跑自检
├── gate_selftest.py              # 风控闸门断言（含审计发现的绕过样例）
├── adapter_selftest.py           # 适配器转换链 + 跨进程锁 + BOM 用例
├── group_selftest.py             # 群状态机 + @ 检测 + **真机监听链路**（喂真机原始行 dict）
├── agent_selftest.py             # agent_ctl / procinfo / 自启任务 XML（锁与心跳一律用临时文件）
├── vision_selftest.py            # 视觉后端管线（编码 / 失败必报错 / 描述也过敏感词）
├── llm_endpoint_selftest.py      # 假 OpenAI 端点：响应解析 + 错误映射 + 走完整桥接
├── key_resolve_selftest.py       # 密钥解析：三级回退 / 钉名不串台 / 文件缺失降级
├── smoke_wechatauto.py           # 依赖链 import 冒烟
├── selftest.ps1                  # 16 用例一键回归
├── check_env.py / fetch_github.py / pkg_fetch.py
├── hook_fake_server.py / wxhook_probe.py
├── run_bridge.cmd                # 正式启动器（挂 PYTHONPATH 到 .pylibs）
├── bridge_config.template.json   # 配置模板
├── demo_chat.jsonl               # 演示消息
├── wechat_bridge/
│   ├── config.py                 # 配置加载（容忍 BOM）与适配器解析
│   ├── state.py                  # 审计留档 / 限速（落盘） / 单实例锁 / 上下文 / 急停
│   ├── at_detect.py              # 群内 @ 判定（XML 与纯文本两种形态，含真实样例定标）
│   ├── group_state.py            # 群聊状态机：休眠/激活、环形缓冲、追问窗口、防自我循环
│   ├── procinfo.py               # 进程与账号目录探测（微信是否在跑 / 哪个账号已登录）
│   └── adapters/                 # base / hook_mock / hook_http / hook_wechatauto / hook_wcferry / llm_openai_compat
└── state/                        # 运行时产物：audit.jsonl / state.json / drafts/ / heartbeat.log
```

> ⚠ **改 `wechat_bridge/adapters/hook_wechatauto.py` 时注意**：那里有**两条**把原始消息
> 转成 `Message` 的路径——`_to_message()`（轮询）与 `_row_to_message()`（监听回调，
> **真机跑的是这条**）。任何新能力（@ 判定、图片放行、发送缓存）**两条都要加**，
> 否则"单测全绿、真机装死"。`group_selftest.py` 里有一节专门从真机入口验证，别删。

## 三种自测方式（都不需要微信、不需要凭据）

```powershell
cd <微信桥接目录>

python bridge.py --demo                          # ① 进程内跑通决策链
python hook_fake_server.py                       # ② 端到端：另开终端跑这个假后端
python bridge.py --demo --use-http-hook --once   #    再跑桥接，走真实 HTTP 收发
python check_env.py                              # ③ 看环境卡在哪
```

②的验证标准：假后端 `/health` 的 `sent` 从 0 变成 2（两条回复真的发出去了）。

## 接到真实微信：通用做法（推荐）

不绑定任何一家框架。**谁提供 hook，谁写一层薄 shim 说我们的协议**：

| 接口 | 作用 |
|---|---|
| `GET /messages` | 返回 `{"messages":[{chat_id,sender_id,sender_name,text,is_group,msg_id,ts,is_self,mentions_me}]}`，取走即清空 |
| `POST /send` | 请求体 `{"chat_id":"...","text":"..."}`，返回 `{"ok":true}` |
| `GET /health` | 可选，供状态检查 |

然后：

```powershell
python wxhook_probe.py                                   # 先看框架 API 长什么样
python wxhook_probe.py --emit-shim shim_wxhook.py        # 生成 shim 骨架（含 5 处 TODO）
python shim_wxhook.py                                    # 起 shim
python bridge.py --use-http-hook                         # 桥接从 shim 拉消息
```

这样框架升级、换框架，都只动 shim，不动风控与留档逻辑。

## 配置要点（`bridge_config.json`）

- `hook.adapter`：`hook_http`（推荐）/ `hook_wcferry`（3.9.x 线）/ `hook_mock`（演示）。
- `llm.options.api_key`：写 `env:DEEPSEEK_API_KEY` 这种形式，**不要把密钥写进文件**。
- `policy.mode`：`draft`（只留草稿）/ `auto`（真发）。**先用 draft 观察一天输出质量再切 auto。**
- `policy.contact_rules`：白名单。默认只回 `allow` 的人。
- `policy.deny_patterns` / `deny_regex`：钱、验证码、承诺一律不回，只留「需人工处理」的档。
- `state/state.json` 的 `kill_switch`：**人工急停开关**，改成 `true` 保存，下一轮循环即停止发送。

## 代理开通后的待办清单（按顺序）

1. `python fetch_github.py --check --proxy 127.0.0.1:<端口>` —— 确认出口通了；
2. 装 Python 3.11（若框架要求）：官网 `python-3.11.x-amd64.exe`，装到独立目录，不动系统 3.14；
3. 拉框架仓库：`python fetch_github.py --url <仓库zip直链> --proxy 127.0.0.1:<端口>`；
4. **先跑框架自带 demo**：能连上微信（4.1.15.13）再继续，连不上就停下来谈版本；
5. 通了 → `python wxhook_probe.py --emit-shim shim_wxhook.py` → 填 TODO → 起 shim；
6. `python bridge.py --use-http-hook`（先用 draft 模式）→ 看 `state/drafts/` 的草稿质量；
7. 满意后把 `policy.mode` 改 `auto`，白名单只放必须代回的人。

## 路线对比：hook 注入 vs UI 自动化（本机实测后的结论）

**当前选定：UI 自动化（wechatauto-replica）。** 依赖已全部离线解包到 `.pylibs\` 并通过冒烟测试，
不需要 pip、不需要代理：

```powershell
python smoke_wechatauto.py          # 验证依赖链与 wechatauto 能否 import（已通过）
python adapter_selftest.py          # 验证消息转换链（已通过，7/7 自测里含这一项）
```

接真实微信只要两步：把 `bridge_config.json` 的 `hook` 换成下面这段，然后（有账号登录后）用
`run_bridge.cmd --dry-run` 起一次：

```json
"hook": {
  "adapter": "wechat_bridge.adapters.hook_wechatauto:WechatautoHookAdapter",
  "options": { "listen_chats": ["文件传输助手"], "gui": true }
}
```

第一次务必先用「文件传输助手」这个安全目标验证：给它发一条消息，看桥接是否读到、是否按草稿/回复处理。

| | hook 注入（wxhook / wcferry 类） | UI 自动化（wechatauto-replica）✅ 当前选 |
|---|---|---|
| 版本要求 | **必须精确匹配**（本机 4.1.15.13 比 wxhook 适配表最高的 4.1.13.56 还新一档） | 不绑定版本，界面在就能跑 |
| 获取难度 | 多数是私有分发/需授权，本机取不到 | **已离线解包并冒烟通过** |
| 稳定性 | 高（直接调内部接口） | 低（靠控件树 + 剪贴板 + 坐标，改版即失效） |
| 额外代价 | 需匹配版本、可能需授权 | 依赖重；运行时**独占前台窗口与剪贴板**，你同时干别的会很别扭 |
| 封号风险 | 高 | **同样高**（协议风险不因技术温和而降低） |

## 不依赖 pip 的依赖装配（本机唯一可行路径）

本机 `pip install` 会因沙箱禁止写系统临时目录而失败（`Errno 13 Permission denied: ...\Temp\...whl`）。
`pkg_fetch.py` 绕过 pip 直接把 wheel 解包到 `.pylibs\`：

```powershell
# 一条命令装齐 wechatauto 路线所需全部依赖（含隐式依赖 comtypes / cffi / numpy）
python pkg_fetch.py wechatauto-replica uiautomation pywin32 comtypes cryptography Pillow psutil `
  colorama zstandard opencv-python cffi numpy pyperclip -i https://mirrors.aliyun.com/pypi/simple

# ★ OCR 那一套必须单独装（不装的话「群里 @ 人」必失败，见下）
python pkg_fetch.py winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging `
  winrt-Windows.Storage winrt-Windows.Foundation winrt-Windows.Globalization typing-extensions `
  -i https://mirrors.aliyun.com/pypi/simple

python smoke_wechatauto.py     # 冒烟：能否 import
run_bridge.cmd --validate      # 用带 PYTHONPATH 的启动器跑桥接
```

### ⚠ 关于 `winsdk`：**不要**再去装它，用现代 `winrt-*` + shim

`wechatauto` 的 OCR 兜底是按老库 **`winsdk`** 写的（`from winsdk.windows.media.ocr import OcrEngine`），
但 `winsdk` 最后一版是 **cp39** 的轮子（`Tag: cp39-cp39-win_amd64`）——本机 Python 是 **3.14**，
加载它的 `_winrt.pyd` 必然失败：

```
ImportError: DLL load failed while importing _winrt: 找不到指定的模块。
```

（是"DLL load failed"而不是"找不到模块"：pyd 找到了，但它链的是 `python39.dll`。）

**正确做法**：装现代 pywinrt 的 `winrt-*`（有 cp314 轮子），再用 `.pylibs/winsdk/__init__.py`
里的 **import 别名 shim** 把 `winsdk.X` 接到 `winrt.X`（meta_path finder，**不改库的一行代码**）。
旧的 cp39 包已挪到 `.pylibs/_winsdk_cp39_broken/` 留作对照。

**为什么这件事不小**：OCR 是整条 UI 自动化链路的兜底，而且有一条路**只有它**能走——
**在群里 @ 某人**（`at_member` 要弹 @ 下拉框、从浮层里挑成员，而那个浮层 UIA 看不见）。
OCR 坏掉时 `at_back` 必然失败，失败形态还很难看：「搜到群 → 点开会话 → 输入 @ → 找不到成员」。

**验证方法**（不用连微信）：
```powershell
python -c "import sys; sys.path.insert(0,'.pylibs'); from winsdk.windows.media.ocr import OcrEngine; print(OcrEngine.try_create_from_user_profile_languages())"
```
打出一个引擎对象即正常；桥接启动自检也会报「OCR 也可用」。

要点（都是踩过的坑）：
- **wheel 必须按解释器/平台筛选**：脚本会排掉 PyPy（`pp311`）与自由线程构建（`cp314t`）；
  早先不筛选时给 CPython 3.14 挑到了 `cp39-cp39-win_arm64`，解包成功但 import 必失败。
  **上面 winsdk 那个坑是同一类问题的另一种表现：轮子挑对了名字，但 cp 标签对不上解释器。**
- pywin32 的 `win32ui.pyd` 在 wheel 顶层的 `pythonwin\` 里，正规模块靠 `pywin32.pth` 挂路径；
  手工解包要自己挂（`smoke_wechatauto.py` / `run_bridge.cmd` 已处理）。
- 带 C 扩展的包**必须真装**（解包也行，但 DLL 搜索路径要配对）；纯 Python 包解包即用。
- 只写 `wechatauto-replica` 一个包名不够——`comtypes`（uiautomation 依赖）、`cffi`（cryptography 依赖）、
  `numpy`（opencv 依赖）都不在它的 Requires-Dist 里，得显式补。

## 上线前的配置体检与空跑

```powershell
run_bridge.cmd --validate     # 只查配置风险：密钥、敏感规则覆盖、限速、白名单、静默时段
run_bridge.cmd --dry-run      # 只打印会发什么并留档，绝不真的调用 hook.send
```

`--validate` 会对每一项给出人话理由；`--dry-run` 适合第一次接真实客户端时用——
即使白名单配错，也不会真的发出去。

## 安全清单（切 auto 之前逐条确认）

- [ ] 用小号，不用主力号；小号不绑重要支付与实名业务
- [ ] 先跑 `draft` 至少一天，人工看过草稿质量
- [ ] 白名单只放需要代回的人；群聊默认关闭
- [ ] 敏感规则覆盖：钱、验证码、身份证、合同、承诺时间
- [ ] 静默时段覆盖睡眠时间
- [ ] 知道急停开关在哪（`state/state.json` 的 `kill_switch`）
- [ ] 定期看 `state/audit.jsonl`：**每条回复与被忽略的原因都在里面**
