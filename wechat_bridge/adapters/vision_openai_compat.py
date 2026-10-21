"""OpenAI 兼容的**图片理解**客户端（本机 LM Studio 或云端 VL 模型都走这里）。

## 为什么需要单独一个后端

主模型 `deepseek-chat` **不支持图片输入**。所以"看懂群友发的图"这件事必须另找一个
能看图的模型——本机 LM Studio 跑个 7B 级 VL 模型是免费选项，云端 VL API 也行。

**默认是关的**（`vision.enabled=false`）。关着的时候 AI 只会看到「[图片]」占位，
能说"这是什么？"但**不会真的看懂**——这一点必须让用户知道，否则在群里会穿帮。

## 接口形状

和 `llm_openai_compat` 一样走标准 OpenAI chat/completions，只是 `content` 变成数组：

    {"role": "user", "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    ]}

LM Studio、vLLM、以及各家云端 VL 都认这个格式。

## 用法

    client = VisionCompatClient(base_url="http://127.0.0.1:1234",
                                model="qwen2-vl-7b-instruct", api_key="lm-studio")
    print(client.describe("C:/path/to/pic.png"))
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from ..imagefmt import describe_unknown, mime_of, sniff

# 单张图的大小上限。VL 模型对超大图既慢又没用（会自己缩放），
# 而 base64 之后体积还要涨 1/3，太大容易把请求撑爆。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

DEFAULT_PROMPT = (
    "用一两句中文描述这张图片：主体是什么、在做什么、有没有文字。"
    "不要客套话，不要 Markdown，直接说内容。"
)


class VisionCompatClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234",
        api_key: str = "lm-studio",
        model: str = "local-model",
        path: str = "/v1/chat/completions",
        timeout_sec: float = 60.0,
        max_tokens: int = 300,
        prompt: str = "",
        **_,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.path = path if path.startswith("/") else "/" + path
        self.api_key = self._resolve(api_key)
        self.model = model
        self.timeout_sec = timeout_sec
        self.max_tokens = max_tokens
        self.prompt = (prompt or DEFAULT_PROMPT).strip()

    @staticmethod
    def _resolve(value: str) -> str:
        """空值自动发现；`env:NAME` 钉死一个名字（与主模型同一套规则）。"""
        value = (value or "").strip()
        try:
            import sys
            from pathlib import Path as _P

            sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
            from resolve_key import find_key, resolve_named
        except Exception:
            return os.environ.get(value[4:], "") if value.startswith("env:") else value
        if value.startswith("env:"):
            key, _ = resolve_named(value[4:])
            return key
        if value:
            return value
        key, _ = find_key()
        return key

    # ---------------- 对外 ----------------

    @staticmethod
    def _encode_image(path: str | Path) -> tuple[str, str]:
        """把图片读成 data URL。返回 (data_url, mime)。

        **格式按文件头判定，不按扩展名**。理由见 `wechat_bridge/imagefmt.py` 的模块注释：
        真机上微信图片是 `.wxgf`（HEVC 容器），按扩展名猜成 `image/png` 只会换来一个
        远端 HTTP 400 unsupported image，日志里看不出是本地格式不对。
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise RuntimeError(f"图片不存在：{file_path}")
        size = file_path.stat().st_size
        if size <= 0:
            raise RuntimeError(f"图片是空文件：{file_path}")
        if size > MAX_IMAGE_BYTES:
            raise RuntimeError(f"图片太大（{size / 1048576:.1f} MB，上限 {MAX_IMAGE_BYTES / 1048576:.0f} MB）")
        raw = file_path.read_bytes()
        kind = sniff(raw)
        if kind is None:
            # 本地就拦住：不发、不猜，错误信息要能直接指向下一步动作。
            raise RuntimeError(
                f"不是模型能读的图片格式（{describe_unknown(raw)}）："
                f"路径={file_path.name}；"
                f"需要先转成 jpg/png/webp/gif（本机缺 ffmpeg 时配 hook.options.ffmpeg_exe，"
                f"或 `python pkg_fetch.py imageio-ffmpeg`）"
            )
        mime = mime_of(kind)
        data = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{data}", mime

    def describe(self, image_path: str | Path, prompt: Optional[str] = None) -> str:
        """描述一张图片。失败一律抛 `RuntimeError`（绝不返回空串假装成功）。"""
        data_url, _ = self._encode_image(image_path)
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": (prompt or self.prompt)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url=self.base_url + self.path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"视觉模型 HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"视觉模型调用失败: {exc!r}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"视觉模型返回无 choices: {str(data)[:300]}")
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise RuntimeError("视觉模型返回了空描述")
        return text


def make_client(config: Any) -> Optional["VisionCompatClient"]:
    """按配置造一个视觉客户端；没启用/造不出来就返回 None（调用方据此退回占位符）。"""
    if not getattr(config, "enabled", False):
        return None
    try:
        from ..config import resolve

        return resolve(config.adapter)(**dict(getattr(config, "options", {}) or {}))
    except Exception as exc:
        print(f"[视觉] 后端加载失败（将退回「[图片]」占位）：{exc!r}")
        return None
