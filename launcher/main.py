#!/usr/bin/env python3
"""DeepSeek Harness portable launcher.

Installs the bundled Node.js runtime and the DSH module closure into the
directory that contains this executable, then starts dsh web and opens the
browser. Runtime user data stays in the default DSH home (USERPROFILE\\.dsh);
nothing is written outside the application directory and that home.

The same pipeline backs the tkinter GUI and the --headless self-test mode, so the
install logic can be validated without a display.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

NODE_VERSION = "v24.21.0"
NODE_ZIP_NAME = f"node-{NODE_VERSION}-win-x64.zip"
NODE_DIST_URL = f"https://nodejs.org/dist/{NODE_VERSION}/"
NODE_DIR_NAME = f"node-{NODE_VERSION}-win-x64"
PNPM_VERSION = "11.7.0"
WEB_URL = "http://127.0.0.1:3080"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DOWNLOAD_CHUNK = 256 * 1024


class Cancelled(Exception):
    """Raised when the user cancels the running pipeline."""


def app_dir() -> Path:
    """Return the portable application directory.

    DSH_APP_DIR wins (used by tests), then the directory of the frozen
    executable, then the directory holding this script.
    """
    override = os.environ.get("DSH_APP_DIR", "").strip()
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class Layout:
    """Resolved paths inside the portable application directory."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.dsh_root = base / "deepseek-harness"
        self.assets = base / "assets"
        self.runtime = base / "runtime"
        self.node_dir = self.runtime / "node"
        self.node_exe = self.node_dir / "node.exe"
        self.corepack_js = self.node_dir / "node_modules" / "corepack" / "dist" / "corepack.js"
        self.npm_cli = self.node_dir / "node_modules" / "npm" / "bin" / "npm-cli.js"
        self.corepack_home = self.runtime / "corepack"
        self.pnpm_store = self.runtime / "pnpm-store"
        self.pnpm_prefix = self.runtime / "pnpm"
        self.marker = self.runtime / ".modules-installed"
        self.logs = base / "logs"
        self.dsh_bin = self.dsh_root / "apps" / "cli" / "lib" / "bin.js"


def kill_tree(pid: int) -> None:
    """Terminate a process and its children on Windows."""
    if os.name != "nt":
        return
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


