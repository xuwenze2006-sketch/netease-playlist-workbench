# 网易云歌单工作台

面向 Windows 的本地歌单工具，使用 Python 服务和 React 界面，提供已保存歌单浏览、歌曲搜索、分类结果查询及任务进度管理。页面和数据保存在本机，服务只监听 `127.0.0.1`。

这是独立开发的个人项目，**不是网易云音乐官方产品，与网易公司没有隶属或背书关系**。源码按 [MIT 许可证](LICENSE) 发布；第三方工具与服务适用各自的许可和使用条款。

## 能做什么

| 功能 | 当前范围 |
| --- | --- |
| 本地工作台 | 浏览已保存的歌单、歌曲明细、分类标签与判断依据，支持搜索、筛选、分页和任务记录。首次启动没有个人记录时显示空状态。 |
| 桌面缓存只读桥 | 读取本机网易云桌面客户端缓存，查询歌单与歌曲，生成本地候选清单，并提供 MCP stdio 工具。 |
| 主动在线读取 | 配置官方 CLI 并授权后，读取账号歌单目录、红心清单或指定歌单明细。普通启动不会自动联网读取账号。 |
| 名称整理 | 使用源码中的既定规则生成预览；用户主动执行后，核对归属与成员，改名并读回确认。规则仍带有项目原有的定制内容。 |
| 歌手精选创建 | **仅支持历史固定的五个歌手及数量，并要求私有批准文件与精确歌曲顺序匹配。尚不是任意账号可直接使用的通用创建功能。** |
| 分类批次 | 展示已有的兼容分类记录；执行脚本需要另行准备的完整计划、逐曲判断与批准记录。仓库不包含个人分类数据，也没有通用的一键自动分类入口。 |
| 分类复核与修正 | 筛选证据冲突、低把握和泛化依据，查看 50 首诊断试点；逐曲保存或撤销本地修正，比较原标签与草稿，并预览歌单增删数量。 |

本工具不下载歌曲、不进行完整音频听辨。目前不支持账号歌单列表整体排序，也不能指定新建歌单为私密；官方默认可见性可能公开。

已有兼容分类资料时，可在“分类成果”中展开“修正分类”，补充理由及录音版本。本地草稿保存在 `.organizer/classification-draft.json`，不会直接修改线上歌单或原分类报告。来源改变或并发保存冲突时需要重新核对；模型自评分不是准确率。诊断与六类场景的收录条件见[分类质量说明](docs/classification-quality.md)。

## 首次运行

准备 **Windows、Python 3.12、Node.js 24.15 或更新的 24.x 版本（含 npm）**，并确认 `python`、`node`、`npm` 已加入 PATH。Windows 推荐安装 Edge，启动器优先打开独立应用窗口。无需安装 Codex；Python 后端使用标准库，无需额外 `pip install`。

下载或克隆源码后，在项目根目录打开 PowerShell：

```powershell
python --version
node --version
npm --version

Push-Location frontend
npm ci --ignore-scripts
npm run build
Pop-Location

python -X utf8 .\run_organizer.py gui
```

`npm ci` 需要联网下载前端依赖。构建成功后，日常也可双击 `启动歌单整理.cmd`。`frontend/dist/` 不随源码提交，所以新下载的项目必须先构建；启动入口不会自动安装依赖。

打开工作台只检查本地接入状态并显示已有资料，不自动扫码、不继续历史任务，也不自动修改账号。没有官方 CLI 时仍可打开页面；没有保存记录时显示空状态。读取桌面缓存需要本机网易云客户端已产生可用缓存。

## 可选：接入官方账号

只有需要在线读取或受支持的账号操作时，才安装官方 CLI。**当前固定版本 `@music163/ncm-cli@0.1.7` 的依赖审计仍有 7 项已知告警**；安装前请阅读 [安全说明](SECURITY.md)，其中记录审计日期、影响范围及处理限制。

在项目根目录运行：

```powershell
.\scripts\install-official-cli.ps1
python -X utf8 .\run_organizer.py doctor
```

安装脚本需要联网，只将明确版本的官方工具安装到项目的 `.tools/ncm-cli/`。官方工具资料见 [NetEase skills](https://github.com/NetEase/skills)。

1. 按[网易云开放平台申请流程](https://developer.music.163.com/st/developer/apply/account?type=INDIVIDUAL)取得 App ID 和私钥。
2. 在工作台“接入设置”保存 App ID 与 UTF-8 私钥文件；凭证由官方 CLI 保存到项目隔离的 `.organizer/cli-home/`。
3. 主动生成扫码授权，用网易云手机应用确认。等待窗口为五分钟，过期后需重新获取。
4. 在“概览”选择快速名称清单，再点击“更新歌单清单”；需要歌曲候选时才选择完整红心清单。单个歌单的明细可在详情抽屉中主动读取。

请先检查预览与功能范围，再执行任何账号修改。接受默认可见性不会替代歌手精选所需的批准记录；公开源码中的历史规则不构成对新账号的执行批准。

## 数据与恢复

- 本地记录、草案、账号执行意图和收据保存在 `artifacts/`；配置、锁及浏览器资料位于 `.organizer/`。这些目录属于个人数据，均不应上传。
- 页面中的“本地记录”和“上次核验”表示已保存的历史资料，不代表当前在线账号状态。缺失歌曲和元资料会明确标记。
- “暂停”在当前请求完成并读回核对后停止；关闭忙碌窗口也会请求暂停。网络超时只代表前端停止等待，不代表后台没有执行。
- 提交接受状态不明、写入结果未知或记录保存失败时，后续修改保持保护。先刷新任务状态并核对，程序不会自动重发写入。存在有效名称意图时，可主动执行“只读核对上次改名”。
- 不要删除执行意图、收据或运行中的锁文件来绕过保护。备份与迁移时应保留完整记录；私钥、Cookie、令牌和原始账号报告不能作为 Issue 附件。

## 命令行与 MCP

以下命令不发送账号修改：

```powershell
python -X utf8 .\run_organizer.py doctor
python -X utf8 .\run_organizer.py plan
python -X utf8 .\run_bridge.py status
python -X utf8 .\run_bridge.py playlists --kind owned
python -X utf8 .\run_bridge.py search --query "示例歌手"
python -X utf8 .\run_bridge.py mcp
```

`plan` 会读取本地缓存并保存候选清单。桌面桥默认读取 `%LOCALAPPDATA%\NetEase\CloudMusic`，可用全局选项 `--data-dir` 指定其他缓存目录；它在临时副本上执行 SQLite 只读事务。

MCP 工具有 `desktop_status`、`list_playlists`、`get_playlist_tracks`、`search_tracks`、`preview_artist_playlist`。配置方式见 [MCP 示例](codex-mcp.example.toml)，需按本机安装位置填写路径，不会自动修改全局配置。其他命令与开发注意事项见 [开发文档](docs/development.md)。

## 本地验证

完成前端依赖安装后，在项目根目录运行：

```powershell
python -X utf8 .\scripts\verify_offline.py
```

它顺序运行后端回归、前端回归和生产构建，不安装依赖，不登录或调用真实账号；结果和日志保存在被忽略的本地输出目录。失败会返回非零。测试通过不等于真实账号操作或浏览器视觉验收通过。

真实浏览器的临时假账号验收、项目结构和贡献前检查见 [开发文档](docs/development.md)。报告问题时请提供复现步骤、运行版本及脱敏错误信息；安全相关问题请先阅读 [SECURITY.md](SECURITY.md)。
