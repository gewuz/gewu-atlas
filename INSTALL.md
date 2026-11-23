# 格物工作区 Agent 安装手册

接到安装请求后，从客户指定的工作区根目录执行以下步骤。先确认操作系统和工作区版本，选用一个对应平台的初始化命令。已有工作区先用初始化脚本的 `--check-only` 检查现状；按回执处理缺项，并在收尾时单独报告工作区和可选插件的状态。

本工作区不包含预先编译好的 Python 虚拟环境（`.runtime/`）、第三方依赖库、业务数据卷（`Volumes/`）内容或运行密钥。无论是通过 `git clone` 获取、解压归档包，还是从其他机器直接拷贝目录，**首次运行前都必须先执行 `GewuInitTools/` 下对应平台的初始化脚本**来初始化当前工作区空间。

---

## 1. 第一步：运行 `GewuInitTools` 初始化空间

请在工作区根目录执行对应平台的脚本：

- **macOS**（`arm64` / `x86_64`）：
  ```sh
  sh GewuInitTools/Gewu_init_mac.sh
  ```
- **Linux**（`x86_64` / `aarch64` glibc）：
  ```sh
  sh GewuInitTools/Gewu_init_linux.sh
  ```
- **Windows**（CMD，`x86_64` / `arm64`）：
  ```bat
  GewuInitTools\Gewu_init_win.cmd
  ```

### 初始化脚本会自动完成以下工作

1. **环境与工具检测**：检测宿主系统与架构、Git 可用性，以及本机 Python 3.9+（需含 `venv`、`ensurepip`、`ssl`）。若本机缺少可用 Python，自动下载对应架构的独立 CPython `3.12.16` 并校验 SHA-256 后落盘至 `.runtime/install-env/python`。
2. **Git 仓库初始化**：若当前工作区尚未包含 `.git`（例如直接解压或目录拷贝），自动在根目录执行 `git init -b main`（已通过 `git clone` 建仓的目录则保留现有 `.git` 不覆盖）。
3. **动态发现域并安装依赖**：扫描当前工作区存在的所有格物域（`<Domain>/app.json` 与 `contracts/cli-manifest.json`），创建/重建本机隔离环境 `.runtime/install-env/venv`，按各域声明安装所需第三方库，并执行 `pip check` 与逐域模块导入验证。
4. **入口与技能校准**：生成或修复根目录统一启动器（macOS / Linux 为 `gewu.command`，Windows 为 `gewu.cmd`），同步 `.agents/skills/` 发现链接，校验全部域的 `capabilities`，并生成本机 `INSTALL_RECEIPT.json`。

### 常用可选参数

- `--check-only`：仅体检核验，不写入任何文件。
- `--force-download-python`：忽略宿主 Python，强制下载独立 CPython 到 `.runtime/install-env/python`。
- `--offline --wheels-dir <dir>`：离线环境安装本地 wheel 包（需本机已有 Python 或配合 `--python-tar <file>`）。
- `--reinstall`：重建 `.runtime/install-env/venv` 并重装全部域依赖。
- `--skip-git-init`：跳过自动 `git init`。
- `--json`：输出机器可读的 JSON 初始化回执。

---

## 2. 第二步：核对工作区入口

初始化完成后，使用对应平台的统一网关入口操作各域：

- **macOS / Linux**：
  ```sh
  ./gewu.command list-domains
  ./gewu.command <domain> capabilities
  ```
- **Windows**：
  ```bat
  .\gewu.cmd list-domains
  .\gewu.cmd <domain> capabilities
  ```

---

## 3. 第三步：首次业务前初始化数据卷

本工作区仅提供空 `Volumes/` 目录骨架，不含任何个人数据或密钥。在开展实际业务前，按各域入口规范完成建卷初始化：

- **GewuAtlas**：参阅 [GewuAtlas/AGENTS.md](GewuAtlas/AGENTS.md)，通过 `./gewu.command atlas ...` 创建托管知识库（首次生成 KEK 建卷后请新开对话，仅携带返回的逻辑句柄继续操作）。
- **GewuTicket**：参阅 [GewuTicket/AGENTS.md](GewuTicket/AGENTS.md)，通过 `./gewu.command ticket ...` 初始化工单数据卷。
- **GewuForge**（开发版包含）：参阅 [GewuForge/AGENTS.md](GewuForge/AGENTS.md)，按 `forge-initialize` 技能初始化任务空间。

---

## 4. 询问客户是否安装 Obsidian 配套插件

完成工作区核验后，向客户询问：

> 是否要在 Obsidian 桌面版中使用配套插件？Claudian 可让 Claude Code、Codex 等 Agent 在应用内读取和编辑笔记、搜索、运行命令及执行多步任务；Dataview 用于查询并展示笔记数据。如果同意安装，请指定要安装到哪个 vault。

等待客户明确同意并确认目标 vault。客户跳过或尚未回复时，在回执中记录「插件跳过」或「待确认」。Claudian 随包版本要求 Obsidian 桌面版 1.13.0 或以上；使用 Agent 还需配置相应后端。

### 客户同意后的操作

1. 确认客户提供的是 Obsidian 打开的 vault 根目录，且其中已有 `.obsidian`。如果客户使用自定义配置目录，应先确认实际目录。Atlas 的 `vlt_XX` 是业务句柄，不能据此推测磁盘位置。
2. 从本工作区的 `GewuAtlas/ob-plugin/plugins/` 取出 `realclaudian/` 和 `dataview/`，分别复制到该 vault 的 `.obsidian/plugins/` 下。如果 `plugins/` 不存在可创建。逐个复制，保留其他插件及配置；遇同名插件，版本和随包文件一致时跳过，不一致时向客户确认是否更新。更新仅覆盖随包文件，保留用户设置。
3. 核对每个目标目录均直接包含 `manifest.json`、`main.js` 和 `styles.css`，并与源文件一致。例如 Claudian 的清单位置应为 `.obsidian/plugins/realclaudian/manifest.json`。
4. 请客户在 Obsidian 的「设置 → 第三方插件」允许社区插件并启用 Claudian 和 Dataview，按所选 Agent 后端完成配置。在 Claudian 中尝试读取当前笔记的标题，再检查一个 Dataview 查询是否显示结果。

## 5. 交付安装回执

记录初始化结果、已发现的域及其 `capabilities` 检查结果、数据卷是否已就绪，以及插件的实际状态。只完成复制时记录「已复制，待启用」；已在应用内验证时再记录「已启用并验证」。
