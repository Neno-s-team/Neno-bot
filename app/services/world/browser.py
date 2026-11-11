"""她的浏览器（N2）：一个真的 Chrome，开在一块看不见的 Windows 桌面上，由 agent-browser 驱动。

- 窗口是真的（不是无头模式——X 之类的站直接拒绝无头浏览器），但开在另一块桌面上：用户的屏幕和任务栏上没有它。
- 不伪装：浏览器照常自报身份，"被程序控制"的标记（navigator.webdriver）不动。被网站拦了就是拦了。
- 用时才开、用完就关：开着一个重网页约 860MB，平时不占。
- 登录状态在 data/browser/neno。账号由用户手动注册 / 登录（scripts/open-neno-browser.ps1 开一个看得见的窗口）；
  那个窗口开着时（data/browser/neno.manual 存在）后台不启动——两个 Chrome 不能共用一份资料。
- agent-browser（vercel-labs，Apache-2.0）装在 data/tools/agent-browser（不入库）：
  `npm install agent-browser@0.42.0`；可用 NENO_AGENT_BROWSER 指到别处。
- 给模型看的不是截图，是 agent-browser 的"快照"（页面的文字和能点的东西，每个带编号）；condense 再压一遍。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from app.services.world.real import clean

ROOT = Path(__file__).resolve().parents[3]
PROFILE = ROOT / "data" / "browser" / "neno"
MANUAL = ROOT / "data" / "browser" / "neno.manual"
SESSION = "neno"
DESKTOP = "NenoSurf"
IDLE = "3m"   # 没人管它时自己关掉的时间（兜底：后端被杀也不会留一个浏览器）


class Unavailable(Exception):
    """浏览器现在用不了（没装、用户正开着她的窗口、起不来、页面打不开）。"""


def tool() -> Path:
    custom = os.getenv("NENO_AGENT_BROWSER", "").strip()
    return Path(custom) if custom else ROOT / "data/tools/agent-browser/node_modules/agent-browser/bin/agent-browser-win32-x64.exe"


def chrome() -> Path | None:
    for base in (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("LOCALAPPDATA")):
        if base and (Path(base) / "Google/Chrome/Application/chrome.exe").is_file():
            return Path(base) / "Google/Chrome/Application/chrome.exe"
    return None


def manual_open() -> bool:
    """用户是不是正开着她的窗口。锁文件超过 12 小时当作没清掉的旧锁（开窗口的脚本被强行关掉会留下它）。"""
    import time
    try:
        return time.time() - MANUAL.stat().st_mtime < 12 * 3600
    except OSError:
        return False


def available() -> bool:
    return sys.platform == "win32" and tool().is_file() and chrome() is not None and not manual_open()


# ── 在隐藏桌面上启动 ─────────────────────────────────────────────────────────

_desktop = None  # 桌面句柄：留着不关，上面的窗口才一直有地方待


def _launch_hidden(command: str, timeout_ms: int = 60000) -> int:
    """在隐藏桌面上跑一条命令（它和它启动的子进程的窗口都在那块桌面上），返回退出码。"""
    import ctypes
    from ctypes import wintypes

    global _desktop
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    if _desktop is None:
        user32.CreateDesktopW.restype = wintypes.HANDLE
        user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                                          wintypes.DWORD, ctypes.c_void_p]
        _desktop = user32.CreateDesktopW(DESKTOP, None, None, 0, 0x10000000, None)  # GENERIC_ALL；已存在就打开它
        if not _desktop:
            raise Unavailable(f"建不了隐藏桌面（错误 {ctypes.get_last_error()}）")
    startup, info = StartupInfo(), ProcessInfo()
    startup.cb = ctypes.sizeof(startup)
    startup.lpDesktop = DESKTOP
    if not kernel32.CreateProcessW(None, ctypes.create_unicode_buffer(command), None, None, False, 0x08000000, None,
                                   str(ROOT), ctypes.byref(startup), ctypes.byref(info)):
        raise Unavailable(f"浏览器起不来（错误 {ctypes.get_last_error()}）")
    try:
        kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)
        kernel32.CloseHandle(info.hThread)


class Browser:
    """她这一回上网用的浏览器。open 第一次调用时才真的启动；用完一定要 close。"""

    def __init__(self, run=None, launch=None):
        self._run = run or self._command
        self._launch = launch or _launch_hidden
        self.started = False

    @staticmethod
    def _command(*args: str, timeout: float = 45) -> str:
        try:
            done = subprocess.run([str(tool()), "--session", SESSION, *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, creationflags=0x08000000)
        except subprocess.TimeoutExpired as error:
            raise Unavailable("浏览器没反应") from error
        return (done.stdout or "") + (done.stderr or "")

    def _start(self) -> None:
        if not available():
            raise Unavailable("浏览器现在用不了")
        PROFILE.mkdir(parents=True, exist_ok=True)
        # 启动这一步在隐藏桌面上做（只开一个空白页，命令里没有需要转义的字符）；之后的命令只是跟它说话
        code = self._launch(f'"{tool()}" --session {SESSION} --headed --executable-path "{chrome()}" '
                            f'--profile "{PROFILE}" --idle-timeout {IDLE} open about:blank')
        if code != 0:
            raise Unavailable(f"浏览器起不来（退出码 {code}）")
        self.started = True

    def open(self, url: str) -> str:
        """打开一个地址，返回页面标题。打不开（断网、被拒）抛 Unavailable。"""
        if not self.started:
            self._start()
        out = self._run("open", url)
        if "✓" not in out:
            raise Unavailable(clean(out, 80) or "打不开")
        self._run("wait", "3500")
        return clean(out.split("✓", 1)[1].strip().splitlines()[0] if out.split("✓", 1)[1].strip() else "", 60)

    def snapshot(self) -> str:
        return self._run("snapshot", "-c")

    def click(self, ref: str) -> bool:
        ok = "✓" in self._run("click", "@" + ref)
        self._run("wait", "2500")
        return ok

    def search(self, ref: str, text: str) -> bool:
        ok = "✓" in self._run("fill", "@" + ref, text) and "✓" in self._run("press", "Enter")
        self._run("wait", "3500")
        return ok

    def scroll(self) -> None:
        self._run("scroll", "down", "1400")
        self._run("wait", "1800")

    def back(self) -> None:
        self._run("back")
        self._run("wait", "2000")

    def url(self) -> str:
        return (self._run("get", "url").strip().splitlines() or [""])[0].strip()

    def title(self) -> str:
        return clean((self._run("get", "title").strip().splitlines() or [""])[0], 80)

    def close(self) -> None:
        if self.started:
            try:
                self._run("close", timeout=20)
            except Unavailable:
                pass
            self.started = False


# ── 快照 → 给模型看的一屏 ────────────────────────────────────────────────────

_LINE = re.compile(r'^\s*- (\w+)(?: "((?:[^"\\]|\\.)*)")?(.*)$')
_KINDS = {"link": "", "button": "按钮 ", "textbox": "输入框 ", "searchbox": "输入框 ", "combobox": "输入框 ", "tab": "标签 "}


def condense(snapshot: str) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """agent-browser 的快照 → (一行行给模型看的字, {编号: (角色, 文字)})。结构性的行不要，重复的字不要。"""
    lines: list[str] = []
    refs: dict[str, tuple[str, str]] = {}
    last = None
    for raw in snapshot.splitlines():
        found = _LINE.match(raw)
        if not found:
            continue
        role, rest = found.group(1), found.group(3)
        name = clean((found.group(2) or "").replace("\\n", " ").replace('\\"', '"'), 160)
        ref = re.search(r"ref=(e\d+)", rest)
        if role == "article":
            if lines and lines[-1] != "——":
                lines.append("——")   # 一条帖子 / 一篇内容的分隔
            last = None
        elif role == "StaticText":
            if len(name) > 1 and name != last:
                lines.append(name)
                last = name
        elif role == "heading" and name:
            lines.append("# " + name)
            last = name
        elif ref and role in _KINDS and name:
            lines.append(f"[{ref.group(1)}] {_KINDS[role]}{name}")
            refs[ref.group(1)] = (role, name)
            last = name
        elif role == "image" and len(name) > 3 and name != last:
            lines.append(f"（图：{name[:50]}）")
    return lines, refs
