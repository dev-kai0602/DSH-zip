DeepSeek Harness 便携版（Windows x64）
======================================

适用系统：Windows 10 / Windows 11 / Windows Server 2016 及以上（x64）

一、如何使用
------------
1. 将压缩包解压到任意目录（路径不要包含中文字符和空格可以获得最佳兼容性）。
2. 双击目录下的 DSH-Launcher.exe。
3. 首次运行会自动完成以下步骤（需要联网）：
   - 解压内置的 Node.js 运行时到 runtime\node；
   - 使用 pnpm 在线安装 DeepSeek Harness 的依赖模块（约 1-2 GB，耗时较长）；
   - 启动 dsh web 并自动打开浏览器（默认地址 http://127.0.0.1:3080）。
4. 后续运行会跳过已完成的安装步骤，直接启动服务。

二、界面说明
------------
启动器使用 tkinter 图形界面（Python 标准库，不依赖任何 Qt 运行库）。
- 进度条：显示当前安装/下载进度（耗时不确定时显示为滚动状态）。
- 日志输出栏：实时显示安装与服务日志。
- 折叠/展开日志：收起或展开日志栏。
- 取消：终止正在进行的安装或服务。
- 打开浏览器 / 打开日志目录：手动打开 Web UI 或日志目录。
- 勾选“强制重新安装依赖”后再次点击“开始安装并启动”可重建 node_modules。

三、API Key 与数据位置
----------------------
本压缩包不包含任何 API Key 或用户数据。
运行数据（含凭据、会话、设置）保存在：%USERPROFILE%\.dsh
  - 凭据文件：%USERPROFILE%\.dsh\.credentials.yaml
  - 设置文件：%USERPROFILE%\.dsh\settings.yaml
配置 API Key 的两种方式：
  1. 启动后在 Web UI 中填写；
  2. 设置环境变量 DEEPSEEK_API_KEY（以及可选的 DEEPSEEK_BASE_URL）。
注意：卸载或迁移时如需保留凭据，请自行备份 %USERPROFILE%\.dsh。

四、目录结构
------------
DSH-zip\
  DSH-Launcher.exe            启动器（tkinter 界面，联网安装 Node 与依赖并启动服务）
  assets\node-v*-win-x64.zip  内置的官方 Node.js 运行时压缩包（可选，缺失时联网下载）
  assets\SHASUMS256.txt       官方校验和
  deepseek-harness\           DeepSeek Harness 源码与已构建产物
  runtime\                    首次运行时自动生成（Node 运行时、pnpm 缓存与依赖存储）
  logs\                       启动器日志（launcher-*.log）

五、常见问题
------------
- 首次安装失败：可在界面点击“取消”后重试；网络不稳定时建议配置 npm 镜像。
- 端口被占用：dsh web 默认使用 127.0.0.1:3080；若该端口已被占用，启动器会自动改用系统分配的端口并打开浏览器。
- 杀软/防火墙提示：本程序未做代码签名，首次运行可能出现 SmartScreen 提示，选择“仍要运行”即可。

六、卸载
--------
删除本目录即可。如需彻底清理，再删除 %USERPROFILE%\.dsh。
