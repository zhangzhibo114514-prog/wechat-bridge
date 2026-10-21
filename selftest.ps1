$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:TZ = "Asia/Shanghai"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 自定位：作为文件直接跑时切到本目录；被写成 scriptblock 内联执行时 $PSCommandPath 为空，
# 此时不作处理（调用方已经把 cwd 放在项目目录，这是 selftest 的既定跑法）。
if ($PSCommandPath) { Set-Location -LiteralPath (Split-Path -Parent $PSCommandPath) }

$results = @()

# 自测必须是**封闭**的：
# 1) demo 用例现在永远走内置演示策略（--demo 不读真实配置），所以不用再挪配置文件；
# 2) 但限速窗口与熔断状态是落盘持久化的（这是特性，防重启绕过限速），
#    真实运行留下的计数会把 demo 用例判成"限速拦截"，所以开跑前清一次。
python bridge.py --reset-limiter > "state\resetlimiter.log" 2>&1
Write-Output ("限速状态已复位：" + (Get-Content "state\resetlimiter.log" -Encoding UTF8 | Select-Object -First 1))
# 演示/回归用的是独立的 state\_demo\，它自己的限速窗口也要清：
# 否则连跑两次就会撞上"每分钟上限 6"，表现为 in-process 用例莫名其妙变红。
Remove-Item "state\_demo\state.json" -ErrorAction SilentlyContinue

Write-Output "########## 1/17 in-process decision chain ##########"
# ⚠ 演示/回归的产物写在 state\_demo\ 里，**不碰真实的 state\audit.jsonl**。
# 旧写法会 Remove-Item 真实留档再指望 --demo 往里写 —— 等于每跑一次回归就删掉一份真实历史。
Remove-Item "state\_demo\audit.jsonl" -ErrorAction SilentlyContinue
# 限速额度是**跨重启续命**的（state.json 里存着上一分钟的发送记录）。
# 不清它，一次跑到一半就会"触发全局限速"，回归结果取决于上一次跑剩多少额度 —— 不确定性。
Remove-Item "state\_demo\state.json" -ErrorAction SilentlyContinue
$out = python bridge.py --demo 2>&1
$code = $LASTEXITCODE
Write-Output ("bridge output lines: " + ($out | Measure-Object).Count)
$sent = (Get-Content "state\_demo\audit.jsonl" -Encoding UTF8 | Select-String '"event": "sent"').Count
Write-Output "demo audit sent records: $sent"
$results += [pscustomobject]@{ Case = "in-process"; Code = $code; Pass = ($code -eq 0 -and $sent -eq 2) }

Write-Output ""
Write-Output "########## 2/17 HTTP end-to-end ##########"
Remove-Item "state\_demo\audit.jsonl" -ErrorAction SilentlyContinue
# 限速额度是**跨重启续命**的（state.json 里存着上一分钟的发送记录）。
# 不清它，一次跑到一半就会"触发全局限速"，回归结果取决于上一次跑剩多少额度 —— 不确定性。
Remove-Item "state\_demo\state.json" -ErrorAction SilentlyContinue
# 用作业方式起假后端：在 Start-Job 环境里也稳定（Start-Process 重定向在作业里会失效）
$serverJob = Start-Job -ScriptBlock { param($r) Set-Location $r; python hook_fake_server.py } -ArgumentList $PWD.Path
$httpPass = $false
$before = "n/a"; $after = "n/a"; $code2 = -1
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 500
  $probe = python -c "import socket;s=socket.socket();print(s.connect_ex(('127.0.0.1',5555)))" 2>$null
  if ($probe -eq "0") { break }
}
if ($probe -eq "0") {
  $before = python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:5555/health',timeout=5))['sent'])" 2>&1
  python bridge.py --demo --use-http-hook --once > "state\bridge_run.log" 2>&1
  $code2 = $LASTEXITCODE
  $after = python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:5555/health',timeout=5))['sent'])" 2>&1
  $httpPass = ($code2 -eq 0 -and [int]$after -ge 2)
} else {
  Write-Output "fake hook server did not become ready in 15s"
}
Stop-Job $serverJob -ErrorAction SilentlyContinue
Remove-Job $serverJob -Force -ErrorAction SilentlyContinue
Write-Output "backend sent counter: before=$before after=$after bridgeExit=$code2"
$results += [pscustomobject]@{ Case = "http-e2e"; Code = $code2; Pass = $httpPass }

