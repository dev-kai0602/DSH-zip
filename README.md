# DSH-zip — DeepSeek Harness 便携版（Windows x64）

把 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 与官方 Node.js 运行时打包成一个便携目录：解压后双击 `DSH-Launcher.exe`，由 Qt 图形界面自动安装 Node 运行时与依赖模块，然后启动 `dsh web` 并自动打开浏览器。

## 下载

发行包（约 138 MB）：见本仓库的 [Releases](../../releases) 页面，下载 `DSH-zip.zip`。

## 包内组成

```
DSH-zip/
  DSH-Launcher.exe                  Qt 启动器（PySide6，单文件，无控制台窗口）
  assets/node-v24.21.0-win-x64.zip  官方 Node.js 运行时（SHA256 已校验）
  deepseek-harness/                  源码与已构建产物
  README.txt                         使用说明
```

## 启动器行为

- 首次运行：解压内置 Node 运行时（缺失时从 nodejs.org 下载）→ 联网执行 `pnpm install --frozen-lockfile` 安装依赖 → 启动 `dsh web` 并打开浏览器。
- 界面包含进度条、日志输出栏、折叠/展开日志按钮、取消按钮，另有打开浏览器/打开日志目录/强制重装依赖。
- 取消会终止子进程树；日志写入 `DSH-zip\logs\launcher-*.log`（UTF-8）。
- 默认端口 127.0.0.1:3080 被占用时，自动改用系统分配的端口。
- 运行数据保存在 `%USERPROFILE%\.dsh`，启动器不覆盖 `DSH_HOME`。

## 自行构建

1. 在 DeepSeek Harness 检出目录执行 `pnpm install` 与 `pnpm run build`，得到 `apps/cli/lib`、`apps/web/dist` 等产物。
2. 把检出目录（排除 `node_modules`、`.git`、`.dsh`）与 `assets/node-v*-win-x64.zip` 放入 `DSH-zip/`。
3. 用 PyInstaller 构建启动器：`python -m PyInstaller --onefile --windowed --name DSH-Launcher launcher/main.py`，把产物放到 `DSH-zip/DSH-Launcher.exe`。
4. 运行 `scripts/package.ps1` 生成 `DSH-zip.zip`（自动排除 `node_modules`/`runtime`/`logs`）。

## 已知限制

- 目标平台为 Windows x64（Windows 10/11、Windows Server 2016 及以上）；Windows Server 2016 未在真机验证。
- 首次运行需要联网，并需要约 4–5 GB 可用磁盘（`node_modules` 约 1.9 GB + pnpm store 约 1.9 GB）。
- 启动器未做代码签名，首次运行可能出现 SmartScreen 提示。
- 不包含任何 API Key；配置方式见包内 `README.txt`。

## 许可

- DeepSeek Harness：MIT，见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- Node.js：MIT。
- `launcher/` 与 `scripts/` 为本仓库为打包所编写的辅助代码。
