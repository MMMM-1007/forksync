# ForkSync

GitHub fork **自动同步**工具：WebUI 里贴链接就能管一批fork，到期自动帮你把上游的更新合过来。

> 核心做法：调用 GitHub 官方 API 的 `merge-upstream`（就是网页上那个 **Sync fork** 按钮背后的接口）在**服务端**完成快进/合并。
> 所以：**不需要本地 clone 仓库、不需要装 git、不消耗本机磁盘**，只用一个 token 就能批量维护几十上百个 fork。

---

## 快速开始

Windows 双击 `start.bat`；其他平台：

```bash
python app.py            # 默认 http://127.0.0.1:8770
python app.py --port 9000 --open
./start.sh               # macOS / Linux
```

只依赖 Python 3.9+ 标准库，**不用 pip install 任何东西**。

打开 <http://127.0.0.1:8770>，点右上角 **⚙ 设置** 填 token，然后把 fork 链接贴到「添加 fork」里就行。

### 配置 token

生成地址：<https://github.com/settings/tokens>（Fine-grained 或经典 PAT 都行）

| 场景 | 所需权限 |
| --- | --- |
| 只同步公开仓库的 fork | `public_repo` |
| 含私有仓库的 fork | `repo` |

未配置 token 也能用，但只能读公开仓库，且匿名 API 限流只有 **60 次/小时**；配了 token 是 5000 次/小时。

> 也可以完全不落盘：`set FORKSYNC_TOKEN=ghp_xxx` 再启动，界面会显示「由环境变量提供」，配置文件里就不会有明文 token。

---

## 添加 fork

「添加 fork」输入框接受这几种写法，都会自动解析：

```
https://github.com/你的名字/某仓库
git@github.com:你的名字/某仓库.git
你的名字/某仓库
```

添加时会实时读取该仓库信息：

- 不是 fork → 直接拒绝（提示先去 GitHub fork）
- 是 fork → 自动填出上游仓库、默认分支，并立刻算出**落后/领先**多少提交

## 三种同步策略

| 策略 | 行为 | 适用 |
| --- | --- | --- |
| **ff**（默认，推荐） | 只做快进；一旦你在 fork 上有自己的提交就**拒绝**并提示 | 纯跟随上游，不动代码 |
| **merge** | 允许产生合并提交，把上游合进你的分支 | 自己在 fork 上有改动，但愿意保留 |
| **force** | 把分支**强制指向**上游 HEAD，**丢弃你在该分支上的独有提交**（执行前有二次确认） | 想彻底回到上游状态 |

同步冲突（上游和你的改动撞在同一处）时 GitHub 会返回 409，工具会明确提示你去网页手动解决，或改用 force。

## 删除保护（重要）

**同步是双向的**：上游不只新增文件，也会删除文件。上游删了，你 fork 里的同名文件**也会跟着消失**——如果那个文件是你 fork 之后才拿到的、或者你指望留着，更新完就找不回来了。

所以每次同步前，工具会先算一遍「这次会删掉哪些文件」，并按配置处理：

| 模式 | 行为 |
| --- | --- |
| **warn**（默认） | 照常同步，但把被删文件写进日志；列表里显示红色的「✗ 删 N」徽章，点开能看清单 |
| **block** | **拦下来，不执行**。弹窗列出会被删的文件，你确认后才真正同步 |
| **off** | 不检查，直接同步（旧行为） |

**保护名单**是更强的保险：按正则写一批路径，只要它们出现在「上游要删除」的清单里，**无论模式选什么都会被拦下**。

```
\.md$          所有 markdown 文档都不许被同步删掉
^config/        config 目录下的任何文件
\.env|Dockerfile
```

> 注意：API 模式的检测依赖 GitHub 的 compare 接口，它**单次最多返回 300 个文件**。超过时工具会在日志里标注「列表可能被截断」，不会假装看全了。

判断依据是 `base=fork_sha, head=upstream_sha` 这个方向的差异——只有这样，`removed` 才准确表示「上游删了、同步后你也会没有」的文件。

## 状态说明

列表里每个 fork 会显示：

- **落后 ↓ / 领先 ↑** —— 与上游的提交差（`ahead_by` / `behind_by`）
- 状态徽章：`已最新` / `落后` / `领先` / `已分叉`（两边都有独有提交）/ `未知` / `错误`
- 每个动作都会写进右侧「同步日志」，可按级别过滤

## 自动同步

在设置里勾上 **开启定时自动同步** 并设定间隔（1 分钟 ~ 7 天）：

- 定时器跑在服务进程内，**关掉浏览器窗口不影响**，进程退出才停
- 每个周期会把所有「参与全部同步」的项目走一遍：已是新的跳过，落后的合并
- 想让开机就常驻：

```bat
:: Windows：登录时静默启动
schtasks /create /tn ForkSync /tr "\"C:\Path\python.exe\" \"<安装目录>\app.py\"" /sc onlogon
```

```ini
# Linux：~/.config/systemd/user/forksync.service
[Unit]
Description=ForkSync
[Service]
ExecStart=/usr/bin/python3 /opt/forksync/app.py
Environment=FORKSYNC_TOKEN=ghp_xxx
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now forksync
```

## 本地 git 模式（可选）

API 模式的局限：**不会同步 tag，也不会同步其他分支**（`merge-upstream` 只处理你指定的那一个分支）。

如果这些你都要，在项目的「设置」里填 **本地克隆路径**，工具就改走本地 git：

```bash
git fetch upstream --tags --prune
git checkout <分支>
git merge --ff-only upstream/<分支>     # 或 --no-edit / reset --hard（按策略）
git push origin HEAD:refs/heads/<分支>
git push origin --tags                   # 勾了「同时推送 tags」才做
```

