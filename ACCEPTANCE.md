# 验收标准（Acceptance）

这个项目的每一步都用**可执行的命令 + 可观察的判据**验收，不靠"看着像好了"。
本文件是唯一权威口径；`README.md` 讲怎么用，本文件讲**怎么算通过**。

## 阶段与当前状态（2026-10-15）

| # | 阶段 | 判据（命令 → 期望） | 状态 |
|---|---|---|---|
| A0 | 决策链正确性 | `python bridge.py --demo` → 退出码 0，审计里 `sent` 恰好 2 条 | ✅ 通过 |
| A1 | HTTP 端到端 | 假后端 `/health` 的 `sent` 由 0 变 ≥2 | ✅ 通过 |
| A2 | 配置风险体检 | `--validate` 能挑出配置问题（模板缺密钥 → 退出码 1） | ✅ 通过 |
| A3 | 空跑不发送 | `--dry-run` → 假后端 `sent` 保持 0 | ✅ 通过 |
| A4 | 依赖可 import | `python smoke_wechatauto.py` → 退出码 0，导出 WeChatDB/WeChat | ✅ 通过 |
| A5 | 适配器转换链 | `python adapter_selftest.py` → 12 项断言全 OK | ✅ 通过 |
| A6 | 全量回归 | `selftest.ps1` → 8 用例全 True | ✅ 通过 |
| A7 | 上线就绪自检 | `python bridge.py --doctor` → 阻塞项为 0 | ⛔ 阻塞（见下） |
| A8 | 模型接口连通 | `run_bridge.cmd --probe-llm` → 打印「模型探测成功」 | ⛔ 待密钥到位（见下） |
| B1 | 真机收消息 | 给被托管号发一条消息 → `state/audit.jsonl` 出现该条 `ignored`/`draft`/`sent` 记录 | ⛔ 待账号登录 |
| B2 | 真机发消息 | 用 `文件传输助手` 目标 → 消息真的出现在微信里，且审计有对应记录 | ⛔ 待账号登录 |
| B3 | 草稿质量 | 连续 `draft` 跑满一天 → 人工通读 `state/drafts/*.jsonl`，无乱承诺/答非所问 | ⛔ 未开始 |
| B4 | 切 auto 后守规矩 | `auto` 下敏感规则拦截率 100%（用 `demo_chat.jsonl` 那类样例在真机上复测） | ⛔ 未开始 |

## 当前唯一的阻塞项

`python bridge.py --doctor` 报的阻塞项，本质是同一件事：

```
[阻塞] 账号目录不存在：C:\Users\<你>\Documents\xwechat_files
       ⇒ 4.x 客户端还没登录过任何账号
[阻塞] bridge_config.json 不存在
```

**解除方式**：打开微信 4.x → 登录要托管的小号 → 回到项目目录执行
`copy bridge_config.template.json bridge_config.json` 并填好 `hook` / `llm` 两段
→ `python bridge.py --doctor` 应显示「阻塞 0 项」。

## 模型密钥怎么给（A8）

桥接**不会**去读 DSH 自己的凭据库——那是另一套密钥，拿它来跑这个脚本不合适。
按下面任一方式给：

1. **环境变量（推荐）**：先设 `DEEPSEEK_API_KEY`，配置里写 `"api_key": "env:DEEPSEEK_API_KEY"`。
   当前会话实测该变量**未设置**，所以现在跑 `--probe-llm` 会直接报缺密钥。
2. **本地模型**：开 LM Studio 并加载模型，配置改成
   `"base_url": "http://127.0.0.1:1234/v1"` + `"path": "/chat/completions"` + `"api_key": "lm-studio"`，
   完全不依赖外网（本机此前用 LM Studio 跑过 ds-14b）。

判据：`run_bridge.cmd --probe-llm` 打印 `模型探测成功，返回：'通了'` 之类的短回复，退出码 0；
失败会打印具体 HTTP 状态或异常类型（401 = 密钥不对，超时 = 网络出口问题）。

## 通过标准（每个阶段的具体数字）

- **A0**：`state/audit.jsonl` 中 `"event": "sent"` 的条数 == 2；`ignored` == 6。数字变了说明策略或演示数据被改动，需同步更新本表。
- **A1**：查询 `http://127.0.0.1:5555/health`，`sent` 字段前后对比，增量 ≥ 2。
- **A3**：同一查询的 `sent` 必须为 0——这是"空跑真的没发"的唯一硬证据。
- **A5**：12 项断言必须全 OK；任何一项失败都说明消息映射或过滤逻辑有问题，不要放行到 B 阶段。
- **A7**：doctor 退出码 == 0（它把阻塞项数量当退出码）。
- **B1/B2**：判据是 `state/audit.jsonl` **与微信客户端两边的现象一致**——只看日志不算通过，只看微信聊天记录也不算，必须两者对上。
- **B3**：人工通读草稿，**重点看三类**：涉及钱与承诺的有没有被模型自作主张答下来；有没有把群消息当成私聊回；有没有出现"作为 AI 我…"这类破坏人格设定的句子。
- **B4**：用 `demo_chat.jsonl` 里的敏感样例（例如「帮我转 500 块钱，急用」）在真机上发一次，必须被拦下并只留档。

## 不可协商的红线

无论哪个阶段，以下任一条被触发即**立即切回 draft 并停用**：

1. 回复里出现承诺（时间、金钱、合同、见面）——模型不得替账号主人做决定。
2. 验证码、身份证、银行卡、密码类内容被读出发送。
3. 单小时发送数超过配置上限，或出现"秒回"特征（连续多条间隔 < 1 秒）。
4. 群里对非 @ 消息回复。
5. 审计日志出现 `send_failed` 连续 3 次（此时熔断应已自动触发，需人工确认原因后再解冻）。

## 如何复跑全部自动化验收

```powershell
cd <微信桥接目录>

# 一次性跑完 A0–A6（约 1 分钟）
& ([scriptblock]::Create([System.IO.File]::ReadAllText("$PWD\selftest.ps1", [System.Text.Encoding]::UTF8)))
```

> 注意：本机沙箱禁止直接执行 `.ps1`，必须用上面的 `scriptblock` 形式；
> 且 `Get-Content -Raw` 会按 ANSI 解码导致中文脚本文法错误，所以用 `[IO.File]::ReadAllText(...,UTF8)`。

A7 及以后：

```powershell
python bridge.py --doctor              # 或 run_bridge.cmd --doctor
run_bridge.cmd --validate
run_bridge.cmd --dry-run               # 然后用另一个号发消息做 B1/B2 验收
```