Write-Output ""
Write-Output "########## 3/17 environment self-check ##########"
python check_env.py > "state\check.log" 2>&1
$code3 = $LASTEXITCODE
Get-Content "state\check.log" -Encoding UTF8 | Select-String "4.1.15.13|适配表|解释器|pypi.org:443"
$results += [pscustomobject]@{ Case = "env-check"; Code = $code3; Pass = ($code3 -eq 0) }

Write-Output ""
Write-Output "########## 4/17 syntax check ##########"
# ⚠ 新增 .py 文件后**记得加进这个列表**，否则它写错语法也不会被测出来
python -m py_compile bridge.py pkg_fetch.py adapter_selftest.py gate_selftest.py analyze_drafts.py setup_autostart.py probe_wechat.py make_config.py smoke_wechatauto.py check_env.py fetch_github.py hook_fake_server.py wxhook_probe.py resolve_key.py list_contacts.py send_check.py digest.py nightly_stop.py llm_endpoint_selftest.py key_resolve_selftest.py fake_llm_server.py group_selftest.py agent_selftest.py agent_ctl.py dump_raw_group.py vision_selftest.py segment_selftest.py bg_selftest.py debounce_selftest.py long_memory_selftest.py proactive_selftest.py speak_gate_selftest.py panel_selftest.py panel/panel_server.py probe_vision_endpoints.py vision_check.py probe_bg_uia.py probe_image.py wechat_bridge/long_memory.py wechat_bridge/adapters/vision_openai_compat.py wechat_bridge/config.py wechat_bridge/state.py wechat_bridge/procinfo.py wechat_bridge/at_detect.py wechat_bridge/group_state.py wechat_bridge/speak_gate.py wechat_bridge/segments.py wechat_bridge/bg_send.py wechat_bridge/imagefmt.py wechat_bridge/ffmpeg.py wechat_bridge/adapters/base.py wechat_bridge/adapters/hook_mock.py wechat_bridge/adapters/hook_wcferry.py wechat_bridge/adapters/hook_http.py wechat_bridge/adapters/hook_wechatauto.py wechat_bridge/adapters/llm_openai_compat.py 2>&1
$code4 = $LASTEXITCODE
$results += [pscustomobject]@{ Case = "syntax"; Code = $code4; Pass = ($code4 -eq 0) }

Write-Output ""
Write-Output "########## 5/17 config validation ##########"
# ⚠ 这里**绝不能**覆盖真实的 bridge_config.json。
# 旧写法是 `Copy-Item 模板 bridge_config.json`，而脚本末尾并不会还原 ——
# 于是一次回归就把用户配好的 hook（→ 假适配器）、mode（→ auto）、白名单（→ 演示 id）、
# 静默时段全部冲成模板默认值，而且不报任何错。已改为写到临时文件 + 用 --config 指定。
Copy-Item "bridge_config.template.json" "state\_selftest_config.json" -Force -ErrorAction SilentlyContinue
# 同时把密钥来源掐断（指向不存在的凭据文件），这样"模板缺密钥"这件事是**确定性**的，
# 不依赖跑测试的机器上恰好有没有可用密钥。
$savedCredFile = $env:DSH_CREDENTIALS_FILE
$env:DSH_CREDENTIALS_FILE = Join-Path $PWD.Path "state\_no_such_credentials.yaml"
python bridge.py --config "state\_selftest_config.json" --validate > "state\validate.log" 2>&1
$code5 = $LASTEXITCODE
if ($null -eq $savedCredFile) { Remove-Item Env:\DSH_CREDENTIALS_FILE -ErrorAction SilentlyContinue }
else { $env:DSH_CREDENTIALS_FILE = $savedCredFile }
Remove-Item "state\_selftest_config.json" -ErrorAction SilentlyContinue
Get-Content "state\validate.log" -Encoding UTF8 | Select-Object -First 6
# 模板没有可用密钥 => validate 应报问题并返回 1
$results += [pscustomobject]@{ Case = "validate"; Code = $code5; Pass = ($code5 -eq 1) }

