#!/usr/bin/env python3
"""DeepSeek Harness portable launcher.

Installs the bundled Node.js runtime and the DSH module closure into the
directory that contains this executable, then starts dsh web and opens the
browser. Runtime user data stays in the default DSH home (USERPROFILE\\.dsh);
nothing is written outside the application directory and that home.

The same pipeline backs the Qt GUI and the --headless self-test mode, so the
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
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtGui import QFont, QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    layout_paths = Layout(app_dir())
    log_file = LogFile(layout_paths.logs)

    class Worker(QThread):
        logged = Signal(str)
        status_changed = Signal(str)
        progressed = Signal(int, int)
        url_found = Signal(str)
        finished_ok = Signal()

        def __init__(self, force: bool) -> None:
            super().__init__()
            self.cancel_event = threading.Event()
            self.pipeline = Pipeline(
                layout_paths,
                lambda message: self.logged.emit(message),
                lambda message: self.status_changed.emit(message),
                lambda current, total: self.progressed.emit(current, total),
                self.cancel_event,
                force,
                on_url=lambda url: self.url_found.emit(url),
            )

        def run(self) -> None:
            try:
                self.pipeline.run()
            except Cancelled:
                pass
            except Exception as error:  # noqa: BLE001 - reported through the log
                self.logged.emit(f"错误: {error}")
            finally:
                self.finished_ok.emit()

        def request_cancel(self) -> None:
            self.cancel_event.set()
            self.pipeline.cancel_process()

    class Window(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("DeepSeek Harness 便携启动器")
            self.resize(880, 620)
            self.worker: Optional[Worker] = None
            self.log_collapsed = False
            self.web_url = WEB_URL

            central = QWidget()
            root = QVBoxLayout(central)

            self.status_label = QLabel("准备就绪")
            self.status_label.setWordWrap(True)
            root.addWidget(self.status_label)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            root.addWidget(self.progress)

            self.hint = QLabel(
                "首次运行会联网安装依赖模块，可能需要较长时间。"
                "运行数据保存在 USERPROFILE\\.dsh，不会写入安装目录。"
            )
            self.hint.setWordWrap(True)
            root.addWidget(self.hint)

            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setFont(QFont("Consolas", 9))
            self.log_view.setMaximumBlockCount(20000)
            root.addWidget(self.log_view, 1)

            self.force_box = QCheckBox("强制重新安装依赖")
            root.addWidget(self.force_box)

            buttons = QHBoxLayout()
            self.start_button = QPushButton("开始安装并启动")
            self.cancel_button = QPushButton("取消")
            self.toggle_button = QPushButton("折叠日志")
            self.browser_button = QPushButton("打开浏览器")
            self.logs_button = QPushButton("打开日志目录")
            for button in (
                self.start_button,
                self.cancel_button,
                self.toggle_button,
                self.browser_button,
                self.logs_button,
            ):
                buttons.addWidget(button)
            root.addLayout(buttons)
            self.setCentralWidget(central)

            self.start_button.clicked.connect(self.start)
            self.cancel_button.clicked.connect(self.cancel)
            self.toggle_button.clicked.connect(self.toggle_log)
            self.browser_button.clicked.connect(self.open_web)
            self.logs_button.clicked.connect(lambda: self.open_path(layout_paths.logs))
            self.cancel_button.setEnabled(False)
            self.log(f"应用目录: {layout_paths.base}")
            self.log(f"日志文件: {log_file.path}")
            self.start()

        # -- helpers ---------------------------------------------------------
        def log(self, message: str) -> None:
            self.log_view.appendPlainText(message)
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            log_file.write(message)

        def open_url(self, url: str) -> None:
            if os.name == "nt":
                os.startfile(url)

        def open_path(self, path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))

        def open_web(self) -> None:
            self.open_url(self.web_url)

        def on_url(self, url: str) -> None:
            self.web_url = url
            self.log(f"Web UI: {url}")

        def on_status(self, message: str) -> None:
            self.status_label.setText(message)
            log_file.write(f"STATUS {message}")

        def on_progress(self, current: int, total: int) -> None:
            if total <= 0:
                self.progress.setRange(0, 0)
                return
            self.progress.setRange(0, 100)
            self.progress.setValue(int(current * 100 / total))

        def toggle_log(self) -> None:
            self.log_collapsed = not self.log_collapsed
            self.log_view.setVisible(not self.log_collapsed)
            self.toggle_button.setText("展开日志" if self.log_collapsed else "折叠日志")
            self.resize(880, 240 if self.log_collapsed else 620)

        def start(self) -> None:
            if self.worker is not None and self.worker.isRunning():
                return
            self.start_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.progress.setRange(0, 0)
            self.worker = Worker(self.force_box.isChecked())
            self.worker.logged.connect(self.log)
            self.worker.status_changed.connect(self.on_status)
            self.worker.progressed.connect(self.on_progress)
            self.worker.url_found.connect(self.on_url)
            self.worker.finished_ok.connect(self.on_finished)
            self.worker.start()

        def cancel(self) -> None:
            if self.worker is not None and self.worker.isRunning():
                self.on_status("正在取消...")
                self.worker.request_cancel()

        def on_finished(self) -> None:
            self.start_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(0)

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
            if self.worker is not None and self.worker.isRunning():
                answer = QMessageBox.question(
                    self,
                    "退出",
                    "服务或安装仍在运行，退出会终止它。确定退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self.worker.request_cancel()
                self.worker.wait(10000)
            event.accept()

    application = QApplication(sys.argv)
    window = Window()
    window.show()
    return application.exec()


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
