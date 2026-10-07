# 开发与验证

本项目当前以 Windows 为主要运行环境。公开仓库包含程序、测试和示例配置；个人账号数据、运行环境、构建产物及私人工作记录不属于源码分发内容。

## 环境准备

使用 Python 3.12、Node.js 24.15 或更新的 24.x 版本及 npm。Python 后端使用标准库；前端依赖由 `frontend/package-lock.json` 固定。默认界面不要求 Tk，运行旧 `tk-gui` 入口才需要 Python 安装包含 Tk 支持。

在项目根目录打开 PowerShell：

```powershell
Push-Location frontend
npm ci --ignore-scripts
npm run build
Pop-Location
python -X utf8 .\run_organizer.py gui
```

`frontend/dist/` 被 Git 忽略，新检出和前端修改后都要重新构建。后台会固定加载同一版源码与页面快照；旧后台有任务或待核对写入时不会自动交接，请先处理页面中的提示。

`npm run dev` 只启动 Vite 前端开发服务器。完整工作台依赖 Python 注入的页面会话及同源 API，不能把 Vite 页面单独启动当作完整接入验证。普通运行使用生产构建和 Python 本地服务。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `frontend/src/` | React 界面、请求封装、响应校验与组件回归。 |
| `netease_organizer/` | 官方 CLI 适配、计划、执行与恢复、本地 HTTP 服务及界面数据投影。 |
| `netease_bridge/` | 桌面缓存只读适配、命令行及 MCP stdio 协议。 |
| `run_organizer.py` | 工作台与显式整理命令入口。 |
| `run_bridge.py` | 只读桥与 MCP 入口。 |
| `scripts/` | 安装、验证、临时模拟服务及高级分类工具。运行前应区分脚本用途。 |
| `tests/` | Python 回归与模拟账号验证。 |

前端 `api.ts` 校验读取结果，`App.tsx` 管理工作台状态与操作意图。`web_server.py` 负责会话和请求边界；账号执行保护在服务与执行器中再次检查，不能仅依赖按钮是否禁用。

## 本地回归

推荐统一入口：

```powershell
python -X utf8 .\scripts\verify_offline.py
```

它顺序执行 Python 回归、前端回归和构建，将每轮日志与结果写入 `artifacts/offline-verification/`。不安装工具，也不请求真实账号操作。应检查本轮退出码和报告，不能沿用旧报告声称通过。

也可分别运行：

```powershell
python -X utf8 -m unittest discover -s tests -v
Push-Location frontend
npm test
npm run build
Pop-Location
```

前端测试使用 jsdom 和模拟请求，最多两个工作进程，单项执行上限 15 秒。这是功能回归的等待预算，不是页面响应速度指标。Python 测试使用临时文件、数据库和受控 CLI 替身；部分平台能力会按环境跳过。改动恢复规则时必须保留未知结果、记录落盘失败、重复提交、重启和账号身份冲突等测试。

## 真实浏览器的模拟验收

浏览器验收使用独立临时项目和假账号。需要本机 Edge 或 Chrome，以及已经安装的 **`@playwright/cli@0.1.22`**。它与账号接入使用的官方 CLI 是两套独立工具。

可显式安装到被忽略的项目工具目录，再指定包位置：

```powershell
npm install --prefix .tools/browser-verification @playwright/cli@0.1.22 --ignore-scripts --no-audit --no-fund
$playwrightCli = Join-Path $PWD '.tools/browser-verification/node_modules/@playwright/cli'
python -X utf8 .\scripts\verify_web.py --cli-path $playwrightCli --browser msedge
python -X utf8 .\scripts\verify_classification_workbench.py --cli-path $playwrightCli --browser msedge
python -X utf8 .\scripts\verify_classification_quality_browser.py --cli-path $playwrightCli --browser msedge
```

安装命令需要联网；验收脚本自身不会自动安装软件或浏览器。始终显式传入 `--cli-path`，不要依赖某台开发机的 npm 临时缓存位置。使用 Chrome 时将 `--browser` 改为 `chrome`。

