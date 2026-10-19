# 本 fork 的更新与测试

代码保存在 [flower-forever/Vibe-Trading](https://github.com/flower-forever/Vibe-Trading)。`main` 包含官方版本和我们的研究审计工具、Cline / OpenCode 兼容修改。密钥、`.env`、会话、持仓、运行日志和本机依赖目录留在本机。

## 获取官方更新

打开 [Check official updates](https://github.com/flower-forever/Vibe-Trading/actions/workflows/sync-upstream.yml)，点击 **Run workflow**，使用 `main`。也会每天在北京时间 10:17 左右自动检查；GitHub 的定时执行可能延迟。

- 官方提交已经包含在我们的 `main` 中：记录“已是最新”，不重复测试。
- 有更新且可合并：创建独立分支和 PR，明确启动该分支的 **Fork verification**。
- 有冲突：创建草稿 PR，列出冲突文件，本次检查显示失败。解决冲突后将提交推入候选分支，再手动运行检查并勾选 `rerun_ci`。
- 候选分支已存在：保留人工修复；若该提交缺少显式启动的 CI，会补启动一次。已有失败结果每天不会自动重跑。需要重跑时勾选 `rerun_ci`。

检查结果在 PR 和 Actions 页面查看。确认后在 GitHub 点击合并；流程不会自动合并或强制推送。每一对“我们的基准提交 + 官方提交”使用独立分支，因此官方继续更新时可能出现多个候选 PR，保留最新、关闭旧 PR 即可。

## 自动测试覆盖

**Fork verification** 会在 `main` 更新、面向 `main` 的 PR 和手动启动时运行：

- Linux：后端完整单元测试（排除真实模型 E2E），包含 OpenBB、统计、Anthropic、Matrix、Slack 和 Telegram 可选依赖；同时检查内部文件、环境变量读法和同步脚本。
- 前端：按锁文件安装、生产构建、全部 Vitest 测试。
- Windows：我们的研究完整性工具、Cline、OpenCode 及任务持久化回归。

测试 XML 和日志作为 Actions artifacts 保存 14 天；同步报告保存 30 天。测试失败会显示红色，不通过忽略失败来制造通过结果。首次迁移前已发现的 CSV 错误文案和 Windows 并发文件替换问题也会继续暴露。

云端使用干净环境及测试替身，不需要本机模型密钥。它能验证代码和构建；本机真实模型连接、券商连接、个人数据仍需在相应配置变化时单独验证。

机器人使用每次任务自动获得的 `GITHUB_TOKEN`，只给同步任务写分支、创建 PR 和启动 Actions 的权限，测试任务只有读取权限。创建 PR 后显式调用 `workflow_dispatch`，避免依赖机器人事件的递归触发行为。参见 [GitHub 官方触发规则](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)。

本 fork 关闭继承的重复 CI、桌面发布、Docker 发布、Wiki 部署和线上数据源定时探测，保留源码中的上游工作流文件，便于后续合并。这些开关只作用于本 fork。

## 将已确认的版本拉回本机

先提交或暂存本机新增修改，再在 Vibe-Trading 仓库执行：

```powershell
git fetch origin
git merge --ff-only origin/main
```

更新成功后，按依赖变动安装依赖并重启应用。云端测试通过后，日常更新无需在本机再跑整套测试；如需手动重跑，打开 [Fork verification](https://github.com/flower-forever/Vibe-Trading/actions/workflows/fork-ci.yml)。
