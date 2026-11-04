# MindStudio Bot

> 面向 Ascend MindStudio 社区（GitCode）的 Issue / PR 自动化协作机器人。

**当前实现**：Bug Issue 关闭审计案例。接入已认证 Webhook，并在仓库默认分支放置 `.msbot/config.yaml` 即可使用，无需预先登记仓库。Python 服务端直接 import `codeasier/gitcode-cli` 服务层；Go CLI 提供结果查询及原有接入管理，SQLite 持久队列支持长期值守。

---

## 项目简介

本仓库用于构建一个运行在 Ascend MindStudio 社区仓库（GitCode）上的自动化机器人，负责处理 Issue、PR 等协作流程中的自动化事项。

## 背景与要解决的问题

> 待补充。

## 功能范围

被纳管 GitCode 仓库中的 `.msbot/config.yaml` 启用关闭审计并引用该仓库的 Issue Forms YAML 模板。含 `bug` 标签的 Issue 被新增 `resolved` 时：

1. 完整读取评论，检查是否至少有一条单独符合关闭模板的登记。
2. 合格则保留标签；不合格只移除 `resolved`，保留其他标签。
3. 可选发送飞书机器人通知，支持自定义安全关键词和签名。
4. 持久记录投递、审计、删标及通知结果，防止重复投递重复执行。

本仓库的 `examples/managed-repo/` 提供被纳管仓库的接入样例；将其中的 `.msbot/` 放入目标仓库根目录。Bot 服务的实例配置参见 [examples/instance.yaml](examples/instance.yaml)，仓库配置与模板通过 GitCode 从被纳管仓库读取。

示例及运行入口：
- [被纳管仓库配置示例](examples/managed-repo/.msbot/config.yaml)
- [Bug 关闭模板示例](examples/managed-repo/.msbot/template/bug-close.yml)
- [关闭登记评论示例](examples/bug-close-comment.md)
- [安装、接入、飞书和长期运行说明](docs/bug-close-audit.md)

## 快速开始

需要 Python 3.11+、Go 1.22+。当前需同时安装本次补充了服务接口的上游 `gitcode-cli` 工作区；该补充尚未发布，详见[依赖说明](docs/bug-close-audit.md#2-开发安装与上游依赖)。

上游依赖：[codeasier/gitcode-cli#161](https://github.com/codeasier/gitcode-cli/pull/161)。

```sh
uv venv --python 3.12 .venv
uv pip install -e '../gitcode-cli[dev]' -e '.[dev]'
go build -o ./msbot ./cmd/msbot
```

按[使用说明](docs/bug-close-audit.md)配置实例 `gitcode` 默认凭据、HTTPS 入口并启动服务。仓库接入只需：

1. 在 GitCode 配置 Webhook，URL 为 `https://<Bot 域名>/hooks/gitcode`，选择 Issue 事件、JSON，并填写与实例一致的密码或签名密钥。
2. 在仓库默认分支提交有效的 `.msbot/config.yaml` 及其中引用的模板，启用所需审计。

首次回调先按本地记录受理并返回 202，无需执行 `msbot repo add`。未登记仓库由后台单个 worker 再核对 GitCode；核对失败只忽略该任务，不改标签、不发通知。配置缺失或任务未启用时跳过处理。统一入口已通过本地回归、上游 HTTP 服务联调及部署后的公网认证／持久受理／防重探测；未预登记仓库的真实 GitCode 首次回调仍待验收。

## 参与贡献

- 开发者规范：[CONTRIBUTING.md](CONTRIBUTING.md)
- 标签规范：[docs/labels.md](docs/labels.md)

## 待补充内容

- [ ] 背景与要解决的问题
- [x] 首个案例范围
- [x] Python 服务端、Go CLI 与上游能力边界
- [x] 首个案例使用与配置说明
- [ ] 路线图

## 许可证

本项目采用 [MIT License](LICENSE)。

## 相关链接

- Ascend MindStudio 社区：<https://gitcode.com/Ascend>