class Pipeline:
    """Node + module installation followed by dsh web."""

    def __init__(
        self,
        layout: Layout,
        log: Callable[[str], None],
        status: Callable[[str], None],
        progress: Callable[[int, int], None],
        cancel: threading.Event,
        force_reinstall: bool = False,
        on_url: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.layout = layout
        self._log = log
        self._status = status
        self._progress = progress
        self.cancel = cancel
        self.force_reinstall = force_reinstall
        self._on_url = on_url if on_url is not None else (lambda url: None)
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self.recent: list = []

    # -- user-facing helpers -------------------------------------------------
    def log(self, message: str) -> None:
        self._log(message)

    def status(self, message: str) -> None:
        self._status(message)

    def progress(self, current: int, total: int) -> None:
        self._progress(current, total)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def _set_proc(self, proc: Optional[subprocess.Popen]) -> None:
        with self._proc_lock:
            self._proc = proc

    def cancel_process(self) -> None:
        """Kill the currently running child process tree, if any."""
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            kill_tree(proc.pid)

    # -- subprocess / download primitives ------------------------------------
    def child_env(self, ci: bool = False) -> dict:
        env = os.environ.copy()
        env["PATH"] = str(self.layout.node_dir) + os.pathsep + env.get("PATH", "")
        env["COREPACK_HOME"] = str(self.layout.corepack_home)
        env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
        if ci:
            env["CI"] = "true"
        return env

    def run_stream(self, argv: list, cwd: Path, tag: str = "", ci: bool = False) -> int:
        """Run a command, streaming merged stdout/stderr line by line."""
        self.log(f"$ {subprocess.list2cmdline(argv)}")
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=self.child_env(ci),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self._set_proc(proc)
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            try:
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, ""):
                    lines.put(line)
            except Exception:  # noqa: BLE001 - stream already reported by exit code
                pass
            finally:
                lines.put(None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        cancelled = False
        while True:
            if self.cancel.is_set() and not cancelled:
                cancelled = True
                self.log("已请求取消，正在终止子进程...")
                self.cancel_process()
            try:
                item = lines.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None and not thread.is_alive():
                    break
                continue
            if item is None:
                break
            text = item.rstrip("\r\n")
            self.recent.append(text)
            if len(self.recent) > 200:
                del self.recent[:100]
            self.log(text)
            marker = "dsh web: "
            if marker in text:
                url = text.split(marker, 1)[1].strip()
                if url.startswith("http"):
                    self._on_url(url)
        proc.wait()
        self._set_proc(None)
        if cancelled:
            raise Cancelled()
        return proc.returncode if proc.returncode is not None else -1

    def download(self, url: str, dest: Path) -> None:
        self.log(f"下载 {url}")
        request = urllib.request.Request(url, headers={"User-Agent": "dsh-portable-launcher"})
        with urllib.request.urlopen(request, timeout=120) as response, open(dest, "wb") as handle:
            total = int(response.headers.get("Content-Length", "0") or "0")
            done = 0
            while True:
                self._check_cancel()
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                self.progress(done, total)

    def extract_zip(self, archive: Path, dest: Path) -> None:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            total = len(members)
            for index, member in enumerate(members, 1):
                self._check_cancel()
                bundle.extract(member, dest)
                if index % 25 == 0 or index == total:
                    self.progress(index, total)

    # -- pipeline steps ------------------------------------------------------
    def check_layout(self) -> None:
        if not self.layout.dsh_root.exists():
            raise RuntimeError(f"缺少 deepseek-harness 目录: {self.layout.dsh_root}")
        if not self.layout.dsh_bin.exists():
            raise RuntimeError(f"缺少 DSH 入口: {self.layout.dsh_bin}")

    def install_node(self) -> None:
        if self.layout.node_exe.exists():
            try:
                probe = subprocess.run(
                    [str(self.layout.node_exe), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
                if probe.returncode == 0:
                    self.log(f"Node 运行时已就绪: {probe.stdout.strip()}")
                    return
            except Exception as error:  # noqa: BLE001 - fall through to reinstall
                self.log(f"已有 Node 探测失败，将重新安装: {error}")
        self.status("安装 Node.js 运行时...")
        self.layout.runtime.mkdir(parents=True, exist_ok=True)
        bundled = self.layout.assets / NODE_ZIP_NAME
        if not bundled.exists():
            candidates = sorted(self.layout.assets.glob("node-v*-win-x64.zip"))
            if candidates:
                bundled = candidates[0]
        if bundled.exists():
            archive = bundled
            self.log(f"使用内置 Node 压缩包: {archive.name}")
        else:
            archive = self.layout.runtime / NODE_ZIP_NAME
            self.status("下载 Node.js 运行时（联网）...")
            self.download(NODE_DIST_URL + NODE_ZIP_NAME, archive)
        self.status("解压 Node.js 运行时...")
        self.extract_zip(archive, self.layout.runtime)
        extracted = self.layout.runtime / NODE_DIR_NAME
        if not (extracted / "node.exe").exists():
            found = [
                item
                for item in self.layout.runtime.iterdir()
                if item.is_dir() and item.name.startswith("node-v") and item.name.endswith("win-x64")
            ]
            if not found:
                raise RuntimeError("解压后未找到 Node 运行时目录")
            extracted = found[0]
        if self.layout.node_dir.exists():
            shutil.rmtree(self.layout.node_dir)
        extracted.rename(self.layout.node_dir)
        if not self.layout.node_exe.exists():
            raise RuntimeError("Node 安装失败：缺少 node.exe")
        self.log(f"Node 已安装到 {self.layout.node_dir}")

    def _pnpm_argv(self) -> list:
        if self.layout.corepack_js.exists():
            return [str(self.layout.node_exe), str(self.layout.corepack_js), "pnpm"]
        if not self.layout.npm_cli.exists():
            raise RuntimeError("Node 运行时缺少 corepack 与 npm，无法安装依赖")
        self.status("安装 pnpm...")
        prefix = self.layout.pnpm_prefix
        code = self.run_stream(
            [
                str(self.layout.node_exe),
                str(self.layout.npm_cli),
                "install",
                "--prefix",
                str(prefix),
                f"pnpm@{PNPM_VERSION}",
                "--no-fund",
                "--no-audit",
            ],
            cwd=self.layout.runtime,
            ci=True,
        )
        if code != 0:
            raise RuntimeError(f"pnpm 安装失败，退出码 {code}")
        pnpm_cjs = prefix / "node_modules" / "pnpm" / "bin" / "pnpm.cjs"
        if not pnpm_cjs.exists():
            raise RuntimeError("pnpm 安装后未找到 pnpm.cjs")
        return [str(self.layout.node_exe), str(pnpm_cjs)]

    def install_modules(self) -> None:
        modules = self.layout.dsh_root / "node_modules"
        installed = self.layout.marker.exists() and modules.exists()
        if installed and not self.force_reinstall:
            self.log("依赖模块已安装，跳过。")
            self.status("依赖模块已安装")
            return
        self.status("联网安装依赖模块（首次运行耗时较长）...")
        argv = self._pnpm_argv() + [
            "install",
            "--frozen-lockfile",
            "--store-dir",
            str(self.layout.pnpm_store),
        ]
        code = self.run_stream(argv, cwd=self.layout.dsh_root, ci=True)
        if code != 0:
            raise RuntimeError(f"依赖安装失败，退出码 {code}")
        self.layout.marker.parent.mkdir(parents=True, exist_ok=True)
        self.layout.marker.write_text(datetime.now().isoformat(), encoding="utf-8")
        self.log("依赖模块安装完成。")

    def _port_conflict(self) -> bool:
        text = " ".join(self.recent).lower()
        return "eaddrinuse" in text or "address already in use" in text or "already in use" in text

    def launch_web(self) -> int:
        self.status("启动 dsh web（将自动打开浏览器）...")
        self.progress(1, 1)
        node = str(self.layout.node_exe)
        binary = str(self.layout.dsh_bin)
        self.recent.clear()
        self.log(f"默认服务地址: {WEB_URL}")
        code = self.run_stream([node, binary, "web"], cwd=self.layout.dsh_root)
        if code != 0 and self._port_conflict():
            self.log("默认端口被占用，改用系统分配的端口重试...")
            self.recent.clear()
            code = self.run_stream(
                [node, binary, "web", "--port", "0"],
                cwd=self.layout.dsh_root,
            )
        return code

    def run(self) -> None:
        try:
            self.check_layout()
            self.install_node()
            self.install_modules()
            code = self.launch_web()
            if code == 0:
                self.status("dsh web 已退出。")
            else:
                self.status(f"dsh web 退出，退出码 {code}。")
        except Cancelled:
            self.status("已取消。")
            self.log("操作已被用户取消。")
        except Exception as error:  # noqa: BLE001 - surfaced in the UI and the log
            self.status(f"失败: {error}")
            self.log(f"错误: {error}")
            raise


class LogFile:
    """Append-only launcher log with a timestamped per-run file."""

    def __init__(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = base / f"launcher-{stamp}.log"
        self.latest = base / "launcher-latest.log"
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                with open(self.latest, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass


def run_headless(args: argparse.Namespace) -> int:
    layout = Layout(app_dir())
    log_file = LogFile(layout.logs)

    def log(message: str) -> None:
        print(message, flush=True)
        log_file.write(message)

    def status(message: str) -> None:
        print(f":: {message}", flush=True)
        log_file.write(f"STATUS {message}")

    def progress(current: int, total: int) -> None:
        if total:
            percent = int(current * 100 / total)
            if percent % 10 == 0:
                print(f"   {percent}% ({current}/{total})", flush=True)

    pipeline = Pipeline(layout, log, status, progress, threading.Event(), args.force_reinstall)
    try:
        contract = getattr(args, "contract", "all")
        if contract == "node":
            pipeline.check_layout()
            pipeline.install_node()
        elif contract == "modules":
            pipeline.check_layout()
            pipeline.install_node()
            pipeline.install_modules()
        else:
            pipeline.run()
    except Cancelled:
        return 130
    except Exception as error:  # noqa: BLE001 - headless exit code
        print(f"FAILED: {error}", flush=True)
        return 1
    return 0


def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    layout_paths = Layout(app_dir())
    log_file = LogFile(layout_paths.logs)
    events: "queue.Queue[tuple]" = queue.Queue()
    state = {
        "worker": None,
        "cancel": threading.Event(),
        "pipeline": None,
        "url": WEB_URL,
        "collapsed": False,
    }

    root = tk.Tk()
    root.title("DeepSeek Harness 便携启动器")
    root.geometry("880x640")
    root.minsize(640, 360)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(3, weight=1)

    status_var = tk.StringVar(value="准备就绪")
    force_var = tk.BooleanVar(value=False)

    status_label = ttk.Label(root, textvariable=status_var, wraplength=840, justify="left")
    status_label.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

    progress = ttk.Progressbar(root, mode="determinate", maximum=100, value=0)
    progress.grid(row=1, column=0, sticky="ew", padx=10, pady=4)

    hint = ttk.Label(
        root,
        text=(
            "首次运行会联网安装依赖模块，可能需要较长时间。"
            "运行数据保存在 USERPROFILE\\.dsh，不会写入安装目录。"
        ),
        wraplength=840,
        justify="left",
    )
    hint.grid(row=2, column=0, sticky="ew", padx=10, pady=4)

    log_frame = ttk.Frame(root)
    log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=4)
    log_frame.grid_columnconfigure(0, weight=1)
    log_frame.grid_rowconfigure(0, weight=1)
    log_text = tk.Text(log_frame, wrap="none", height=18, font=("Consolas", 9))
    log_text.grid(row=0, column=0, sticky="nsew")
    scroll_y = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    scroll_y.grid(row=0, column=1, sticky="ns")
    scroll_x = ttk.Scrollbar(log_frame, orient="horizontal", command=log_text.xview)
    scroll_x.grid(row=1, column=0, sticky="ew")
    log_text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set, state="disabled")

    force_check = ttk.Checkbutton(root, text="强制重新安装依赖", variable=force_var)
    force_check.grid(row=4, column=0, sticky="w", padx=10, pady=4)

    buttons = ttk.Frame(root)
    buttons.grid(row=5, column=0, sticky="ew", padx=10, pady=(4, 10))
    start_button = ttk.Button(buttons, text="开始安装并启动")
    cancel_button = ttk.Button(buttons, text="取消", state="disabled")
    toggle_button = ttk.Button(buttons, text="折叠日志")
    browser_button = ttk.Button(buttons, text="打开浏览器")
    logs_button = ttk.Button(buttons, text="打开日志目录")
    for index, button in enumerate(
        (start_button, cancel_button, toggle_button, browser_button, logs_button)
    ):
        button.grid(row=0, column=index, padx=2)

    def append_log(message: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", message + "\n")
        if int(log_text.index("end-1c").split(".")[0]) > 20000:
            log_text.delete("1.0", "10000.0")
        log_text.see("end")
        log_text.configure(state="disabled")
        log_file.write(message)

    def on_status(message: str) -> None:
        status_var.set(message)
        log_file.write(f"STATUS {message}")

    def on_progress(current: int, total: int) -> None:
        if total <= 0:
            progress.configure(mode="indeterminate")
            progress.start(12)
        else:
            progress.stop()
            progress.configure(mode="determinate", maximum=100, value=int(current * 100 / total))

    def enqueue(kind: str, payload) -> None:
        events.put((kind, payload))

    def make_pipeline(force: bool) -> Pipeline:
        return Pipeline(
            layout_paths,
            lambda message: enqueue("log", message),
            lambda message: enqueue("status", message),
            lambda current, total: enqueue("progress", (current, total)),
            state["cancel"],
            force,
            on_url=lambda url: enqueue("url", url),
        )

    def worker_run(force: bool) -> None:
        pipeline = make_pipeline(force)
        state["pipeline"] = pipeline
        try:
            pipeline.run()
        except Cancelled:
            pass
        except Exception as error:  # noqa: BLE001 - surfaced in the log
            enqueue("log", f"错误: {error}")
            enqueue("status", f"失败: {error}")
        finally:
            enqueue("done", None)

    def drain() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "log":
                    append_log(payload)
                elif kind == "status":
                    on_status(payload)
                elif kind == "progress":
                    on_progress(payload[0], payload[1])
                elif kind == "url":
                    state["url"] = payload
                    append_log(f"Web UI: {payload}")
                elif kind == "done":
                    on_done()
        except queue.Empty:
            pass
        root.after(100, drain)

    def start() -> None:
        if state["worker"] is not None and state["worker"].is_alive():
            return
        state["cancel"].clear()
        start_button.configure(state="disabled")
        cancel_button.configure(state="normal")
        progress.configure(mode="indeterminate")
        progress.start(12)
        state["worker"] = threading.Thread(target=worker_run, args=(force_var.get(),), daemon=True)
        state["worker"].start()

    def cancel() -> None:
        if state["worker"] is not None and state["worker"].is_alive():
            on_status("正在取消...")
            state["cancel"].set()
            pipeline = state["pipeline"]
            if pipeline is not None:
                pipeline.cancel_process()

    def on_done() -> None:
        start_button.configure(state="normal")
        cancel_button.configure(state="disabled")
        progress.stop()
        progress.configure(mode="determinate", maximum=100, value=0)

    def toggle_log() -> None:
        state["collapsed"] = not state["collapsed"]
        if state["collapsed"]:
            log_frame.grid_remove()
            toggle_button.configure(text="展开日志")
            root.geometry("880x240")
        else:
            log_frame.grid()
            toggle_button.configure(text="折叠日志")
            root.geometry("880x640")

    def open_url() -> None:
        if os.name == "nt":
            os.startfile(state["url"])

    def open_logs() -> None:
        layout_paths.logs.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(layout_paths.logs))

    def on_close() -> None:
        if state["worker"] is not None and state["worker"].is_alive():
            if not messagebox.askyesno("退出", "服务或安装仍在运行，退出会终止它。确定退出吗？"):
                return
            state["cancel"].set()
            pipeline = state["pipeline"]
            if pipeline is not None:
                pipeline.cancel_process()
            state["worker"].join(timeout=10)
        root.destroy()

    start_button.configure(command=start)
    cancel_button.configure(command=cancel)
    toggle_button.configure(command=toggle_log)
    browser_button.configure(command=open_url)
    logs_button.configure(command=open_logs)
    root.protocol("WM_DELETE_WINDOW", on_close)

    append_log(f"应用目录: {layout_paths.base}")
    append_log(f"日志文件: {log_file.path}")
    drain()
    root.after(250, start)
    root.mainloop()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="DeepSeek Harness portable launcher")
    parser.add_argument("--headless", action="store_true", help="run the pipeline without a GUI")
    parser.add_argument("--contract", choices=["all", "node", "modules"], default="all")
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--app-dir", default="")
    args = parser.parse_args()
    if args.app_dir:
        os.environ["DSH_APP_DIR"] = args.app_dir
    if args.headless:
        return run_headless(args)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())