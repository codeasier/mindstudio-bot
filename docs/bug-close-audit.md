# Bug Issue 关闭审计：安装与使用

这是当前唯一实现的业务案例。Python 服务持续接收 GitCode Issue Hook；Go CLI 管理接入和查询；平台请求直接复用 `gitcode-cli` 服务层。

## 1. 仓库侧配置

将 [.msbot/config.yaml](../.msbot/config.yaml) 和 [.msbot/template/bug-close.yml](../.msbot/template/bug-close.yml) 放入被管理仓库的默认分支：

```yaml
version: 1
audits:
  bug_issue_close:
    enabled: true
    template: .msbot/template/bug-close.yml
    # 配置实例侧飞书连接后可启用：
    # notification: engineering
```

模板参考用户提供的 `bug-close.yml`，保留原字段 ID、标题、类型、必填及选项。入口与模板从默认分支的同一完整提交读取；合法更新在下一次新审计开始时生效。非法更新保留上一有效包，入口确认缺失或 `enabled: false` 停止新审计。读取故障不会被当作入口删除。

### 登记评论

使用 [完整登记示例](../examples/bug-close-comment.md)，或从自定义模板生成空白登记：

```sh
msbot-server render-template .msbot/template/bug-close.yml
```

填写后作为 **一条 Issue 评论** 提交。字段标题必须保留为 `### 字段名称`，与模板 `attributes.label` 一致。一级／二级标题用于分组，字段内小标题使用四级或更深；仅有标题或空代码围栏不算填写内容。

- 必填字段不得为空、仅有填写说明，或保留 `_No response_` 等未填标记。
- input 为单行；textarea 支持多行、代码和图片 Markdown。
- dropdown 必须填写模板声明的值；单选只能填一个。
- checkboxes 使用 `- [x] 选项`／`- [ ] 选项`，选项必须在模板内；可选组可不勾选。
- description 中的 SHA／链接格式建议和“选择是时……”等自然语言说明，当前用于提示填写，不自动转换为额外校验规则。
- 至少一条评论独立合格即可；不把几条评论拼接，不以最新一条普通回复覆盖此前合格登记。

## 2. 开发安装与上游依赖

需要 Python 3.11+、Go 1.22+。本次上游服务补充位于与 Bot 并列的 `../gitcode-cli` 工作区，基于提交 `461806f8ce5455960a8ebffc259eb71cfb3b133d`。**这些新增接口尚未合入／发布，不能用当前 PyPI 的 pygitcode 0.1.6 替代。**

