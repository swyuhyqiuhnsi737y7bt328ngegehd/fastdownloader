# ⚡ 极速下载器 Pro (Fast Downloader Pro)

基于 Python + PyQt5 的多线程下载器：分片并行下载、TLS 指纹伪装、浏览器 Cookie 注入、
Playwright 降级、断点续传、全局限速，深色现代 GUI。

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 多线程分片下载 | 将文件按字节范围拆成多段并行下载；服务器不支持 Range 时自动降级单线程 |
| TLS 指纹伪装 | 基于 `curl_cffi`，模拟 Chrome 120 的 TLS 握手特征，降低被反爬拦截的概率 |
| 浏览器 Cookie 注入 | 自动读取 Chrome / Edge / Brave / Vivaldi / Opera 等 Chromium 系及 Firefox 的 Cookie 用于认证 |
| Playwright 降级 | 服务器返回 403 时，自动用真实浏览器解析 Cookie 后重试，或直接经浏览器下载 |
| 断点续传 | 下载进度写入 `.part` 临时文件，暂停/崩溃后重新开始会自动续传 |
| 全局限速 | 设置 KB/s 限速并分摊到各线程；对运行中任务实时生效 |
| 自动重试 | 分片失败按指数退避自动重试（含抖动），并从已写入位置继续，不重复下载；4xx 不重试 |
| 代理 / SOCKS | 支持 http(s) / socks4 / socks5 代理，下载与浏览器降级均生效 |
| 自定义请求头 | 可附加 Referer / Authorization / Cookie 等任意请求头 |
| 任务队列 | 同时下载任务数上限，超出自动排队；支持高/低优先级插队 |
| 任务持久化 | 任务列表落盘，重启后自动恢复，未完成任务可断点续传 |
| 冲突策略 | 目标文件已存在时可选自动重命名 `name (1).ext` / 覆盖 / 跳过 |
| 磁盘保护 | 下载前预检查剩余空间（续传只算缺口），运行中空间过低自动暂停并保留断点 |
| 自动更新 | 检查 GitHub Releases，按当前打包形态下载对应文件、校验 sha256、解压覆盖后自动重启 |
| Cookie 策略 | 浏览器 Cookie 默认按需读取（服务器要求认证时才碰浏览器数据），可关闭 |
| 令牌桶限速 | 全局速率精确可控，运行中可实时调整，不再受线程数影响 |
| 剪贴板监控 | 复制 http(s) 链接时自动提示添加下载任务 |
| 分类管理 | 视频 / 音乐 / 文档 / 程序 / 压缩包 / 未完成 / 已完成 + 关键词搜索 |
| 批量下载 | 每行一个 URL 批量添加 |
| 任务导入导出 | JSON 格式，方便迁移任务列表 |
| 远程服务器浏览 | 浏览并下载 FTP / FTPS / WebDAV / HTTPS 服务器上的文件 |
| C 兼容 API | `downloader_api.py` 实现 `downloader_api.h` 定义的接口，为将来 C++ DLL 实现预留 |

## 🚀 快速开始

### 方式一：源码运行

要求 Python 3.10+（开发环境为 3.12）。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2.（可选）安装 Playwright 自带的 Chromium；
#    若本机已装 Edge/Chrome 可跳过，程序会自动使用
playwright install chromium

