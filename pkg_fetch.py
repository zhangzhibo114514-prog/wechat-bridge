"""不依赖 pip 的取包与解包工具 —— 应对本机沙箱禁止 pip 写临时目录的情况。

背景：本机执行 `pip install` 时，pip 要在系统临时目录解包，而 DSH 沙箱只允许写工作区，
于是报 `Errno 13 Permission denied: ...\\Temp\\...whl`。改用本脚本：
    1. 直接读 PyPI 镜像的 simple 索引（urllib，能通）；
    2. 挑选匹配当前 Python 版本与平台的 wheel（纯 Python 包最省事）；
    3. 下载到 `wheelhouse/`，再解压到目标目录（默认 `.pylibs/`）。

用法：
    python pkg_fetch.py wechatauto-replica                  # 下载 + 解压
    python pkg_fetch.py wechatauto-replica --no-extract     # 只下载
    python pkg_fetch.py wechatauto-replica --index https://pypi.tuna.tsinghua.edu.cn/simple

解压后的目录要加进 sys.path 才能 import（脚本会打印提示）。
"""

from __future__ import annotations

import argparse
import html.parser
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_INDEXES = [
    "https://mirrors.aliyun.com/pypi/simple",     # 实测响应最快（0.3s），放第一个
    "https://pypi.tuna.tsinghua.edu.cn/simple",   # 次选（首包约 22s）
    "https://pypi.org/simple",                    # 直连最慢（约 86s），兜底
]
UA = {"User-Agent": "Mozilla/5.0 (pkg_fetch)"}


class _LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.links.append(value)


def fetch_index(index: str, package: str, attempts: int = 2) -> list[str]:
    """PyPI 的 simple 索引路径要用**原始包名**（实测 aliyun 上 `wechatauto-replica` 有 51 个文件、
    而按 PEP 503 归一化后的 `wechatauto_replica` 反而 404），所以两种写法都试，命中即返回。

    注意：镜像偶发对同一路径返回 404（负载到尚未同步的后端），因此每个名字默认重试 2 次。
    """
    normalized = re.sub(r"[-_.]+", "-", package).lower()
    last_error: Exception | None = None
    for name in dict.fromkeys([package, normalized, normalized.replace("-", "_")]):
        url = f"{index.rstrip('/')}/{name}/"
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8", "replace")
            except Exception as exc:
                # 备选写法本来就该失败（例如归一化名在镜像上不存在），不算致命，继续试
                print(f"  [第{attempt}次失败] {url} -> {type(exc).__name__}: {exc}")
                last_error = exc
                continue
            parser = _LinkParser()
            parser.feed(body)
            candidates = list(parser.links)
            if not any(c.endswith((".whl", ".tar.gz")) for c in candidates):
                # HTMLParser 对个别镜像的畸形标签会漏抓，退化为正则直接在原文里找文件名
                candidates = re.findall(r'([^\s"\'<>]+\.(?:whl|tar\.gz))', body)
            # 关键：必须以**页面 URL（带尾斜杠）**为基准做 urljoin，
            # 否则相对链接 "../../packages/.." 会相对丢掉最后一段路径，拼出 404 的地址
            base = url if url.endswith("/") else url + "/"
            links = [urllib.parse.urljoin(base, c) for c in candidates if c.endswith((".whl", ".tar.gz"))]
            if links:
                print(f"  [索引命中] {url} -> {len(links)} 个文件")
                return links
            print(f"  [页面存在但无 wheel] {url}（{len(body)} 字节）")
            break
    if last_error:
        raise last_error
    return []


def wheel_score(name: str, py_tag: str) -> tuple[int, int] | None:
    """给 wheel 文件名打分；返回 None 表示与当前解释器/平台不兼容，必须跳过。

    踩过的坑：早先只按 "py3-none-any" / 版本标记的先后顺序挑，结果给 CPython 3.14 挑到了
    `pywin32-312-cp39-cp39-win_arm64.whl`、`pillow-...-pp311-pypy311_...`（PyPy 专用）——
    解包成功但 import 必失败。所以这里必须显式做兼容性判断。
    """
    lower = name.lower()
    if not lower.endswith(".whl"):
        return None
    if "pypy" in lower:
        return None                                    # PyPy 专用，CPython 用不了
    is_win = "win_amd64" in lower
    is_any = "none-any" in lower or "-any." in lower
    if not (is_win or is_any):
        return None                                    # 非本平台
    tags = lower[:-4].split("-")                       # 去掉 .whl，切标签；自由线程构建会形成 cp314.t-... 两段
    current = f"cp{sys.version_info.major}{sys.version_info.minor}"   # 本机: cp314
    free_threaded = any(t == f"{current}.t" or t == f"{current}t" for t in tags)
    if free_threaded:
        return None                                    # cp314t 自由线程构建，普通解释器装不了
    exact = f"{current}-{current}-" in lower
    this_version = exact or any(t == current or t.startswith(f"{current}.") for t in tags)
    if exact:
        return (4, 1)                                  # ABI 与解释器都精确匹配
    if this_version:
        return (4, 0)
    if "abi3" in lower and is_win:
        return (3, 0)                                  # 稳定 ABI（如 opencv 的 cp37-abi3），可跨 cp3x
    if is_any:
        return (2 if is_win else 1, 0)                 # 纯 Python 包
    # 平台特定的「纯 Python」包：代码是 py3 纯 Python，二进制（ffmpeg.exe 之类）直接塞在包里，
    # 例如 `imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl`。这类包的标签是 `py3-<abi>-<平台>`，
    # 既不是 `none-any` 也不是 `cp3x`，早先会被误判成"非本平台"而跳过——
    # 后果是 `.deps.txt` 里的 imageio-ffmpeg 永远装不上，wxgf（微信 HEVC 图片）转不了码。
    if is_win and ("py3" in tags or py_tag in tags):
        return (2, 0)
    if "cp3" in lower and is_win:
        return (0, 0)                                  # 其它 cp3x 版本，最后兜底（多半装不上）
    return None