前提：本机装了 git，并且仓库已经 clone 到那个路径。没有 git 时 API 模式完全不受影响。

---

## 数据与文件

```
forksync/
├── app.py                # 服务端（HTTP + GitHub 客户端 + 同步引擎 + 定时器）
├── webui/index.html      # 单页 WebUI（无构建、无依赖）
├── start.bat / start.sh  # 启动脚本
├── config.json           # token + 项目列表（首次运行自动生成，权限 600）
├── logs/sync.jsonl       # 同步日志，超过 8MB 自动轮转
└── tests/                # 离线自测：假 GitHub API + 全流程断言
```

环境变量：

| 变量 | 作用 |
| --- | --- |
| `FORKSYNC_TOKEN` | 提供 token（优先于配置文件，适合不想落盘明文） |
| `FORKSYNC_HOME` | 存放 `config.json` 与日志的目录，默认是程序目录 |
| `FORKSYNC_API_BASE` | 替换 API 地址（自测/GitHub Enterprise 用） |

## 安全建议

- 默认只监听 `127.0.0.1`，别人访问不到；token 只写在本机 `config.json`，界面回显永远打码。
- 确实要放到局域网/公网时再加访问密钥：

```bash
python app.py --host 0.0.0.0 --port 8770 --access-key 你的密钥
```

此时 API 需要 `X-Access-Key`，网页会自动弹框要密钥（存在浏览器 localStorage）。

- 建议用 Fine-grained token，**只勾选你确实要同步的那些仓库**的 Contents 写权限，而不是给全账号 `repo`。

---

## 常见问题

**同步报 403 / rate limit？**
没配 token，或这小时请求用完了。配置 token 后上限 5000 次/小时。

**同步报 `refusing to allow ... without workflow scope`？**
上游这次带上了 `.github/workflows/` 里的文件，而你的 token 没有 `workflow` 权限——GitHub 规定这类文件必须额外授权（因为它能在你仓库里跑代码）。到 <https://github.com/settings/tokens> 勾上 `workflow` 后**重新生成** token，填回设置即可。
在设置里点「验证 token」会提前告诉你有没有这个权限。

**上游删了文件，我的 fork 会跟着删吗？**
会。所以默认开着删除保护（warn 会预警、block 会拦下）。不想让某类文件被同步删掉，把它们写进「保护名单」。

**提示「同步冲突」？**
你 fork 分支上的提交和上游改到了同一处。去 GitHub 网页解决，或者把策略改成 force（放弃自己的提交）。

**提示「分支已分叉，ff 策略不合并」？**
你在 fork 上改了东西。想要合并提交就选 merge，想彻底跟随上游就选 force。

**tag / 其他分支没同步？**
API 模式只同步指定分支。需要 tag 和其他分支就用「本地 git 模式」。

**上游仓库改名或删除了？**
改名一般有重定向；上游转私有或删除后 fork 会脱离 fork 网络，此时不能再同步，工具会报错提示。

**同步了 fork，我提的 PR 会跟着更新吗？**
不会。PR 的 base 是上游仓库，只有你往 PR 那个分支上推新提交才会动。

**「不是 fork」的仓库能不能跟上游？**
不能，只能设为 template 或先 fork。

## 离线自测

不联网、不碰真实仓库，用内置的假 GitHub API 把全流程跑一遍：

```bash
python tests/run_tests.py          # API 模式：76 项
python tests/run_tests_local.py    # 本地 git 模式：32 项
```

覆盖：添加解析、状态计算、ff 快进、已最新跳过、409 冲突、分叉拒绝、merge、强制对齐二次确认、
缺少 workflow 权限的提示、token 权限探测、**删除保护三种模式 + 保护名单 + 项目级覆盖**、
无 token 守卫、参数校验、批量接口、日志过滤、删除。

## HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 配置 + 项目列表 + 统计 + 定时器状态 |
| GET | `/api/logs?limit=&level=&project=` | 日志 |
| GET | `/api/token/test` | 校验 token 并返回剩余额度 |
| POST | `/api/settings` | token / 间隔 / 自动同步 / 默认策略 / 删除保护 / 保护名单 |
| POST | `/api/projects` | 新增（body: `input`, `strategy`） |
| PATCH | `/api/projects/{id}` | 策略 / 分支 / 本地路径 / 启用 / tags / 删除保护 |
| DELETE | `/api/projects/{id}` | 移除（不影响 GitHub 上的仓库） |
| POST | `/api/projects/{id}/check` | 刷新落后/领先，并算出待删文件 |
| POST | `/api/projects/{id}/sync` | 同步（可带 `strategy`、`allow_deletions` 绕过删除拦截） |
| POST | `/api/projects/{id}/force` | 强制对齐（需 `{"confirm":true}`） |
| POST | `/api/sync_all` / `/api/check_all` | 批量 |

## 和 GitHub Actions 方案比

| | ForkSync（本工具） | 上游仓库里的 sync workflow |
| --- | --- | --- |
| 需要什么 | 一个 token | 每个 fork 里都要有 workflow 文件 |
| 上游改了 workflow | 不受影响 | fork 里的 workflow 常被停用，容易失效 |
| 管理方式 | 一个界面管全部 | 逐个仓库看 Actions 日志 |
| token 存放 | 本机一份 | 每个仓库一份 secret |
| 运行位置 | 你的机器/服务器 | GitHub 云端 |

**想彻底无人值守、且机器会关机** → 用 Actions；**想集中管理、可观测、不受上游 workflow 影响** → 用本工具。
