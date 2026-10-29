# 贡献指南

本文档说明本仓库的协作约定。当前落地 Bug Issue 关闭审计案例：Python 服务端直接复用 gitcode-cli 服务层，Go CLI 调用管理 API。

## 可以贡献什么

- 缺陷报告与修复
- 新能力与改进建议
- 文档补充与纠错
- 规则与配置的完善

## 提问与反馈

- 提交前先搜索已有 Issue，避免重复。
- 请选择对应模板填写：缺陷报告 / 功能需求 / 使用咨询。空白 Issue 已关闭。
- 表单会自动带上类型标签，优先级与范围由维护者在分诊时补充，含义见 [标签规范](docs/labels.md)。

## 分支约定

- `main` 为受保护分支：禁止强制推送与删除分支，合并需经 Pull Request 且至少 1 人审批。
- 仓库管理员当前可以绕过上述限制（`enforce_admins: false`），这是启动期的临时设置，收紧后本文档会同步更新。
- 从 `main` 拉出短分支开发，合并后删除：

```
feat/<简短描述>
fix/<简短描述>
docs/<简短描述>
chore/<简短描述>
```

## 提交信息

格式：`type(scope): subject`

| 部分 | 说明 |
| --- | --- |
| type | `feat`、`fix`、`refactor`、`docs`、`test`、`chore`、`ci` |
| scope | 模块或目录名，如 `triage`、`deploy`、`docs` |
| subject | 祈使句，中文或英文均可，尽量控制在 50 字符内 |

示例：

```
feat(triage): 按日志关键字识别组件
fix(webhook): 重复投递时不再重复评论
docs(readme): 补充本地运行说明
```

## Pull Request

- 按 PR 模板填写：变更说明、关联 Issue、变更类型、验证方式、自查清单。
- 一个 PR 只做一件事；改动较大时先开 Issue 讨论，避免返工。
- 收到评审意见后直接追加提交，不需要 force push 压平历史。
- 合并方式（merge commit / squash）待定。

## 代码与文档

- Python 3.11+，Go 1.22+；开发安装见[关闭审计使用说明](docs/bug-close-audit.md)。
- Python 检查：`.venv/bin/python -m pytest`、`.venv/bin/python -m ruff check src tests`、`.venv/bin/python -m ruff format --check src tests`。
- Go 检查：`go test ./...`、`go vet ./...`、`gofmt -d cmd/msbot`。
- GitCode 平台路径、分页和通用服务缺项在上游补齐；本仓库不复制平台 REST 实现。
- 文档以中文为主，专有名词保留英文原文；行为变更需同步更新 README 或 `docs/`。

## 标签

- 新建 Issue 会自动获得类型标签；优先级、范围、状态标签由维护者补充。
- 完整定义、判定标准与状态流转见 [标签规范](docs/labels.md)。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。提交即表示同意你的贡献以该许可证发布。

## 待补充

- [ ] 行为准则（Code of Conduct）
- [x] 开发环境与依赖安装
- [x] 测试与静态检查命令
- [ ] 合并方式与发布流程
