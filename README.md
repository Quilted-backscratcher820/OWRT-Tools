# OpenWrt 本地编译工具

一个面向 Linux 和 WSL2 的 PySide6 图形化 OpenWrt 编译工具。它把环境检查、源码管理、配置生成、自定义插件、工具链复用、编译日志和固件备份串成一套可视化工作流。

当前版本：**4.0**

> 本工具会下载并修改 OpenWrt 源码、执行 `make`，还可以运行用户选择的 Shell 脚本。使用前请阅读[安全说明](#安全说明)，并在可恢复的工作目录中操作。

## 主要功能

- 检查编译依赖、运行权限及 Google、YouTube、GitHub 主域名连通性；任一前置检查失败时锁定项目与编译页面。
- 按指定分支浅克隆 OpenWrt 源码，自动执行 feeds 更新与安装。
- 支持多个设备、常规 `CONFIG_*` 配置、完整 `.config` 和普通 TXT 配置导入。
- 支持自定义插件仓库、核心包与 LuCI 配套包识别、同名插件去重。
- 支持把已编译的 IPK/APK 文件内容集成到固件。
- 支持自定义 Shell 脚本的换行转换、语法校验、权限设置和构建前执行。
- 每次编译先生成有效配置，再执行 `make clean`；跨日期时刷新 feeds，并在并行编译失败后自动使用单线程详细日志重试。
- 自动保存和应用匹配“项目名 + 平台名”的工具链，同时保留手动操作。
- 实时显示当前步骤、编译耗时和日志；支持停止正在运行的任务。
- 可配置源码、工具链、日志和固件备份目录，编译完成后可自动或手动打开输出目录。

## 支持范围

| 环境 | 启动方式 | 状态 |
| --- | --- | --- |
| Debian/Ubuntu 桌面 | `run_owrt_linux.sh` 或桌面入口 | 支持 |
| WSL2 + WSLg/可用图形环境 | Linux 入口或 Windows CMD 入口 | 支持 |
| Windows 原生目录 | 直接双击 CMD | 不支持 |
| macOS 或非 apt 发行版 | 自动安装依赖 | 不支持 |
| Linux `root` 用户 | 编译 | 不支持 |

自动依赖安装目前只支持带有 `apt-get` 的 Debian、Ubuntu 和 WSL2 环境。其他 Linux 发行版可以参考 [`support/dependencies.txt`](support/dependencies.txt) 手动准备依赖，但不属于当前支持范围。

## 下载发布包

不熟悉 Git 的用户可以直接打开 [最新版本下载页面](https://github.com/VIKINGYFY/OWRT-Tools/releases/latest)，下载 `OWRT-Tools-版本号.zip` 并解压。压缩包内已经包含 Linux、Linux 桌面和 Windows WSL2 入口，不需要下载 GitHub 自动生成的 Source code 压缩包。

每次 `main` 分支出现新提交后，GitHub Actions 会自动重新打包并发布，同时生成 `SHA256SUMS`。发布页面始终只保留最新一份自动发布及其标签。

## 快速开始

### Linux 或 WSL2 终端

克隆或下载仓库后，在项目根目录运行：

```bash
./run_owrt_linux.sh
```

Linux 桌面环境也可以双击 [`run_owrt_linux.desktop`](run_owrt_linux.desktop)。如果桌面环境首次询问是否信任或执行该文件，请选择执行。

### 从 Windows 启动 WSL2

1. 在资源管理器中通过 `\\wsl.localhost\发行版名称\...` 或 `\\wsl$\发行版名称\...` 打开本仓库。
2. 双击 [`run_owrt_windows_wsl2.cmd`](run_owrt_windows_wsl2.cmd)。
3. CMD 入口会识别当前 WSL 发行版，并在该发行版中启动同一个 Linux 入口。

CMD 入口不会从 `C:\`、`D:\` 等 Windows 原生路径启动。WSL2 还需要 WSLg，或已经正确配置的 X11/Wayland 图形环境。

## 首次运行

入口固定使用系统 Python，不创建虚拟环境，也不调用远程环境初始化脚本。依赖不完整时会运行本地 [`support/system_setup.sh`](support/system_setup.sh)，依次执行：

```text
apt-get update
apt-get full-upgrade
apt-get install
apt-get autoremove --purge
apt-get autoclean
apt-get clean
```

随后会把 PySide6 安装到系统 Python、恢复入口权限，并重新检查全部依赖。

注意：首次安装包含系统完整升级，可能耗时较长，也可能需要输入 `sudo` 密码。请先确认当前系统允许升级，并使用普通用户启动工具；不要使用 `sudo ./run_owrt_linux.sh`。

## 使用流程

1. **检查环境**：等待编译依赖、运行权限和三个网络目标全部变为绿色“通过”。
2. **调整目录**：在“环境与设置”中选择编译目录、工具链目录、日志目录及可选的固件备份目录。
3. **添加项目**：填写项目地址、分支和项目名。工具会浅克隆源码，然后执行 `scripts/feeds update -a` 和 `scripts/feeds install -a`。
4. **选择配置**：选择已有项目并填写平台、设备、主机名、LAN IP、WiFi 账号和密码。
5. **添加扩展**：按需添加插件仓库、导入 IPK/APK、导入配置或选择自定义脚本。
6. **开始编译**：点击“修改并开始编译”。工具会清理源码树、准备插件和设置、生成并校验配置、下载依赖，然后编译固件。
7. **查看结果**：成功后打开项目的 `bin/targets/`，或使用自动打开选项。
8. **保存产物**：启用备份时，固件、配置、日志和校验文件会保存到独立的时间戳目录。

## 配置说明

### 平台和设备

平台支持 OpenWrt 常见写法，例如：

```text
qualcommax/ipq60xx
mediatek_filogic
x86_64
```

多个设备名使用空格分隔。工具会在 `make defconfig` 后检查指定的平台、设备和软件包是否仍然存在；如果 Kconfig 丢弃了明确要求的项目，构建会停止。

每次构建还会读取 [`support/forced_config.txt`](support/forced_config.txt)，把其中的配置按符号去重后强制混入。该清单没有界面入口，只能手动编辑文件；导入 `.config` 或工具元数据时，其中的强制项不会显示在常规配置编辑区。默认清单包含多设备、逐设备 rootfs、工具链和 ccache 所需设置，并关闭 initramfs 与 video feed。清单缺失、为空、格式错误或在 `make defconfig` 后未保留时，环境检查或构建会停止。

默认值如下：

| 项目 | 默认值 |
| --- | --- |
| 主机名 | `OWRT` |
| LAN IP | `192.168.10.1` |
| WiFi 账号 | `OWRT` |
| WiFi 密码 | `12345678` |

WiFi 密码必填且至少 8 位，在界面中以普通文本显示。

### 完整配置和导入配置

选择已有项目时，工具会优先检测源码根目录中的完整 `.config`。如果其中包含有效的平台和设备选择，会自动填入界面；之后手动修改、添加插件或导入新配置时，界面会提示当前完整配置将被覆盖。

“导入配置”支持：

- 标准 OpenWrt `.config`；
- 包含 `CONFIG_*` 项的普通 `.txt`；
- 工具生成的配置快照及同目录 `build-settings.json`。

`build-settings.json` 用于恢复主机名、IP、WiFi、插件、脚本和备份选项，权限设置为 `0600`。常规配置上方的过滤器只改变显示内容，不改变最终用于构建的完整配置；过滤期间编辑器会暂时只读。

## 自定义插件

每个插件仓库占一行，需要填写仓库地址、分支和插件名。多个插件名使用空格分隔，名称支持连字符。

构建时工具会浅克隆插件仓库，把匹配的软件包复制到 `package/custom/`，并移除源码树或 feeds 中的同名包，避免重复定义。

当只填写核心包名时，工具会尝试识别同一仓库中的 LuCI 配套包。例如仓库同时包含 `axonhub` 和 `luci-app-axonhub` 时，只填写：

```text
axonhub
```

即可同时选择两者。也可以显式填写完整名称：

```text
axonhub luci-app-axonhub
```

## 自定义脚本

“选择脚本”仅接受 Shell 脚本。选择后，工具会：

1. 复制脚本到当前项目的 `.builder/scripts/`；
2. 使用 `dos2unix` 转换为 Linux 换行格式；
3. 使用 `bash -n` 做语法检查；
4. 设置 `0755` 权限并记录 SHA-256；
5. 编译前再次校验摘要；
6. 在初始配置生成后、配置校验前，从项目根目录执行脚本。

脚本可以读取与 OpenWRT-CI `Settings.sh` 类似的环境变量，包括 `WRT_TARGET`、`WRT_NAME`、`WRT_IP`、`WRT_SSID`、`WRT_WORD`、`WRT_DATE` 和 `GITHUB_WORKSPACE`。

脚本不会在选择时执行，但点击“修改并开始编译”后会以当前用户权限实际执行。语法检查和 SHA-256 校验不能证明脚本可信，选择前必须审阅脚本内容。

## IPK/APK 集成

“导入 IPK/APK”会把文件复制到项目 `.builder/prebuilt/`，记录 SHA-256，并生成 `package/custom/builder-prebuilt` 包。构建期间：

- APK 使用源码工具链中的 `apk extract` 提取；
- IPK 使用 `ar` 和 `tar` 提取 `data.tar.*`；
- 提取出的文件会直接复制到固件 rootfs；
- 不执行软件包的安装脚本或 post-install 脚本。

这是一种“只集成文件数据”的方式，不会自动解决依赖、内核模块 ABI、架构兼容性、文件冲突或维护脚本。普通软件包优先使用源码插件和 OpenWrt 的正常依赖机制。

## 编译顺序

每次构建都执行以下核心步骤：

```text
自动应用匹配工具链
下载和去重自定义插件
生成默认设置与预编译包集成项
生成初始 .config
执行可选自定义脚本
强制混入并去重 support/forced_config.txt
make defconfig 并校验初始配置
make clean
日期变更时更新、安装 feeds 并再次去重插件
再次强制混入配置并写入内部编译标识
make defconfig 并校验最终配置
make download -j$(nproc)
make -j$(nproc)
失败时 make -j1 V=s
备份固件并自动保存工具链
```

工具会在内部生成不可见、不可编辑的 `OWRT-Tools-YYYYMMDD-HHMMSS` 编译标识。标识会写入固件 `/etc/owrt-tools-build-id`；如果源码包含受支持的 LuCI 固件版本页面，也会追加到版本显示中。

源码浅克隆完成时会记录本项目最后一次 feeds 更新日期。后续构建只有在日期变化或旧项目缺少日期记录时，才会在初始 `make defconfig` 和 `make clean` 完成后执行一次 `scripts/feeds update -a` 与 `scripts/feeds install -a`；同一天的重复构建会跳过刷新。

编译计时从生成最终配置开始。停止按钮会请求终止当前任务；具体退出速度取决于正在执行的外部命令。

## 工具链

成功编译后，工具会把以下目录打包为带时间戳的工具链归档：

```text
staging_dir/toolchain-*
staging_dir/host
staging_dir/hostpkg
```

下次编译同一“项目名 + 平台名”时会自动应用最新匹配归档。应用前会校验清单、SHA-256、项目名、平台名和归档路径；被替换的现有目录会移动到项目 `.builder/toolchains/`。工具链页面仍提供手动保存和应用。

工具链不能保证跨源码版本、架构或主机环境兼容。切换上游版本后如果出现异常，应移除不再适用的归档并重新完整编译。

## 日志、输出和备份

实时日志和保存日志的每一行都带有 `YYYY/MM/DD HH:MM:SS` 时间戳。默认日志文件格式为：

```text
logs/log-平台-时间戳.txt
```

如果已有项目的 `bin/targets/` 下存在常见固件镜像，“打开当前输出目录”会直接可用。

启用固件备份后，目标目录按 `平台名-时间戳` 命名，包含：

- `bin/targets/` 中的固件产物；
- 初始配置、校验后的初始配置和最终 `.config`；
- 本轮完整构建日志；
- `build-settings.json`；
- `SHA256SUMS`。

备份留存数按同一平台计算，超出数量的旧目录会被删除。关闭固件备份后，备份目录和留存数设置不会参与构建。

默认运行数据目录：

| 目录 | 内容 |
| --- | --- |
| `projects/` | 浅克隆的 OpenWrt 源码 |
| `logs/` | 操作和编译日志 |
| `toolchains/` | 工具链归档与 JSON 清单 |
| `backup_firmware/` | 固件备份、配置、日志和校验文件 |
| `项目/.builder/` | 项目元数据、配置快照、脚本、预编译包和回退内容 |

这些默认运行目录已由 [`.gitignore`](.gitignore) 排除。更换到仓库外目录时，请自行确保空间、权限和备份策略符合需求。

## 安全说明

- 不要把访问令牌、账号或密码直接写入 Git 仓库地址。需要认证的仓库应使用系统 Git 凭据管理。
- WiFi 密码会按原文写入构建日志和权限为 `0600` 的 `build-settings.json`，固件备份也会包含这些文件。
- 自定义脚本会以启动工具的普通用户权限运行，能够修改源码和该用户可访问的其他文件；本工具不提供容器或沙箱隔离。
- 自定义插件和预编译包来自外部仓库或文件，构建前应核对来源、分支、提交内容和摘要。
- 编译目录需要大量磁盘空间。不要把重要文件所在目录直接设为编译目录，也不要与固件输出目录互相嵌套。
- 发布问题报告前请先清理日志中的 WiFi 密码、私有仓库地址、本地路径和其他敏感信息。

## 常见问题

### 项目与编译页面全部变灰

这是前置检查未全部通过的预期行为。回到“环境与设置”，查看红色检查项并处理缺失依赖、目录权限或网络问题，然后重新检测。

### 依赖安装后仍然失败

在终端重新运行入口以查看完整安装输出：

```bash
./run_owrt_linux.sh
```

也可以单独执行只读检查：

```bash
python3 support/check_requirements.py
```

不要在虚拟环境中启动。入口会优先选择系统 Python，PySide6 也必须能被该 Python 导入。

### WSL2 的 CMD 入口无法启动

确认仓库位于 WSL 文件系统，并从 `\\wsl.localhost\...` 或 `\\wsl$\...` 路径双击 CMD。Windows 盘符路径不会被转换为 WSL 路径。

### 导入脚本后无法编译

脚本必须能通过 `dos2unix` 和 `bash -n`，且暂存后不能再被修改。如果脚本运行失败，查看日志中“执行自定义脚本”步骤的原始错误。

### 编译结束后没有输出目录

工具只在源码树存在 `bin/targets/` 时认定输出有效。先查看单线程 `V=s` 重试日志中的首个实际错误；缺少输出目录本身通常只是前面编译失败的结果。

## 开发与测试

建议在仓库根目录使用系统 Python 执行：

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
ruff check core tests support/check_requirements.py
python3 -m compileall -q core tests support/check_requirements.py
for script in run_owrt_linux.sh support/*.sh; do bash -n "$script"; done
python3 support/check_requirements.py
```

当前 4.0 版本包含 45 个自动化测试，覆盖环境门禁、GUI 状态、入口、配置导入、强制配置、脚本暂存、插件解析、预编译包、工具链、日志、备份和模拟构建流程。自动化测试不等于所有上游源码、目标设备或真实 WSL/Windows 图形环境都已经完成运行验证。

版本规则：用户没有明确指定大版本时，每次推送前自动把一位小数版本号递增 `0.1`，并同步代码、桌面入口、测试和 README；明确指定大版本时以用户要求为准。

## 已知限制

- 自动安装仅支持 Debian/Ubuntu/WSL2 的 apt 环境。
- Google、YouTube、GitHub 三项网络检测必须全部通过后才能继续，即使某次编译只使用其中一个站点。
- 完整固件构建结果取决于上游源码、feeds、目标配置、网络、磁盘空间和主机资源。
- 插件自动配对依赖仓库中存在可明确识别的核心包与 `luci-app-*` 包；名称不明确或重复时会要求显式填写。
- IPK/APK 数据集成不替代 OpenWrt 包管理和 ABI 校验。
- 当前测试主要覆盖静态检查、单元测试和模拟工作流，不代表固件已在目标硬件上启动或通过网络功能验证。

## 致谢

自定义修改流程和脚本变量设计参考了 [VIKINGYFY/OpenWRT-CI](https://github.com/VIKINGYFY/OpenWRT-CI)。

## 许可证

本项目采用 [MIT License](LICENSE) 开源。