# 3. 运行
python main.py
```

### 方式二：使用预编译版本

从 [Releases](https://github.com/你的用户名/fastdownloader/releases) 下载
`FastDownloader.exe`（单文件）或 `fastdownloader.zip`（绿色目录），无需安装 Python。

## 🖥 使用说明

### 添加任务

- 工具栏 **添加**（或 Ctrl+N）：粘贴 URL。程序会自动从剪贴板取链接，
  并从 URL 参数（`response-content-disposition` / `rscd` / `filename` 等）
  或路径末段提取文件名，默认保存到设置的目录。
- **保存路径**可直接编辑/粘贴，也可点"选择文件夹"；改了文件夹会同时记为默认下载目录。
  链接变化时会自动按新文件名刷新默认路径（手动改过的路径不会被覆盖）。
- **批量下载**（Ctrl+B）：每行一个 URL，选择保存目录后统一添加。

### 任务操作

| 操作 | 行为 |
|------|------|
| 暂停 | 保留 `.part` 进度，可随时继续 |
| 继续 | 从 `.part` 断点续传 |
| 停止 | 删除 `.part`，下次从头下载 |
| 重新下载 | 停止后重新开始 |
| 删除 | 停止任务并从列表移除 |

- 双击任务行：运行中 → 暂停；暂停/失败 → 继续。
- 右键任务：开始 / 暂停 / 停止 / 重新下载 / 详情 / 删除。
- **详情**：查看 URL、保存路径及每个线程的字节范围、已下载量、实时速度。
- **限速**：工具栏限速按钮直接开关/设定（KB/s），设置对话框亦可调整；对运行中任务立即生效。
- **队列**：设置里的"同时下载任务数"决定并发上限，超出的任务显示"排队中"，有空位自动开始。
- **优先级**：右键任务 → 高/低/普通优先级，空出槽位时高优先级先跑。
- **设置**：默认下载目录、线程数、并发任务数、全局限速、代理、超时、重试、
  自定义请求头、浏览器 Cookie 策略、TLS 校验、文件冲突策略、磁盘空间保护、启动时检查更新。
- **检查更新**：菜单 **帮助 → 检查更新**；也可以在设置里关闭启动时的自动检查。

### 远程服务器

工具栏 **远程** 打开服务器管理：添加 FTP / FTPS / WebDAV / HTTPS 服务器，
浏览目录后双击文件即可通过下载引擎下载。服务器列表保存在 `servers.json`
（**明文**存储密码，请勿提交到版本库，已加入 .gitignore）。

## 🔧 高级说明

### 浏览器 Cookie

- 支持 Chromium 系（Edge / Chrome / Brave / Chrome Canary / Chromium / Vivaldi / Opera）
  与 Firefox；自动解密 v10/v11 加密的 Cookie（DPAPI + AES-GCM）。
- 浏览器运行时数据库会被锁定，程序会先把 Cookies 连同 WAL 复制到临时目录再读取，不受影响。
- Cookie 缓存 `cookie_cache.json` 有效期 1 小时，库被占用时回退使用。

### Playwright 降级

服务器返回 403 时自动触发：启动浏览器访问页面、等待 JS 挑战/重定向结束、
取回 Cookie 后重试请求。默认**无头模式**，部分站点会检测无头浏览器，
设置环境变量 `FD_PW_HEADLESS=0` 可强制显示真实浏览器窗口。

### 网络与重试配置

| 配置 | 说明 |
|------|------|
| 代理 | `http://host:port`、`socks5://host:port` 等，留空直连；对 Playwright 降级同样生效 |
| 连接/停滞超时 | 建连超时；停滞超时指"连上后长时间收不到数据"即判定失败并重试 |
| 分片重试 | 每个分片失败后的重试次数与初始退避，退避按 `退避×2ⁿ` 增长（上限 30s，含随机抖动） |
| 自定义请求头 | 每行 `Name: Value`，会覆盖同名默认头（例如 `Referer`、`Authorization`） |
| TLS 校验 | 默认关闭（兼容自签名站点），可开启严格校验 |

重试只针对可恢复故障（连接重置、超时、5xx、429、响应体截断）；401/403/404 等直接报错，
避免在无解的情况下空转。每次重试都带 `Range` 从上次写入位置继续。

### 自动更新

菜单 **帮助 → 检查更新**（启动后也会静默检查一次，可在设置里关闭）。
检查走 GitHub Releases API，并**按当前安装形态**挑选文件：

| 运行形态 | 下载文件 | 更新方式 |
|----------|----------|----------|
| 单文件版 `FastDownloader.exe` | `FastDownloader.exe` | 直接替换自身 |
| 目录版（zip 解压后运行） | `fastdownloader.zip` | 解压后覆盖安装目录 |
| 源码运行 | —— | 不自动更新（提示用 git pull） |

