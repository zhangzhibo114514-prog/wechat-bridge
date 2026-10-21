"""OpenAI 兼容的 chat/completions 客户端（标准库实现，无第三方依赖）。

适用：DeepSeek、OpenAI、以及各类 OpenAI 兼容网关；本地 LM Studio 也走这里
（base_url 填 http://127.0.0.1:1234/v1，path 填 /chat/completions，api_key 填任意非空字符串）。
api_key 支持 "env:NAME" 形式从环境变量读取，避免把密钥写进配置文件。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import LLMClient


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        path: str = "/v1/chat/completions",
        temperature: float = 0.7,
        timeout_sec: float = 60.0,
        **_,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path if path.startswith("/") else "/" + path
        self.api_key = self._resolve(api_key)
        self.model = model
        self.temperature = temperature
        self.timeout_sec = timeout_sec

    @staticmethod
    def _resolve(value: str) -> str:
        """空值 → 自动发现一个可用密钥；`env:NAME` → 钉死这个名字。

        为什么要自动发现：本机常见的变量名不止一个（DSH_BRIDGE_LLM_KEY / DEEPSEEK_API_KEY …），
        用户设了 DEEPSEEK_API_KEY 但配置里写着 env:DSH_BRIDGE_LLM_KEY 是最容易踩的坑。
        显式写 `env:NAME` 时**只认这一个名字**（不会偷偷换成别的 key），但这个名字既可以来自
        环境变量、也可以来自 ~/.dsh/.credentials.yaml（本机 DSH 已在那里登记了 DEEPSEEK_API_KEY）。
        """
        value = (value or "").strip()
        try:
            import sys
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from resolve_key import find_key, resolve_named
        except Exception:
            # 退化成最朴素的行为：env: 只读环境变量，其余原样使用
            return os.environ.get(value[4:], "") if value.startswith("env:") else value

        if value.startswith("env:"):
            key, source = resolve_named(value[4:])
            if key:
                print(f"[密钥] {source}")
            else:
                print(f"[密钥] 配置钉在 {source}，当前取不到值")
            return key
        if value:
            return value
        key, source = find_key()
        if key:
            print(f"[密钥] 自动使用：{source}")
        return key

    def complete(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url=self.base_url + self.path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"LLM 调用失败: {exc!r}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM 返回无 choices: {str(data)[:300]}")
        return ((choices[0].get("message") or {}).get("content") or "").strip()