Write-Output ""
Write-Output "########## 6/17 dry-run must not send ##########"
$serverJob2 = Start-Job -ScriptBlock { param($r) Set-Location $r; python hook_fake_server.py } -ArgumentList $PWD.Path
$dryPass = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 500
  $probe2 = python -c "import socket;s=socket.socket();print(s.connect_ex(('127.0.0.1',5555)))" 2>$null
  if ($probe2 -eq "0") { break }
}
if ($probe2 -eq "0") {
  python bridge.py --demo --use-http-hook --once --dry-run > "state\dryrun.log" 2>&1
  $code6 = $LASTEXITCODE
  $sentAfter = python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:5555/health',timeout=5))['sent'])" 2>&1
  Write-Output "dry-run exit=$code6 backendSent=$sentAfter (expect 0)"
  $dryPass = ($code6 -eq 0 -and [int]$sentAfter -eq 0)
} else {
  $code6 = -1
  Write-Output "fake hook server did not become ready"
}
Stop-Job $serverJob2 -ErrorAction SilentlyContinue
Remove-Job $serverJob2 -Force -ErrorAction SilentlyContinue
$results += [pscustomobject]@{ Case = "dry-run"; Code = $code6; Pass = $dryPass }

Write-Output ""
Write-Output "########## 7/17 wechatauto adapter offline ##########"
python adapter_selftest.py > "state\adapterselftest.log" 2>&1
$code7 = $LASTEXITCODE
Get-Content "state\adapterselftest.log" -Encoding UTF8 | Select-Object -Last 3
$results += [pscustomobject]@{ Case = "adapter"; Code = $code7; Pass = ($code7 -eq 0) }

Write-Output ""
Write-Output "########## 8/17 doctor (readiness) ##########"
# doctor 把"阻塞项数量"当退出码：当前必然 > 0（账号未登录 / 无正式配置）。
# 这里只验证它**能跑且能给结论**，不要求 0；等 B 阶段账号就绪后再要求退出码 0。
python bridge.py --doctor --no-connect > "state\doctor.log" 2>&1
$code8 = $LASTEXITCODE
Get-Content "state\doctor.log" -Encoding UTF8 | Select-String "结论：" | ForEach-Object { $_.Line }
$doctorRan = (Get-Content "state\doctor.log" -Encoding UTF8 | Select-String "结论：阻塞").Count -eq 1
$results += [pscustomobject]@{ Case = "doctor"; Code = $code8; Pass = $doctorRan }

Write-Output ""
Write-Output "########## 9/17 gate self-test (audit bypass cases) ##########"
python gate_selftest.py > "state\gateselftest.log" 2>&1
$code9 = $LASTEXITCODE
Get-Content "state\gateselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "gate"; Code = $code9; Pass = ($code9 -eq 0) }

Write-Output ""
Write-Output "########## 10/17 draft analyzer self-test ##########"
python analyze_drafts.py --selftest > "state\analyzer_selftest.log" 2>&1
$code10 = $LASTEXITCODE
Get-Content "state\analyzer_selftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "analyzer"; Code = $code10; Pass = ($code10 -eq 0) }