对应上游 PR 为 [codeasier/gitcode-cli#161](https://github.com/codeasier/gitcode-cli/pull/161)；开发安装时，`../gitcode-cli` 需要包含其 `feat/bug-close-audit-services` 分支上的服务补充。

在 Bot 工作区执行：

```sh
uv venv --python 3.12 .venv
uv pip install -e '../gitcode-cli[dev]' -e '.[dev]'
go build -o ./msbot ./cmd/msbot
```

上游工作包含 `IssueService.list_all_comments/remove_label`、`RepoService.get_branch`、`WebhookService` 和客户端资源释放。Bot 不包含平台 REST 实现。待上游合入／发布后，再固定正式依赖版本。

## 3. 实例配置与飞书

按 [实例样例](../examples/instance.yaml) 创建 `/etc/msbot/instance.yaml`。凭据使用实例进程的环境变量，不放入仓库。

```yaml
database: /var/lib/msbot/audit.sqlite3
public_url: https://msbot.example.com
admin_token_env: MSBOT_ADMIN_TOKEN
feishu:
  engineering:
    url_env: MSBOT_FEISHU_URL
    repositories: ["owner/repo"]
    keywords: ["Bug关闭审计"]
    secret_env: MSBOT_FEISHU_SECRET
```

飞书连接整体可省略。`repositories` 由实例管理员列出允许使用该连接的仓库；仓内引用不能扩大此范围。未开启关键词或签名时，分别省略 `keywords` 或 `secret_env`。在仓库入口增加 `notification: engineering` 才选择该连接。

配置以下环境变量，值由部署者填写：

| 环境变量 | 用途 |
|---|---|
| `MSBOT_ADMIN_TOKEN` | 本案例管理 API 的实例管理员 Bearer Token |
| `MSBOT_GITCODE_TOKEN` | 接入账户的 GitCode Token；需能读仓库／评论、删除 Issue 标签；自动建 Hook 还需 Hook 管理权限 |
| `MSBOT_GITCODE_WEBHOOK_SECRET` | 为该仓库 Webhook 配置的密码或签名密钥 |
| `MSBOT_FEISHU_URL` | 可选，飞书自定义机器人完整 Webhook URL |
| `MSBOT_FEISHU_SECRET` | 可选，飞书机器人安全设置中的签名密钥 |

飞书消息正文包含配置的全部关键词。签名使用秒级 timestamp，加换行与 secret 作为 HMAC-SHA256 的 key，对空消息计算后 Base64；密钥不出现在消息正文或审计结果中。机器人 URL 和安全设置须与飞书侧配置一致。

本案例采用实例管理员引导接入；此前讨论的完整 OAuth／逐仓操作者管理产品尚未实现。

## 4. 启动与接入

开发时启动：

```sh
.venv/bin/msbot-server serve --config /etc/msbot/instance.yaml
```

默认监听 `127.0.0.1:8080`，外部 HTTPS 入口转发至此地址。启动一个服务进程即可；数据库锁阻止多个后台 worker 同时使用同一状态库。

在 CLI 环境设置 `MSBOT_URL`（本地默认 `http://127.0.0.1:8080`）和同一个 `MSBOT_ADMIN_TOKEN`，再执行：

```sh
./msbot repo add owner/repo
./msbot repo list
```

`repo add` 绑定仓库身份、加载配置并创建仅订阅 Issue 事件的 GitCode Webhook。默认密码模式；可用 `--signature` 选择 GitCode 签名密钥模式。其他仓库可用不同的实例环境变量：

```sh
./msbot repo add owner/another-repo \
  --token-env ANOTHER_GITCODE_TOKEN \
  --webhook-secret-env ANOTHER_WEBHOOK_SECRET
```

若手工配置 Hook，执行：

```sh
./msbot repo add owner/repo --manual-webhook
```

将返回的 `webhook_url` 填入 GitCode 项目设置 → WebHook，选择 Issue 事件和 JSON；填写与服务环境变量一致的密码。创建和手工接入方式不能用同一 operation_id 混用。

CLI 发起接入前在用户缓存目录的 `msbot/operations/` 保存操作 ID 和非敏感请求参数，stderr 同时显示 ID。可通过 `MSBOT_OPERATION_DIR` 改变目录。连接中断时查询原 ID：

```sh
./msbot operation get <operation_id>
```

同 ID 同参返回原操作，不再次创建 Hook；异参返回冲突。外部创建结果未知时保留 blocked，不能靠更换 ID 盲目重复接入。

## 5. 审计流程与结果

1. 校验 Webhook 密码／签名、项目路径和稳定项目 ID；持久保存投递后返回 202。
2. 只有 Issue update 的明确标签差异新增 resolved 才入队。同仓 delivery ID 或事件 uuid 重复均复用记录；同标识不同正文冲突。
3. 审计开始时绑定配置与模板快照；重新读取当前 Issue，缺少 bug 或 resolved 时忽略。
4. 完整分页读取评论，逐条验证模板；合格则保留标签。
5. 全部不合格时再次检查当前标签，使用专用接口只删除 resolved，并读取当前标签确认结果。
6. 配置飞书时发送通知，包含仓库、Issue 链接、模板路径及校验原因。

查询最近 50 条记录或指定记录：

```sh
./msbot task list
./msbot task get <task_id>
```

| status | 含义 |
|---|---|
| `queued`／`running` | 已持久接收／正在审计 |
| `passed` | 找到合格登记，保留 resolved |
| `rejected` | 登记不合格，已确认标签移除；通知结果另见 notification |
| `ignored` | 不是触发事件、任务未启用，或当前标签不再适用 |
| `failed` | 配置／读取等确定错误，没有据此执行删标 |
| `blocked` | 标签写入或通知结果不确定，保留记录，不自动重发 |

飞书结果分别记录 `none`、`sending`、`sent`、`failed`、`unknown`。HTTP 200 但飞书 code 非零不算成功。通知失败不恢复标签，也不重复删标。`GET /healthz` 检查后台 worker 是否存活。

重启恢复只重新排队尚未发出外部写调用的工作；调用在途阶段保留未知结果。当前未提供强制重发／通用任务重试入口，先通过查询核对原操作和平台事实。

## 6. Linux 长期运行

生产环境从两个本地源码工作区构建 wheel，并作为同一发布包安装，避免 editable 安装引用开发者 home：

```sh
uv build ../gitcode-cli --wheel --out-dir ./dist
uv build --wheel
```

在目标主机的 `/opt/msbot/current/.venv` 中一起安装两个 wheel 和运行依赖。将实例文件置于 `/etc/msbot/instance.yaml`，凭据环境文件置于 `/etc/msbot/secrets.env`，仅允许运维账户和服务读取。使用专用非 root `msbot` 用户。

将 [systemd 单元](../deploy/msbot.service) 安装至 `/etc/systemd/system/msbot.service`，配置已有 HTTPS 反向代理，然后：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now msbot
sudo systemctl status msbot
```

状态库放在 `/var/lib/msbot/`，服务重启不清理。备份应使用 SQLite 一致性备份工具；WAL 模式下不能只复制主文件作为运行中备份。

## 7. 已验证与待真实验收

离线测试覆盖真实上游服务的 HTTP 请求、超过一页的评论、合法／缺失／错误选项评论、精确删标、重复投递、跨项目拒绝、鉴权、配置回退、重启恢复、飞书签名与错误响应，以及 Go CLI 接入回执。

GitCode 官方 Issue Event 文档说明了 `changes`，但未提供标签更新的完整样本。当前实现严格识别 `changes.labels.previous/current`（元素为名称或含 name/title 的对象）；未知形状记录为 `unrecognized_label_change`，不凭当前 labels 推断添加动作。**部署验收需取得真实“添加 resolved”回调，核对该差异形状及密码／签名模式。**

本地测试不代表已经对真实仓库创建 Webhook、删除标签或发送真实飞书消息。

参考：
- [GitCode Issue Event](https://docs.gitcode.com/docs/help/home/org_project/webhook/webhook-stepup/issue-events)
- [GitCode Webhook 配置](https://docs.gitcode.com/docs/help/home/org_project/webhook/web-hook)
- [删除单个 Issue 标签](https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-issues-number-labels-name)
- [飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
