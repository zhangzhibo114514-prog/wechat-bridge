"""API 密钥解析。

**本脚本不打印任何密钥**，只打印"每个来源有没有 key"。
用法：python resolve_key.py          # 自检：看看这台机器上能从哪儿找到 key
      python -c "import resolve_key" # 嵌入使用

来源优先级（先命中先用）：
    1. 真实环境变量
    2. 项目内 .env / bridge.env（不覆盖已存在的环境变量）
    3. DSH 凭据文件 ~/.dsh/.credentials.yaml
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

KEY_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 顶层或缩进的 `NAME: value`（YAML 标量行）；列表项 '-' 开头不匹配
YAML_SCALAR_PATTERN = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*)?$")
BLOB_MARKERS = ("|", ">", "|-", ">-", "|+", ">+")


def load_env_file(path: Path) -> dict[str, str]:
    """读一个极简 .env 文件（KEY=VALUE，支持 # 注释与引号）。不覆盖已有环境变量。"""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not KEY_NAME_PATTERN.match(name):
            continue
        loaded[name] = value
        os.environ.setdefault(name, value)          # 已设置的环境变量优先
    return loaded


ENV_FILE_NAMES = (".env", "bridge.env")


# 按优先级尝试的变量名：先本项目的、再常见的服务商变量名
CANDIDATE_NAMES = ("DSH_BRIDGE_LLM_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

# DSH 自己的凭据文件（宿主写入，里面通常已经有可用的 DEEPSEEK_API_KEY）。
# 这里只**按路径读取**，不把密钥抄进 .env —— 避免 key 轮换后留下一份过期副本。
DEFAULT_CREDENTIALS_PATH = Path.home() / ".dsh" / ".credentials.yaml"


def credentials_path() -> Path:
    """凭据文件位置，可用 DSH_CREDENTIALS_FILE 覆盖（便于自测）。"""
    override = os.environ.get("DSH_CREDENTIALS_FILE", "").strip()
    return Path(override) if override else DEFAULT_CREDENTIALS_PATH


def load_credentials_file(path: Path) -> dict[str, str]:
    """从 DSH 凭据文件里挑出可用的 KEY: VALUE 标量。

    只做行级解析：`NAME: value`，值非空且不是块标量标记。优先取顶层（无缩进）的定义，
    嵌套字段只作为兜底。文件不存在 / 读不动一律返回空字典——凭据文件不该阻断启动。
    """
    top: dict[str, str] = {}
    nested: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return top
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = YAML_SCALAR_PATTERN.match(raw)
        if not match:
            continue
        indent, name, value = match.group(1), match.group(2), (match.group(3) or "").strip()
        value = value.strip('"').strip("'")
        if not value or value in BLOB_MARKERS:
            continue
        (nested if indent else top).setdefault(name, value)
    for name, value in nested.items():
        top.setdefault(name, value)
    return top


def resolve_named(name: str) -> tuple[str, str]:
    """按名字取一个密钥：先环境变量，再 DSH 凭据文件里的**同名**条目。

    刻意只在同名条目里找——写 `env:DEEPSEEK_API_KEY` 就是钉死这一个名字，
    不会因为环境变量没设就偷偷改用别的 key。
    """
    value = os.environ.get(name, "")
    if value:
        return value, f"环境变量 {name}"
    path = credentials_path()
    value = load_credentials_file(path).get(name, "")
    if value:
        return value, f"DSH 凭据文件 {path} 中的 {name}"
    return "", f"{name}（环境变量与凭据文件里都没有）"


def resolve_api_key(configured: str, project_root: Path) -> tuple[str, str]:
    """把配置里的 api_key 解析成真正的密钥。

    支持三种写法：
        "env:NAME"   → 只认这一个名字（缺了就报错，别偷偷换别的 key）
        空字符串      → 依次尝试 环境变量 → .env → DSH 凭据文件
        直接写值      → 原样使用（不推荐，密钥会落在配置文件里）

    返回 (密钥, 来源说明)；密钥为空时调用方应当拒绝启动。
    """
    for name in ENV_FILE_NAMES:
        loaded = load_env_file(project_root / name)
        if loaded:
            names = ", ".join(loaded)
            print(f"[密钥] 已从 {name} 载入 {len(loaded)} 个变量（{names}）", file=sys.stderr)

    configured = (configured or "").strip()
    if configured.startswith("env:"):
        return resolve_named(configured[4:])
    if configured:
        return configured, "配置文件内明文（建议改用 env: 形式）"
    for name in CANDIDATE_NAMES:
        value = os.environ.get(name, "")
        if value:
            return value, f"自动发现的环境变量 {name}"
    path = credentials_path()
    credentials = load_credentials_file(path)
    for name in CANDIDATE_NAMES:
        value = credentials.get(name, "")
        if value:
            return value, f"DSH 凭据文件 {path} 中的 {name}"
    return "", "未设置"


def find_key(project_root: Optional[Path] = None) -> tuple[str, str]:
    """便捷入口：不读配置，直接从环境变量/.env/DSH 凭据文件里找一个可用密钥。"""
    root = project_root or Path(__file__).resolve().parent
    return resolve_api_key("", root)


def _mask(value: str) -> str:
    return f"已设置(len={len(value)})" if value else "-"


def _main() -> int:
    root = Path(__file__).resolve().parent
    credentials = load_credentials_file(credentials_path())

    print("[来源自检]（只显示是否设置，不显示内容）")
    for name in ENV_FILE_NAMES:
        print(f"  env 文件 {name:<12} {'存在' if (root / name).exists() else '不存在'}")
    print(f"  凭据文件 {credentials_path()} {'存在' if credentials_path().exists() else '不存在'}")
    print()
    print(f"  {'变量名':<22}{'环境变量':<20}{'凭据文件':<20}")
    for name in CANDIDATE_NAMES:
        print(f"  {name:<22}{_mask(os.environ.get(name, '')):<20}{_mask(credentials.get(name, '')):<20}")

    key, source = find_key(root)
    print()
    for name in ("DEEPSEEK_API_KEY", "DSH_BRIDGE_LLM_KEY"):
        pinned, pinned_source = resolve_named(name)
        state = f"可用（{pinned_source}）" if pinned else "不可用"
        print(f"  env:{name:<18} → {state}")
    print()
    if key:
        print(f"==> 最终选中：{source}（长度 {len(key)}）")
        print("    下一步：python bridge.py --probe-llm")
        return 0
    print("==> 未找到任何可用密钥。三种填法任选其一：")
    print("    A. 跑 python set_key.py，按提示粘贴（输入不回显、不落 shell 历史）")
    print("    B. 复制 .env.example 为 .env，填 DEEPSEEK_API_KEY=")
    print("    C. setx DEEPSEEK_API_KEY \"<key>\"（新开终端生效）")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