| 脚本 | 主要场景 |
| --- | --- |
| `verify_web.py` | 创建过程中的暂停、手动续做、完成后防重放及关窗暂停。 |
| `verify_classification_workbench.py` | 分类查询、标签与歌单筛选、分页、导航恢复和显式刷新。 |
| `verify_classification_quality_browser.py` | 复核筛选、本地修正与版本理由、差异预览、重载保留、撤销和窄屏编辑。 |
| `verify_recovery.py` | 缺少接入环境、账号冲突及未知写入结果的界面保护。 |
| `verify_restart.py` | 重启后的未决操作保护及主动只读核对。 |
| `verify_authorization.py` | 未决名称操作下重新授权与只读核对。 |
| `verify_details.py` | 歌曲详情、搜索、元资料筛选、键盘焦点与短窗口布局。 |
| `verify_playlist_read.py` | 指定歌单的完整读取、部分读取与在途暂停。 |

这些脚本的模拟账号报告不能证明真实服务可用性、真实写入结果或第三方计费状态。应顺序运行浏览器验收，避免多套浏览器和完整测试同时争用资源；失败后保留本轮报告，再定位错误阶段。截图只使用模拟数据，发布前检查是否仍包含本机路径或其他私人信息。

`verify_local.py` 和 `verify_organizer.py` 另有读取本机真实缓存或本地接入工具的检查，不属于默认的临时假账号浏览器验收。不要将它们不加区分地加入公共 CI。

## 命令与功能边界

命令参数以入口的帮助为准，查看帮助不会初始化账号操作：

```powershell
python -X utf8 .\run_organizer.py --help
python -X utf8 .\run_bridge.py --help
```

| 入口 | 行为 |
| --- | --- |
| `run_organizer.py gui` / `tk-gui` | 默认现代工作台 / 显式旧 Tk 界面。普通启动不主动授权。 |
| `doctor` / `plan` | 本地接入检查 / 从桌面缓存生成离线候选清单。 |
| `online-plan --names-only` / `online-plan` | 经授权读取在线目录 / 完整红心清单，需要联网。 |
| `login` / `login-status` / `discover` | 主动授权、在线授权检查、取得动态命令定义。 |
| `execute-renames` | 按既有规则执行账号改名，不是纯预览。应先检查清单。 |
| `reconcile-renames` | 对有效未决名称意图只读核对，不重发改名。 |
| `execute-artists --accept-default-visibility` | 仅执行固定历史批准范围；标志不绕过身份、候选顺序及记录检查。 |

`service.py` 的 `_APPROVED_ARTIST_COUNTS` 仍绑定五个历史目标及数量，执行还需要本机私有批准文件。`planning.py` 包含固定名称与分类顺序规则。这些是已知的定制边界，不能在文档或 UI 中描述成任意账号通用的自动整理服务；修改前应先设计独立的预览与批准流程，并保留已有执行记录的解释方式。

高级分类工具同样需要独立的数据准备和批准：`build_classification_plan.py` 消费本地基线、逐曲判断与语言复核；它不会自动为新曲库产生模型判断。`execute_classification.py` 默认仅预览已有计划，`--execute-approved` 或 `--resume-verified` 才会请求账号执行。不要把生成的计划或历史“已批准”字段视为新用户已经授权的证明，也不要将真实账号执行命令放入测试或 CI。

## 提交前检查

1. 先通过与改动相关的回归，再运行统一验证；涉及布局、焦点或窗口生命周期时补对应浏览器验收。
2. 用 `git status --short` 和将要提交的差异确认范围。`artifacts/`、`.organizer/`、`.tools/`、浏览器资料、依赖和构建产物均应保留在本机。
3. 文档与示例不得包含真实账号标识、私钥、Cookie、授权链接、个人曲库快照或某台电脑的绝对路径。模拟数据应明确可辨识。
4. 不通过删除意图、收据或锁文件让测试“通过”，也不放松账号匹配或写后核验以扩大兼容范围。
5. 依赖升级单独验证。当前官方 CLI 版本的已知风险与披露规则见 [安全说明](../SECURITY.md)，项目许可证见 [MIT](../LICENSE)。
