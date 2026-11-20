# GewuInitTools

面向「`git clone` 或用户直接拷贝工作区目录到新设备」场景的跨平台环境检测与初始化工具集（支持 macOS / Linux / Windows）。

## 包含工具

| 平台 | 脚本 | 基础 Python 缺省下载目标 | Git 缺省处理 | 生成网关启动器 |
| :--- | :--- | :--- | :--- | :--- |
| macOS (`arm64` / `x86_64`) | `GewuInitTools/Gewu_init_mac.sh` | `.runtime/install-env/python` (`CPython 3.12.16`) | 检测系统 Git（缺省提供国内 Homebrew 镜像指引） | `gewu.command` |
| Linux (`x86_64` / `aarch64` glibc) | `GewuInitTools/Gewu_init_linux.sh` | `.runtime/install-env/python` (`CPython 3.12.16`) | 检测系统 Git（缺省提供 apt/dnf 国内源指引） | `gewu.command` |
| Windows (`x86_64` / `arm64`) | `GewuInitTools/Gewu_init_win.cmd` | `.runtime\install-env\python` (`CPython 3.12.16`) | 缺省自动下载便携版 `MinGit 2.56.1` 到 `.runtime\install-env\git` | `gewu.cmd` |

## 国内镜像与自动回退策略

- **独立 CPython（`python-build-standalone 3.12.16+20261110`）**：`npmmirror`（阿里） → 南京大学 `mirror.nju.edu.cn` → GitHub 官方源，全链路强制校验原版 SHA-256。
- **Windows 便携 Git（`MinGit 2.56.1`）**：华为云 `repo.huaweicloud.com` → `npmmirror` → GitHub 官方源，强制校验官方 SHA-256 并自动接入 `gewu.cmd` `PATH`。
- **PyPI 依赖库**：默认 `mirrors.aliyun.com/pypi/simple/` → 失败自动回退清华 `pypi.tuna.tsinghua.edu.cn/simple`（支持通过 `GEWU_PIP_INDEX_URL` 自定义覆盖）。

## 解决的问题

当工作区目录通过 `git clone` 或直接拷贝部署时，目标机器可能存在：
1. 本机缺少 Python 3.9+，或缺失 `venv` / `ensurepip`（如 Ubuntu 未装 `python3-venv`、macOS 仅有 `/usr/bin/python3` Xcode CLT 占位符、Windows 仅有 Microsoft Store 别名）。
2. 从源机器拷贝过来的 `.runtime/install-env/venv` 跨机路径失效、跨平台（如 Windows 与 macOS/Linux 互拷）或损坏。
3. 拷贝或拉取后丢失 `gewu` / `gewu.command` 可执行权限，或缺少对应平台的启动器（`gewu.command` / `gewu.cmd`）。
4. Windows 未开启 `core.symlinks=true` 时，Git 将 `.agents/skills/*` 检出为纯文本占位文件而非目录链接。
5. 缺少根 `requirements.txt` 或工作区增减了格物域，需要按当前实际存在的域动态汇总依赖。

## 快速使用

**macOS**：
```sh
sh GewuInitTools/Gewu_init_mac.sh
```

**Linux**：
```sh
sh GewuInitTools/Gewu_init_linux.sh
```

**Windows (CMD)**：
```bat
GewuInitTools\Gewu_init_win.cmd
```

常用参数（三平台通用）：

- `--check-only`：只做体检与核验，不写入或修改工作区。
- `--workspace <dir>`：指定目标工作区根目录（默认自动识别 `GewuInitTools` 的上级目录）。
- `--force-download-python`：强制下载对应平台/架构的独立 CPython 到 `.runtime/install-env/python` 并基于它建 venv。
- `--python <path>`：显式指定本机 Python 3.9+ 解释器。
- `--python-tar <file>` / `--python-url <url>` / `--python-sha256 <hex>`：使用本地或自定义镜像站独立 CPython 压缩包。
- `--offline --wheels-dir <dir>`：离线安装依赖包。
- `--reinstall`：重建 `.runtime/install-env/venv` 并重装全部域依赖。
- `--skip-skills`：跳过 `.agents/skills` 链接同步。
- `--skip-git-check`：缺省 `git` 时降级为警告而不阻断初始化。
- `--skip-git-init`：工作区尚未建仓时跳过自动 `git init`。
- `--json`：输出机器可读的 JSON 检测与安装回执。

## Git Clone 后的完整启用说明

1. 运行本工具后，**基础 Python、隔离 venv、各域 Python 依赖、`gewu.command` / `gewu.cmd`、`.agents/skills` 与各域 `capabilities` 均已就绪**。
2. **业务数据卷（`Volumes/`）与密钥不随 Git 仓库分发**：首次使用业务域前仍需按各域规则建卷初始化（例如 `GewuAtlas` 执行 `atlas vault-create` 并在生成 KEK 后新开对话；`GewuTicket` 执行 `ticket init-volume`；`GewuForge` 开发任务按 `forge-initialize` 建 Task）。
3. **跨平台 Git 建议**：建议在仓库根 `.gitattributes` 固定 `*.sh` / `gewu` / `gewu.command` 为 `eol=lf`、`*.cmd` 为 `eol=crlf`，并将本地生成的 `INSTALL_RECEIPT.json` 或平台启动器按需纳入 `.gitignore` 以保持工作区 `git status` 干净。
4. **原生验收边界**：`Gewu_init_mac.sh` 已在 macOS (`arm64`) 实测通过；`Gewu_init_linux.sh` 与 `Gewu_init_win.cmd` 需在各自目标真机上运行完成原生实装验收。