Write-Output ""
Write-Output "########## 11/17 daily digest ##########"
# 日报只要**能跑且能给结论**即可。读的是演示留档（真实留档不该被测试写入或读取）
python digest.py --audit "state\_demo\audit.jsonl" > "state\digest.log" 2>&1
$code11 = $LASTEXITCODE
Get-Content "state\digest.log" -Encoding UTF8 | Select-Object -First 3
$digestOk = (Get-Content "state\digest.log" -Encoding UTF8 | Select-String "共 \d+ 条记录").Count -ge 1
$results += [pscustomobject]@{ Case = "digest"; Code = $code11; Pass = $digestOk }

Write-Output ""
Write-Output "########## 12/17 LLM endpoint (fake OpenAI-compatible server) ##########"
python llm_endpoint_selftest.py > "state\llmendpoint.log" 2>&1
$code12 = $LASTEXITCODE
Get-Content "state\llmendpoint.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "llm-http"; Code = $code12; Pass = ($code12 -eq 0) }

Write-Output ""
Write-Output "########## 13/17 key resolution (env:NAME -> credentials file) ##########"
python key_resolve_selftest.py > "state\keyresolve.log" 2>&1
$code13 = $LASTEXITCODE
Get-Content "state\keyresolve.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "key-resolve"; Code = $code13; Pass = ($code13 -eq 0) }

Write-Output ""
Write-Output "########## 14/17 group state machine + @ detection ##########"
# 纯离线：不需要微信、不需要网络、不需要凭据。
# 这两块决定"什么时候花钱调用模型"，规则错了的症状是"该回的不回/不该回的乱回"，
# 上线后极难现场调试，所以必须在离线阶段钉死。
python group_selftest.py > "state\groupselftest.log" 2>&1
$code14 = $LASTEXITCODE
Get-Content "state\groupselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "group"; Code = $code14; Pass = ($code14 -eq 0) }

Write-Output ""
Write-Output "########## 15/17 unattended entry (agent_ctl up/status + autostart XML) ##########"
# 重点是**幂等性**：开机自启任务每 10 分钟调一次 up，不幂等就会起出第二个桥接进程，
# 而多个桥接挂同一个号 = 同一条消息被回复多次。全程离线，不碰微信、不改真实状态。
python agent_selftest.py > "state\agentselftest.log" 2>&1
$code15 = $LASTEXITCODE
Get-Content "state\agentselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "unattended"; Code = $code15; Pass = ($code15 -eq 0) }

Write-Output ""
Write-Output "########## 16/17 vision pipeline (image understanding) ##########"
# 重点：①异常响应必须报错而不是假装成功 ②降级路径必须**如实说"看不到"**
# （绝不能让模型以为"图里什么都没有"）③图片描述要再过一遍敏感词。
python vision_selftest.py > "state\visionselftest.log" 2>&1
$code16 = $LASTEXITCODE
Get-Content "state\visionselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "vision"; Code = $code16; Pass = ($code16 -eq 0) }

Write-Output ""
Write-Output "########## 17/17 multi-segment output (human-like pacing) ##########"
# 重点：①一次响应真的被拆成多条发出 ②**部分失败绝不许记成 sent**
# （群里已经看到半句话，审计必须如实呈现）③越界时不丢内容、但必须留痕。
python segment_selftest.py > "state\segmentselftest.log" 2>&1
$code17 = $LASTEXITCODE
Get-Content "state\segmentselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "segments"; Code = $code17; Pass = ($code17 -eq 0) }

Write-Output ""
Write-Output "########## 18/18 background send (no focus stealing / no mouse) ##########"
# 重点：①输入框里有草稿时**绝不覆盖**（那是用户没发出去的话）
# ②读不到输入框内容时也不许赌"它是空的" ③回车没生效要判失败并清干净。
python bg_selftest.py > "state\bgselftest.log" 2>&1
$code18 = $LASTEXITCODE
Get-Content "state\bgselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "bg-send"; Code = $code18; Pass = ($code18 -eq 0) }