流程：下载到 `%TEMP%\fd_update` → **校验 sha256**（GitHub API 提供摘要，不匹配就丢弃）
→ 目录版解压到临时目录 → 生成替换脚本 → 程序退出后由脚本完成
「等待进程退出 → 复制新文件 → 删除临时目录 → 重启新版本」。

之所以要退出后再替换：Windows 上正在运行的 exe 被占用，无法覆盖自己。
如果系统缺少 CA 证书链（企业代理环境），检查更新会自动降级为不校验证书重试，
但下载内容仍会做 sha256 校验。

> 发布新版本时：改 `version.py` 里的 `__version__` → 构建 → 上传 Release，
> tag 用 `v1.2.3` 形式，资产名保持 `FastDownloader.exe` / `fastdownloader.zip`。

### 文件冲突与磁盘保护

**目标文件已存在**（设置 → 常规）三种处理方式：

| 策略 | 行为 |
|------|------|
| 自动重命名（默认） | 存为 `name (1).ext`、`name (2).ext`…，绝不覆盖用户已有文件 |
| 覆盖 | 下载完成后替换原文件（下载中途失败不会动原文件） |
| 跳过 | 直接标记"已存在"，不产生网络请求 |

> 存在未完成的 `.part` 时始终按**断点续传**处理，不会因为"文件已存在"改名或跳过，
> 否则续传进度会丢失。若下载期间有别的程序在同路径新建了文件，完成时会自动另存
> `(1)` 而不是把对方的文件删掉。

**磁盘空间**：

- 开始前预检查剩余空间是否够写下缺口（续传只算未完成的部分，不会误报）；
- 运行中每 10 秒检查一次，剩余空间低于"磁盘保留余量"（默认 100 MB）时**自动暂停**，
  并保留 `.part`，清理磁盘后点"继续"即可断点续传；
- 写入过程中磁盘写满会给出明确提示（磁盘满不重试），已下载部分同样保留。

### 运行时文件

| 文件 | 说明 |
|------|------|
| `downloader_settings.json` | 用户设置（线程数 / 限速 / 保存目录） |
| `download.log` | 下载日志 |
| `cookie_cache.json` | Cookie 缓存 |
| `servers.json` | 远程服务器配置（含明文密码，注意保管） |
| `tasks.json` | 任务列表（重启后恢复；不自动开始下载） |
| `*.part` | 未完成的下载临时文件，删除即放弃续传 |

> **源码运行**时上述文件位于项目根目录；
> **打包版（onefile/standalone）** 位于 `%LOCALAPPDATA%\FastDownloader`
> （onefile 会把代码解压到临时目录并在退出时删除，文件放那里会丢失）。

### C 兼容 API

`downloader_api.h` 定义了 `dl_init / dl_create_task / dl_start_task / dl_pause_task /
dl_cancel_task / dl_get_status / dl_get_progress / dl_get_info_json / dl_free_string /
dl_get_error / dl_shutdown` 等接口，`downloader_api.py` 提供同签名 Python 实现，
可作为 ctypes 绑定或将来替换为 C++ DLL 的对接层（GUI 本身不依赖它）。

## 📦 编译打包

