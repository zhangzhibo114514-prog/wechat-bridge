"""密钥解析自测：离线、确定性、不碰真机凭据。

为什么要有这一步：`api_key` 写 `env:NAME` 时，取不到值的表现是**静默拿到空串**，
最后以一句 `HTTP 401 (auth header format should be Bearer sk-...)` 收场——看着像密钥失效，
其实是"名字没对上"。本脚本用 DSH_CREDENTIALS_FILE 指到临时凭据文件，把各条解析规则钉死在测试里：
    1. env:NAME  → ①环境变量 ②项目内 .env ③~/.dsh/.credentials.yaml 的同名条目
    2. 钉名不串台：钉了 DEEPSEEK_API_KEY 就不会偷偷改用别的 key
    3. 环境变量优先级高于凭据文件
    4. 文件缺失 / 嵌套字段 / 块标量 都不能把解析弄崩或误取

跑法：python key_resolve_selftest.py      # 全程本地，不联网、不读真实凭据
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import resolve_key                                                            # noqa: E402
from wechat_bridge.adapters.llm_openai_compat import OpenAICompatClient       # noqa: E402

SANDBOX = ROOT / "state" / "_keyselftest"
FAKE_KEY = "sk-selftest-not-a-real-key-000000"
CANDIDATE_NAMES = resolve_key.CANDIDATE_NAMES


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return ok


def main() -> int:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    creds = SANDBOX / "credentials.yaml"
    missing = SANDBOX / "does-not-exist.yaml"

    # 临时凭据文件：故意混入嵌套记录、块标量与无关条目，看解析会不会误取
    creds.write_text(
        "version: 1\n"
        "records:\n"
        "  some-grant:\n"
        "    kind: grant\n"
        "    payload:\n"
        "      token: this-is-a-session-token-not-an-api-key\n"
        "refs:\n"
        "  DEEPSEEK_API_KEY: " + FAKE_KEY + "\n"
        "  OPENAI_API_KEY: ''\n"
        "  BLOCK_SCALAR_KEY: |\n"
        "    should-be-skipped\n",
        encoding="utf-8",
    )

    saved_env = {name: os.environ.pop(name, None) for name in CANDIDATE_NAMES}
    saved_creds = os.environ.get("DSH_CREDENTIALS_FILE")
    ok = True
    try:
        os.environ["DSH_CREDENTIALS_FILE"] = str(creds)

        print("-- 凭据文件解析 --")
        parsed = resolve_key.load_credentials_file(creds)
        ok &= check("取到目标条目", parsed.get("DEEPSEEK_API_KEY") == FAKE_KEY)
        ok &= check("嵌套条目也要收（真机上 DEEPSEEK_API_KEY 就在 refs: 缩进层里）",
                    parsed.get("kind") == "grant")
        ok &= check("块标量被跳过", "BLOCK_SCALAR_KEY" not in parsed)
        ok &= check("空值不入选", "OPENAI_API_KEY" not in parsed)

        # 自动发现只认白名单里的名字：文件里那些 token/secret 永远不会被当成 API key
        only_token = SANDBOX / "only-token.yaml"
        only_token.write_text("records:\n  grant:\n    payload:\n"
                              "      token: session-token-not-an-api-key\n", encoding="utf-8")
        os.environ["DSH_CREDENTIALS_FILE"] = str(only_token)
        key, source = resolve_key.find_key(ROOT)
        ok &= check("自动发现不会把会话 token 当成 API key", key == "", source)
        os.environ["DSH_CREDENTIALS_FILE"] = str(creds)

        print("\n-- env:NAME 的三级回退 --")
        os.environ["DEEPSEEK_API_KEY"] = "sk-from-real-env"
        key, source = resolve_key.resolve_named("DEEPSEEK_API_KEY")
        ok &= check("环境变量优先于凭据文件", key == "sk-from-real-env", source)
        del os.environ["DEEPSEEK_API_KEY"]

        key, source = resolve_key.resolve_named("DEEPSEEK_API_KEY")
        ok &= check("环境变量没有则回落到凭据文件", key == FAKE_KEY, source)

        key, source = resolve_key.resolve_named("DSH_BRIDGE_LLM_KEY")
        ok &= check("钉名不串台：另一个名字取不到就是空", key == "", source)

        key, source = resolve_key.resolve_api_key("env:DSH_BRIDGE_LLM_KEY", ROOT)
        ok &= check("配置写错名字时明确报出「取不到」，不是静默给空串",
                    key == "" and "DSH_BRIDGE_LLM_KEY" in source, source)

        key, source = resolve_key.resolve_api_key("env:DEEPSEEK_API_KEY", ROOT)
        ok &= check("配置写对名字时命中", key == FAKE_KEY, source)

        print("\n-- 自动发现（api_key 留空） --")
        key, source = resolve_key.find_key(ROOT)
        ok &= check("留空也能找到密钥", key == FAKE_KEY, source)

        print("\n-- 适配器侧（真正发请求那条路） --")
        ok &= check("OpenAICompatClient 钉名命中",
                    OpenAICompatClient._resolve("env:DEEPSEEK_API_KEY") == FAKE_KEY)
        ok &= check("OpenAICompatClient 钉名落空返回空串",
                    OpenAICompatClient._resolve("env:NO_SUCH_KEY") == "")
        ok &= check("明文 key 原样使用",
                    OpenAICompatClient._resolve("sk-plain-text") == "sk-plain-text")

        print("\n-- 降级：文件不存在也不能崩 --")
        os.environ["DSH_CREDENTIALS_FILE"] = str(missing)
        key, source = resolve_key.resolve_named("DEEPSEEK_API_KEY")
        ok &= check("凭据文件缺失 → 返回空且不抛异常", key == "", source)

        print(f"\n结论：{'密钥解析自测通过' if ok else '存在失败项'}")
        return 0 if ok else 1
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if saved_creds is None:
            os.environ.pop("DSH_CREDENTIALS_FILE", None)
        else:
            os.environ["DSH_CREDENTIALS_FILE"] = saved_creds
        for path in (creds, missing, SANDBOX / "only-token.yaml"):
            if path.exists():
                path.unlink()
        try:
            SANDBOX.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