Write-Output ""
Write-Output "########## 19/19 reply debounce (KI-2: one person sends 3 -> reply once) ##########"
# 重点：①同一人连发要合并成**一次**模型调用 ②合并后内容不能丢
# ③窗口期别人说的话不能被并进来、更不能丢 ④关掉防抖时行为回到"一条一回"
python debounce_selftest.py > "state\debounceselftest.log" 2>&1
$code19 = $LASTEXITCODE
Get-Content "state\debounceselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "debounce"; Code = $code19; Pass = ($code19 -eq 0) }

Write-Output ""
Write-Output "########## 20/20 long memory (记住 / 忘掉 / 按相关度召回 / 模型摘要) ##########"
# 重点：①存进去能按相关性取出来 ②会话隔离 ③重启不丢 ④能删
# ⑤显式"记住…"不花模型钱 ⑥摘要输出能解析 ⑦提示词里真的带上了记忆
python long_memory_selftest.py > "state\longmemoryselftest.log" 2>&1
$code20 = $LASTEXITCODE
Get-Content "state\longmemoryselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "long-memory"; Code = $code20; Pass = ($code20 -eq 0) }

Write-Output ""
Write-Output "########## 21/21 proactive (情景交融：看见全场 -> 自己决定要不要插话) ##########"
# 重点：①没被 @ 的群消息也进上下文 ②冷场才考虑 ③模型可以说 <SILENT>
# ④开口时不 @ 人 ⑤小时预算与冷却期生效 ⑥@别人的消息不安排
python proactive_selftest.py > "state\proactiveselftest.log" 2>&1
$code21 = $LASTEXITCODE
Get-Content "state\proactiveselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "proactive"; Code = $code21; Pass = ($code21 -eq 0) }

Write-Output ""
Write-Output "########## 22/22 speak gate (该不该接这一句) ##########"
# 重点：①@我 / 引用我的照常回（不许把该说的憋回去）②@别人 / 引用别人 / 只是应了一声
# / 别人在说话 ⇒ 不说，且审计留 reason 码 ③静音的消息仍然进上下文、仍然安排"冷场再开口"
# ④mode=model 时判定报错/超预算一律按"不说"处理。
python speak_gate_selftest.py > "state\speakgateselftest.log" 2>&1
$code22 = $LASTEXITCODE
Get-Content "state\speakgateselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "speak-gate"; Code = $code22; Pass = ($code22 -eq 0) }

Write-Output ""
Write-Output "########## 23/23 panel (本机配置面板) ##########"
# 重点：①不带 token 的请求被拒 ②状态里不带密钥 ③白名单外的键改不动 ④非法值被拒且文件不动
# ⑤合法开关落盘且留备份 ⑥人设卡幂等 ⑦折腾完配置仍能解析。
# 注意：这里只起本地 HTTP 服务、不碰微信，也不碰真实配置（用的是一份拷贝）。
python panel_selftest.py > "state\panelselftest.log" 2>&1
$code23 = $LASTEXITCODE
Get-Content "state\panelselftest.log" -Encoding UTF8 | Select-Object -Last 2
$results += [pscustomobject]@{ Case = "panel"; Code = $code23; Pass = ($code23 -eq 0) }

Write-Output ""
Write-Output "########## summary ##########"
$results | Format-Table -AutoSize
# ⚠ 别写回 `($results | Where-Object {...}).Count`：一个都没匹配上时它返回空（不是 0），
# `总数 - $null` 仍然等于总数 —— 于是"恰好一个用例失败"会被打印成 "passed 17 / 17" 并以 0 退出。
# 必须用 @() 兜成数组、再转 int。
$failed = [int](@($results | Where-Object { $_.Pass -ne $true }).Count)
$passed = [int]$results.Count - $failed
Write-Output "passed $passed / $($results.Count)"
if ($failed -gt 0) { exit 1 } else { exit 0 }