使用 [Nuitka](https://nuitka.net/) 编译，需要本机安装 C 编译器
（MSVC 或 MinGW-w64）。Python 3.12 环境已装 Nuitka 即可。

### 标准版（`build.py`）

Nuitka `--standalone` 把程序与全部依赖打包为免安装的绿色目录：

```bash
python build.py            # 构建 dist/main.dist/main.exe
python build.py --zip      # 构建并打包 dist 为 fastdownloader.zip
python build.py --clean    # 清理所有构建产物
```

> 早期版本的"模块化 DLL"方案（各模块编译为 .dll 单独替换）无法工作——
> Windows 上 Python 只加载 `.pyd` 后缀扩展模块，且模块依赖的第三方包不会随
> .dll 分发，该方案已移除。

### 单文件版（`build_onefile.py`）

打包为单个 `dist/FastDownloader.exe`，运行时自动解压到临时目录，双击即用：

| 模式 | 命令 | 说明 |
|------|------|------|
| 标准（推荐） | `python build_onefile.py` | LTO + 排除无用包 |
| 安全兜底 | `python build_onefile.py --safe` | 最少优化，兼容性优先 |
| 额外瘦身 | `python build_onefile.py --tiny` | 标准 + 排除极少用的包（收益有限，无需 UPX） |
| 绿色目录 | `python build_onefile.py --standalone-upx` | standalone + UPX 压缩后手动打 zip |
| 清理 | `python build_onefile.py --clean` | 清理构建产物 |

**UPX 路径**（仅 `--tiny` / `--standalone-upx` 需要）：脚本自动搜索
`E:\upx-5.2.0-win64\upx.exe`、`E:\upx\upx.exe`、`C:\upx\upx.exe`、
`C:\Program Files\upx\upx.exe`、`tools/upx.exe`，或 PATH 中的 `upx`。

> 注意：UPX 会跳过 PyQt5/Qt 的 DLL（压缩后可能不稳定）；`--onefile` 模式下
> Nuitka 已用 zstd 压缩 payload，再 UPX 收益有限。

## 🛠 项目结构

```
fastdownloader/
├── main.py                 # 程序入口（PyQt5）
├── ui.py                   # GUI：任务表、分类、搜索、批量、设置、远程管理
├── engine.py               # 下载引擎：分片下载、断点续传、限速、Playwright 降级
├── utils.py                # 大小/时间格式化
├── settings.py             # 设置持久化（downloader_settings.json）
├── clipboard_watcher.py    # 剪贴板链接监控
├── browser_cookies.py      # 浏览器 Cookie 读取与解密
├── playwright_handler.py   # Playwright 浏览器适配（Cookie 解析 / 直接下载）
├── remote_browser.py       # FTP / FTPS / WebDAV / HTTPS 远程服务器
├── downloader_api.py       # C 兼容 API 的 Python 实现
├── downloader_api.h        # C/C++ 接口头文件
├── paths.py                # 数据文件位置（打包版写入 %LOCALAPPDATA%）
├── task_store.py           # 任务列表持久化与恢复
├── version.py              # 版本号与仓库地址
├── assets/app.ico          # 应用图标（构建时写入 exe 版本资源）
├── packaging/FIRST_RUN.txt # 随发布包分发的首次运行/白名单说明
├── updater.py              # 更新检查 / 下载校验 / 解压 / 后台替换
├── tests/                  # 测试（故障注入服务器 + 引擎/队列/GUI 测试）
├── requirements.txt        # Python 依赖
├── build.py                # standalone 编译 + zip 打包
├── build_onefile.py        # 单文件编译
└── README.md
```

## 🔧 技术栈

| 组件 | 技术 |
|------|------|
| GUI | PyQt5（Fusion 深色主题） |
| HTTP 引擎 | curl_cffi（TLS 指纹伪装，Chrome 120） |
| 浏览器引擎 | Playwright（Edge / Chrome / Chromium） |
| Cookie 解密 | pycryptodome（AES-GCM）+ pywin32（DPAPI） |
| 编译工具 | Nuitka |
| Python | 3.12 |

## ❓ 常见问题

**下载 403 / 被服务器拒绝？**
程序会自动尝试浏览器 Cookie 与 Playwright 降级。若仍失败：
确认本机装有 Edge/Chrome 或执行过 `playwright install chromium`；
部分站点检测无头浏览器，设置 `FD_PW_HEADLESS=0` 后重试。

**Cookie 一个都没读到？**
- 确认以同一 Windows 用户运行（DPAPI 解密依赖当前用户凭据）；
- 首次读取需浏览器生成过该域名的 Cookie；
- 检查 `download.log` 中的 [cookie] 日志。

**下载失败了会自动重试吗？**

会。分片级重试（默认 3 次，指数退避）只针对连接重置／超时／5xx／429 等可恢复故障，
并从已写入位置带 Range 续传；4xx（认证、权限、不存在）直接报错不空转。
重试可在**设置 → 重试**里调整次数与退避。

**怎么让某个任务先下载？**

右键该任务选择"⏫ 高优先级"，当前任务跑完后它会插到队首。
并发上限在**设置 → 常规 → 同时下载任务数**。

**限速不准确？**
限速为全局目标值并分摊到各线程，高速网络下受网络抖动影响会有轻微偏差。

**下载到一半停了，能续传吗？**
能。只要 `.part` 文件还在，重新开始任务即可断点续传；点"停止"会删除 `.part`。

**Playwright 打不开浏览器？**
源码运行需先 `playwright install chromium`，或本机安装 Edge/Chrome；
编译版不含浏览器二进制，同样依赖本机浏览器。

**提示"数据库被锁定"？**
新版本已改为复制数据库副本读取，一般无需关闭浏览器；如仍提示，关闭对应浏览器后重试。

## 🛡 关于杀毒软件误报

打包版（尤其单文件版）可能被杀软报毒，这**几乎都是误报**，原因是可以解释的：

| 触发点 | 为什么被盯上 |
|--------|--------------|
| 未做代码签名 | 没有签名 = "来源不明的可执行文件"，启发式评分直接抬高 |
| 单文件自解压 | 把自己解压到 `%TEMP%` 再运行，与恶意软件 dropper 行为一致 |
| 读取浏览器 Cookie | 本项目会读 Chromium/Firefox 的 Cookies 并做 DPAPI 解密——这正是凭据窃取类恶意软件的特征行为 |
| 自更新替换自身 | 下载 exe 并替换当前程序，类似"自更新型"恶意软件 |

### 已经做了哪些降低误报的处理

- **补全版本资源**：产品名/公司/文件描述/版权/版本号/图标（构建时自动写入，`build.py`、`build_onefile.py`）；
  缺这些元数据的裸 exe 最容易被判可疑。
- **Cookie 按需读取**（默认）：普通下载**完全不碰**浏览器数据，只有服务器返回 401/403
  需要登录态时才读取一次；也可以在 设置 → 网络 里彻底关闭。
- **不使用 UPX 默认加壳**：加壳会**提高**误报率，见下。
- 目录版（zip）比单文件版误报更少（没有自解压行为），建议优先分发。

### 为什么"加壳"适得其反

UPX 是最常被恶意软件使用的壳，杀软对它的启发式规则很激进；**未签名 + 加壳**是
典型的恶意软件画像，评分往往比不加壳更高。反例：Windows 自带的 `notepad.exe`
本身就是 UPX 压过的（PE 节名 `UPX0`/`UPX1`），但它**有微软签名**，所以从不报毒——
决定因素是签名，不是壳。

所以本项目的构建默认不加壳（`--standalone-upx` 仅作为极限体积的实验性选项，
运行时会打印警告）。真要压体积，优先 `--tiny`。

### 误报了怎么办：自己加白名单

> **本程序不会、也不会尝试去关闭或绕过杀毒软件。**
> 自动规避杀软是恶意软件的行为，而且会被杀软判为"篡改防护"（Defender 就会报
> `VirTool:Win32/DefenderTamperingRestore`），只会让情况更糟。
> 是否信任本程序、要不要加白名单，请**由你自己判断并手动操作**。

#### 第 0 步：先确认文件确实没被篡改

不要给来路不明的文件加白名单。先核对哈希：

```bat
:: 单文件版
certutil -hashfile FastDownloader.exe SHA256

:: 目录版（对 main.exe）
certutil -hashfile main.exe SHA256
```

把输出和 Release 说明里公布的 SHA256 比对——**一致再加白名单**。
程序里也有现成入口：**帮助 → 被杀软误报了？**，可以直接复制当前文件的 SHA256 和排除命令。

#### 方式 A：Windows 安全中心（Defender）

图形界面：

1. 打开 **设置 → 隐私和安全性 → Windows 安全中心**
2. 点 **病毒和威胁防护 → "病毒和威胁防护"设置 → 管理设置**
3. 拉到底部 **排除项 → 添加或删除排除项**
4. **添加排除项**，选择：
   - 单文件版 → 选 **文件**，指向 `FastDownloader.exe`
   - 目录版 → 选 **文件夹**，指向解压出来的整个目录
5. 如果文件已经被隔离：**保护历史记录 → 找到该威胁 → 操作 → 还原**

命令行（自己以**管理员**身份打开 PowerShell 执行）：

```powershell
# 单文件版（把路径换成你的实际位置）
Add-MpPreference -ExclusionPath 'D:\FastDownloader.exe'

# 目录版
Add-MpPreference -ExclusionPath 'D:\fastdownloader'

# 查看已添加的排除项
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath

# 想撤销：
# Remove-MpPreference -ExclusionPath 'D:\FastDownloader.exe'
```

> 程序里的"复制 Defender 排除命令"按钮只是把上面这条命令放进剪贴板，
> **不会替你执行**——提权和修改杀软设置必须由你本人确认。

#### 方式 B：第三方杀软

| 软件 | 加白名单的位置 |
|------|----------------|
| 360 安全卫士 / 杀毒 | 设置 → 白名单 → 添加目录（或"信任区"） |
| 火绒安全 | 防护中心 → 信任区 → 添加文件/目录 |
| 腾讯电脑管家 | 病毒查杀 → 设置 → 白名单 → 添加文件/目录 |
| 卡巴斯基 | 设置 → 威胁与排除项 → 管理排除项 → 添加 |
| 诺顿 / Avast / ESET | 设置里搜"排除项 / Exclusions / 例外"，添加文件或目录 |

加完之后，记得去**隔离区把已删除的文件还原**，否则程序仍然缺文件。

> 打包好的 zip / 目录版里已经附带一份 **《安装说明（首次运行必读）.txt》**，
> 内容就是下面这些步骤，解压后第一眼就能看到。

#### 方式 C：不加白名单（更省事）

- 直接**源码运行**：`pip install -r requirements.txt` 后 `python main.py`——杀软几乎不会拦 Python 脚本；
- 用 **zip 目录版**代替单文件版：没有自解压行为，误报率明显更低；
- 把程序放在**非系统盘的普通目录**（别放 `C:\Windows`、`%TEMP%`、启动目录这类高危位置）。

#### 顺带一提：确认到底是哪套杀软在拦你

安全中心里"注册"了几套杀软 ≠ 几套在运行：卸载残留的条目会一直留在注册表里，
看起来像装了三套，实际可能只有一套在工作。先分别查"注册"和"在运行"：

```powershell
# 已注册的（含卸载残留）
Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct |
    Select-Object displayName, productState

# 真正在跑的实时防护进程（360 / 腾讯管家 / Defender 各自的特征进程）
Get-Process | Where-Object { $_.ProcessName -match '360|QQPC|ZhuDongFangYu|MsMpEng' } |
    Select-Object ProcessName, Id
```

注意：只要安全中心里注册了第三方杀软，Windows 就会把 **Defender 降为被动模式**
（`WinDefend` 服务显示 Stopped 是正常的，不是故障）。所以"加白名单"要加在
**实际在跑的那一套**上，加错了地方等于没加。

尤其留意 **360 的主动防御（ZhuDongFangYu 进程）**：它拦的是"未知程序的行为"，
对"读取浏览器 Cookie""程序自我更新"这类动作最敏感——而这正是本程序会被拦的原因。
在 360 的信任区里放行整个安装目录，比逐个文件放行更省事。

#### 提交误报，让厂商修掉（推荐同时做）

- 微软：<https://www.microsoft.com/en-us/wdsi/filesubmission>（选 "Software developer" 或
  "File submitted is clean but detected"）——免费，通常 1-2 天处理，处理完全球用户都不再误报；
- 先上传 <https://www.virustotal.com/> 看看有多少家报、报什么名字，申诉时附上链接更有效。

#### 根治：代码签名

买一张代码签名证书（OV 约 $100-200/年）给 exe 签名，是唯一能让误报**基本消失**的办法。
本项目没有签名证书，所以打包版始终可能被个别杀软误报——这也是为什么上面反复强调
"确认哈希再加白名单"。

> 本程序不会上传任何数据：唯一的出网行为是下载你添加的链接、读取 release 检查更新，
> 以及（可选）通过 Playwright 打开站点取 Cookie。所有本地文件读写都限于你指定的下载目录与程序数据目录。

## 📄 License

MIT