def pick_wheel(links: list[str], py_tag: str = f"py{sys.version_info.major}{sys.version_info.minor}") -> str | None:
    """在候选中挑与当前解释器/平台兼容且版本最新的 wheel（索引通常旧版本在前）。"""
    scored: list[tuple[tuple[int, int], str]] = []
    for link in links:
        name = urllib.parse.unquote(Path(urllib.parse.urlparse(link).path).name)
        score = wheel_score(name, py_tag)
        if score is not None:
            scored.append((score, link))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    same = [link for score, link in scored if score == best]
    return same[-1]


def download(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    out = dest_dir / name
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp, out.open("wb") as fh:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)
    print(f"[下载] {name}  {out.stat().st_size / 1024:.1f} KB")
    return out


def extract(archive: Path, target: Path) -> None:
    """解包 wheel（zip）或源码包（tar.gz）。源码包只对纯 Python 项目有意义。"""
    target.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".whl"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target)
    else:
        import tarfile
        with tarfile.open(archive, "r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            _extract_tar_flat(tf, members, target)
    print(f"[解包] {archive.name} -> {target}")


def _extract_tar_flat(tf, members, target: Path) -> None:
    """把 sdist 里的包目录摊平到 target（去掉顶层 `pkg-1.0.0/` 前缀）。"""
    tops = {m.name.split("/")[0] for m in members}
    prefix = next(iter(tops)) + "/" if len(tops) == 1 else ""
    for m in members:
        rel = m.name[len(prefix):] if prefix and m.name.startswith(prefix) else m.name
        if not rel or rel.startswith(("..", "/")):
            continue
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        src = tf.extractfile(m)
        if src is not None:
            out.write_bytes(src.read())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="不依赖 pip 的取包工具")
    ap.add_argument("packages", nargs="+", help="包名（可多个）")
    ap.add_argument("--index", action="append", default=None,
                    help="PyPI 镜像，可重复指定；默认依次尝试 " + " , ".join(DEFAULT_INDEXES))
    ap.add_argument("--wheelhouse", default=str(ROOT / "wheelhouse"))
    ap.add_argument("--target", default=str(ROOT / ".pylibs"))
    ap.add_argument("--no-extract", action="store_true")
    args = ap.parse_args(argv)

    indexes = args.index or DEFAULT_INDEXES
    wheelhouse, target = Path(args.wheelhouse), Path(args.target)
    got_all = True
    for pkg in args.packages:
        links: list[str] = []
        for index in indexes:
            try:
                links = fetch_index(index, pkg)
            except Exception as exc:
                print(f"[试错] {pkg} @ {index}: {exc!r}")
                continue
            if links:
                print(f"[索引] {pkg} 命中 {index}（{len(links)} 个文件）")
                break
        if not links:
            print(f"[失败] {pkg}: 所有镜像都没有可用文件（可能是私有分发，或包名不对）")
            got_all = False
            continue
        # 索引页里同时存在两类 href：指向 /packages/... 的真实文件地址，和以文件名结尾的相对链接
        # （相对链接在镜像上会 404）。优先用前者，没有再多说。
        absolute = [l for l in links if "/packages/" in l]
        link = pick_wheel(absolute or links)
        if not link:
            # 没有可用 wheel：退而取源码包（纯 Python 包解包即可用；带扩展的装不了）
            sdist = next((l for l in reversed(links) if l.endswith(".tar.gz")), None)
            if not sdist:
                print(f"[跳过] {pkg}: 没有可用 wheel 也没有 sdist")
                got_all = False
                continue
            print(f"[提示] {pkg}: 只有源码包，按纯 Python 尝试解包（若是 C 扩展包则不可用）")
            link = sdist
        try:
            archive = download(link, wheelhouse)
            if not args.no_extract:
                extract(archive, target)
        except Exception as exc:
            print(f"[失败] {pkg}: {exc!r}")
            got_all = False

    if not args.no_extract:
        print(f"\n用法提示：import 前先把 {target} 加进路径：")
        print(f'  import sys; sys.path.insert(0, r"{target}")')
    return 0 if got_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
