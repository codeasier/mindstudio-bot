# Bug Issue 关闭审计：安装与使用

这是当前唯一实现的业务案例。Python 服务持续接收 GitCode Issue Hook；仓库接入只需已认证 Webhook 和默认分支中的有效 `.msbot/config.yaml`，无需预先 `repo add`。Go CLI 提供查询和原有接入管理；平台请求直接复用 `gitcode-cli` 服务层。

## 1. 被纳管 GitCode 仓库配置

本仓库在 `examples/managed-repo/` 提供接入样例。将其中的 [配置示例](../examples/managed-repo/.msbot/config.yaml) 和 [关闭模板示例](../examples/managed-repo/.msbot/template/bug-close.yml) 复制到被纳管 GitCode 仓库，并提交到其默认分支，目标路径为：

```text
<被纳管仓库>/
└── .msbot/
    ├── config.yaml
    └── template/
        └── bug-close.yml
```

Bot 服务使用[实例配置](../examples/instance.yaml)运行，通过 GitCode 读取被纳管仓库的配置和模板。`examples/managed-repo/` 仅用于分发接入样例，配置中的模板路径相对于被纳管仓库根目录。

仓库配置示例：

```yaml
version: 1
audits:
  bug_issue_close:
    enabled: true
    template: .msbot/template/bug-close.yml
    # 只审计这些 GitCode 登录名提单的 Issue；省略则审计全部提单人：
    # authors:
    #   - alice
    # 配置实例侧飞书连接后可启用：
    # notification: engineering
```

`authors` 是仓库侧提单人名单，填写 GitCode 登录名，对应 Issue 详情里的 `user.login`，不是显示名 `user.name`，也不是后来添加 `resolved` 的操作者。名单 1 到 100 人，不区分大小写，不能重复。它不能单独扩大审计范围，有效名单是它与实例 `tester_authors` 的交集。交集之外的提单不读评论、不删除 `resolved`，任务记为 `ignored`（`author_not_allowlisted`）。读不到 `user.login` 时记为 `failed`（`author_unavailable`），同样不删标签。省略 `authors` 时审计实例测试名单中的全部账号。

模板参考用户提供的 `bug-close.yml`，保留原字段 ID、标题、类型、必填及选项。入口与模板从默认分支的同一完整提交读取；合法更新在下一次新审计开始时生效。非法更新保留上一有效包，入口确认缺失或 `enabled: false` 停止新审计。读取故障不会被当作入口删除。

### 登记评论

使用 [完整登记示例](../examples/bug-close-comment.md)，或在被纳管仓库的本地工作区从自定义模板生成空白登记：

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
gitcode:
  token_env: MSBOT_GITCODE_TOKEN
  webhook_secret_env: MSBOT_GITCODE_WEBHOOK_SECRET
  webhook_mode: password
tester_authors:
  - alice
feishu:
  engineering:
    url_env: MSBOT_FEISHU_URL
    repositories: ["owner/repo"]
    keywords: ["Bug关闭审计"]
    secret_env: MSBOT_FEISHU_SECRET
    # default: true
```

`gitcode` 定义统一入口 `/hooks/gitcode` 使用的默认账户与认证方式；`webhook_mode` 为 `password` 或 `signature`，默认 `password`。环境变量名可自定义，部署时必须提供对应值；省略 `gitcode` 整段时统一入口返回 503，已有带仓库 ID 的入口仍可使用。

`tester_authors` 是实例上的组织测试同学名单，填写 GitCode 登录名，1 到 500 人，不区分大小写。仓库启用关单审计后，这些账号提单必须审计。仓库若再配置 `authors`，有效名单是两边的交集：不在测试名单里的登录名不会被审计，测试名单里被仓库名单排除的账号也不会被审计。仓库省略 `authors` 时，有效名单就是完整的 `tester_authors`。

飞书连接整体可省略。实例按仓库名把仓库映射到连接；同一仓库只能映射一个连接。仓库配置中的 `notification` 仍可作为兼容字段，但实例映射优先生效。未映射且未配置 `notification` 的仓库不发通知。连接不保存 URL 或密钥，未开启关键词或签名时分别省略 `keywords` 或 `secret_env`。通知配置异常只跳过飞书发送，不阻塞审计和标签操作。

配置以下环境变量，值由部署者填写：

| 环境变量 | 用途 |
|---|---|
| `MSBOT_ADMIN_TOKEN` | 本案例管理 API 的实例管理员 Bearer Token |
| `MSBOT_GITCODE_TOKEN` | 默认账户的 GitCode Token；需能读目标仓库／评论、删除 Issue 标签；使用旧 CLI 自动建 Hook 时还需 Hook 管理权限 |
| `MSBOT_GITCODE_WEBHOOK_SECRET` | 统一入口的密码或签名密钥，在各目标仓库的 Webhook 中填写相同值 |
| `MSBOT_FEISHU_URL` | 可选，飞书自定义机器人完整 Webhook URL |
| `MSBOT_FEISHU_SECRET` | 可选，飞书机器人安全设置中的签名密钥 |

飞书消息正文包含配置的全部关键词。签名使用秒级 timestamp，加换行与 secret 作为 HMAC-SHA256 的 key，对空消息计算后 Base64；密钥不出现在消息正文或审计结果中。机器人 URL 和安全设置须与飞书侧配置一致。

本案例采用实例侧默认账户处理统一入口事件，管理 API 仍使用实例管理员凭据；完整 OAuth／逐仓操作者管理产品尚未实现。

## 4. 启动与接入

开发时启动：

```sh
.venv/bin/msbot-server serve --config /etc/msbot/instance.yaml
```

默认监听 `127.0.0.1:8080`，外部 HTTPS 入口转发至此地址。启动一个服务进程即可；数据库锁阻止多个后台 worker 同时使用同一状态库。

### 轻量接入：Webhook + 仓内配置

实例已配置 `gitcode` 后，在目标 GitCode 仓库的项目设置 → WebHook 中填写：

- URL：`https://<Bot 域名>/hooks/gitcode`，各仓库使用同一 URL。
- 事件：Issue 事件；请求格式：JSON。
- 认证：密码或签名密钥，与实例的 `webhook_mode` 和对应密钥一致。

确认仓库默认分支已提交第 1 节的配置和模板，并启用审计，即可通过新增 `resolved` 触发处理。**无需执行 `repo add`，也无需在实例中维护接入仓库名单。** 实例为仓库映射了飞书连接时发送通知；通知失败不影响审计和撤标。

认证成功后，Bot 从事件读取仓库路径和稳定项目 ID，并只查本地记录。记录已存在、状态为 `active` 且项目 ID 一致时直接持久受理并返回 202，这次请求不再访问 GitCode。本地没有该仓库时，先写入 `pending` 记录和 `queued` 任务再返回 202；现有单个 worker 随后用默认账户读取 GitCode 项目 ID。核对失败则把任务标为 `ignored`（`repository_unverified`），不改标签、不发通知，也不把记录升为 `active`。同一路径上的另一个未核实项目 ID 会另存一行 `pending`，不覆盖已有候选的项目 ID；worker 只把与 GitCode 一致的那一行升为 `active`，旧候选上的任务仍不可执行。不会自动创建远端 Hook。路径或 ID 非法返回 400，认证失败返回 401。与已有 `active` 记录的项目 ID 不一致，或记录不是 `active`／`pending`，返回 409，且不访问 GitCode。

202 表示已持久受理，具体审计结果通过任务查询查看：配置不存在或任务未启用会记录 `ignored`，初次配置／模板非法记录 `failed`，均不会删标或通知；后续补齐配置后，新事件即可处理。合法更新、非法更新回退及入口删除停用沿用第 1 节行为。

已有 active 内部记录会被复用，不改写其凭据、Hook ID、项目 ID 或配置快照，即使 Token 环境变量引用与当前实例默认值不同。若该 active 记录的项目 ID 不一致，或记录既不是 `active` 也不是待核对的 `pending`，统一入口返回 409；该仓库可继续使用原有专属入口。同一条内部记录跨两种入口的重复投递共享防重记录。未核实候选之间不共享这条绑定。

### 查询与原有逐仓接入

在 CLI 环境设置 `MSBOT_URL`（本地默认 `http://127.0.0.1:8080`）和同一个 `MSBOT_ADMIN_TOKEN`，再执行：

```sh
./msbot repo list
./msbot task list
```

此前的逐仓接入方式继续支持：`repo add` 绑定仓库身份、加载配置并创建仅订阅 Issue 事件、带仓库 ID URL 的 GitCode Webhook。默认密码模式；可用 `--signature` 选择 GitCode 签名密钥模式。逐仓管理员 Token、Webhook 密码和接入步骤见[接入一个仓库](repository-onboarding.md)。需要单独指定账户或密钥时可用不同的实例环境变量：

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

1. 校验 Webhook 密码／签名、项目路径和稳定项目 ID。统一入口先按本地记录受理并返回 202：已登记且项目 ID 一致时不再访问 GitCode；未登记则写入待核对任务，由单个 worker 再读取 GitCode。核对失败只把该任务标为忽略。
2. 只有 Issue update 的明确标签差异新增 resolved 才入队。同仓 delivery ID 或事件 uuid 重复均复用记录；同标识不同正文冲突。
3. 审计开始时绑定配置与模板快照；重新读取当前 Issue，缺少 bug 或 resolved 时忽略。提单人必须属于实例 `tester_authors`；仓库配置了 `authors` 时还要同时属于该名单。
4. 完整分页读取评论，逐条验证模板；合格则保留标签。
5. 全部不合格时再次检查当前标签，使用专用接口只删除 resolved，并读取当前标签确认结果。
6. 配置飞书时发送通知，包含仓库、Issue 链接、模板路径及校验原因。

查询最近 50 条记录或指定记录：

```sh
./msbot task list
./msbot task get <task_id>
./msbot task stats
```

| status | 含义 |
|---|---|
| `queued`／`running` | 已持久接收／正在审计 |
| `passed` | 找到合格登记，保留 resolved |
| `rejected` | 登记不合格，已确认标签移除；通知结果另见 notification |
| `ignored` | 不是触发事件、任务未启用、提单人不在白名单，或当前标签不再适用 |
| `failed` | 配置／读取等确定错误，没有据此执行删标 |
| `blocked` | 标签写入或通知结果不确定，保留记录，不自动重发 |

标签移除后的飞书通知结果记录在 `notification`，取值为 `none`、`sending`、`sent`、`failed`、`unknown`。HTTP 200 但飞书 code 非零不算成功。通知失败不恢复标签，也不重复删标。`GET /healthz` 检查后台 worker 是否存活，不代表所有审计任务都成功。

重启恢复只重新排队尚未发出外部写调用的工作；调用在途阶段保留未知结果。`blocked` 任务仍须人工核对原操作和平台事实，不提供强制重发外部写入的入口。

### 5.1 读取故障、诊断与失败告警

GitCode 幂等读取遇到 `NetworkError`、HTTP 429 或 5xx 时最多尝试 **3 次（含首次）**，两次等待分别为 0.5 秒、1 秒。其他错误不按此策略自动重试。有限重试失败后，审计仍可进入 `failed`；不会仅因读取失败删除 `resolved`。创建 Webhook、删标签、发送飞书等外部写入不采用此重试策略，结果不确定时保留未知状态，避免重复执行。

故障诊断记录在 `result.error`，使用 `type`、`cause_type`、`message`、`status_code`、`trace_id`、`request_id`、`retryable` 表达固定错误分类和可用元数据。`message` 是固定分类说明，不是服务器原文；HTTP 状态码和 trace 元数据仅在可用且通过校验时保留，不保存任意异常原文、请求 URL、响应正文或 Token。当前上游 `APIError` 不保留 trace，因而这类错误可能只有分类和 HTTP 状态码；不能把缺少 trace 解释为没有发生上游请求。

审计执行失败会记录日志，并可发送独立飞书告警。告警使用实例为该仓库映射的连接；未映射时按兼容字段 `notification` 选择连接。通知失败只记录通知状态，不改写审计结果。告警包含任务 ID、仓库、Issue 和安全诊断，便于管理员定位。

失败告警状态单独记录在 `failure_notification`，取值同样为 `none`、`sending`、`sent`、`failed`、`unknown`；它不改写标签删除状态 `removal` 或标签删除后的通知状态 `notification`。失败告警发送失败或结果未知不会把原 `failed` 任务变成成功，也不会自动重发告警。

### 5.2 任务统计

管理员可使用 `./msbot task stats`，或调用相同管理 API：

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${MSBOT_ADMIN_TOKEN}" \
  "${MSBOT_URL}/api/tasks/stats"
```

返回格式如下，所有已知状态均有计数，空状态为 0：

```json
{
  "ok": true,
  "data": {
    "counts": {
      "queued": 0,
      "running": 0,
      "passed": 0,
      "rejected": 0,
      "ignored": 0,
      "failed": 0,
      "blocked": 0
    },
    "failed": 0,
    "blocked": 0
  }
}
```

统计覆盖状态库中的全部任务，不限于 `task list` 的最近 50 条。`failed`、`blocked` 是保留任务的状态计数，**不是尚未处理的告警数**。人工重试会保留原失败任务，因此后继审计通过后，旧 `failed` 仍计入历史统计；不要据此认定重试未生效。统计与其他管理 API 一样要求管理员 Bearer Token，不向匿名请求开放。

### 5.3 人工重试失败任务

先查询原任务，核对失败原因，并确认 GitCode 访问、实例配置或仓库配置问题已经修复，再发起重试：

```sh
./msbot task get <failed_task_id>
./msbot task retry <failed_task_id>
```

只接受仓库绑定仍为 `active`，且原任务同时满足 `status=failed`、`removal=none`、`notification=none` 的重试。`queued`、`running`、`passed`、`rejected`、`ignored`、`blocked`，以及已有标签写入／标签删除后通知记录的任务都不能通过此入口重放。独立失败告警的 `failure_notification` 不作为标签删除或业务通知的证明。

CLI 在发送前生成 UUIDv4 `operation_id`，并将实例地址、目标 `task_id` 与 `operation_id` 写入私有操作回执，不写入管理员 Token。回执仍位于用户缓存目录 `msbot/operations/`，或 `MSBOT_OPERATION_DIR` 指定的目录；不能保存回执时不会发送请求。stderr 会给出查询命令：

```sh
./msbot operation get <operation_id>
```

重试 API 为 `POST /api/tasks/{task_id}/retry`，请求 JSON 只有 `operation_id`。用 curl 时也应在调用前自行保存目标任务与操作 ID，不要在连接中断后重新生成 ID：

```sh
TASK_ID='<failed_task_id>'
OPERATION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
# 先保存 TASK_ID 和 OPERATION_ID，再调用：
curl --fail-with-body \
  -H "Authorization: Bearer ${MSBOT_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data "{\"operation_id\":\"${OPERATION_ID}\"}" \
  "${MSBOT_URL}/api/tasks/${TASK_ID}/retry"
```

成功返回既有的 operation envelope：`data.status` 为 `succeeded`，`data.result.task_id` 是新任务 ID，`data.result.retry_of` 是原失败任务 ID。**这只确认重试任务已创建，不表示审计通过或标签状态已经正确。** 继续用 `./msbot task get <new_task_id>` 查询新任务的最终结果。

每个失败任务最多创建一个直接后继。相同目标与相同 `operation_id` 再次请求返回原操作回执，不重复入队；同 ID 改目标或换成其他管理操作会冲突。原任务已有后继后，换一个 `operation_id` 也不能再为它创建第二个后继。API 对冲突或不满足重试条件返回 HTTP 409，未知任务返回 404，非法 UUIDv4 返回 400，缺少或错误管理员凭据返回 401。

连接中断、响应损坏或非成功回执时，CLI 返回非零退出码且不自动重发；先查询原操作，必要时只用原 ID 重发同一请求：

```sh
./msbot task retry <failed_task_id> --operation-id <original_operation_id>
```

新任务保留 `retry_of` 关联，原 `failed` 审计记录及其诊断、告警状态不被覆盖。新任务开始时重新加载配置，不沿用原任务快照；合法更新生效，非法更新仍遵循上一有效配置回退规则。它也重新检查当前 Issue 标签和提单人范围，因此可能得到 `ignored` 等结果。后继若再次满足安全重试条件，可在修复原因后针对该后继发起下一次人工重试。

人工重试不会修改原 Webhook 去重规则：重复投递仍返回原记录。当前没有定时补偿扫描、自动重放全部失败任务、强制重发标签写入或未知通知的能力。

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

离线测试覆盖真实上游服务的 HTTP 请求、超过一页的评论、合法／缺失／错误选项评论、精确删标、重复投递、跨项目拒绝、鉴权、配置回退、重启恢复、飞书签名与错误响应，以及 Go CLI 接入回执。统一入口新增覆盖免登记首次接入、密码／签名先于上游读取、已登记仓库的本地受理、未登记仓库延后核对、身份核实失败不改标签、并发首投、多仓隔离、跨入口防重、配置缺失／非法／后续补齐及飞书授权；两种入口均通过实际上游服务的 mock HTTP 完整处理链路。

统一入口已于 2026-10-29 部署，公网健康、缺失／错误密码拒绝、正确密码校验、通过实际 GitCode 仓库服务核实身份后的持久受理及重复投递防重均已验证。受理探测使用显式的不可执行业务的合成事件，记录为 `ignored`，未执行删标或通知；尚不代表未预登记仓库的真实首次回调验收。下述真实业务验收使用此前带仓库 ID 的入口。

### 真实仓库验收（2026-10-29）

已在 `codeasier/msinsight` 的 `master` 提交 `.msbot/`，由服务创建 Webhook（密码模式），并通过界面新增 `resolved` 取得真实回调：

- 无登记评论：任务 `rejected`，删除 `resolved` 后读取确认，其他标签保留。
- 有完整登记评论：任务 `passed`，保留 `resolved`。
- 创建 Issue、关闭 Issue、未改标签的普通更新均被记录为 `ignored`，未触发审计。
- 严格识别的 `changes.labels.previous/current` 与真实回调一致。

平台行为差异：直接调用独立的标签子资源接口（`POST/DELETE /repos/{owner}/{repo}/issues/{n}/labels...`）不会触发 Issue Hook；通过界面标签选择器（关闭对话框时提交）或 `PATCH /repos/{owner}/{repo}/issues/{n}` 携带 `labels` 更新会触发，且当前实现能识别其 `changes.labels`。如用脚本批量改标签，必须走 Issue 更新接口，不能依赖标签子资源接口。

本次真实验收覆盖密码模式和单页评论；签名模式、Feishu 发送、多页评论及配置回退尚未在真实平台验证。

参考：
- [GitCode Issue Event](https://docs.gitcode.com/docs/help/home/org_project/webhook/webhook-stepup/issue-events)
- [GitCode Webhook 配置](https://docs.gitcode.com/docs/help/home/org_project/webhook/web-hook)
- [删除单个 Issue 标签](https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-issues-number-labels-name)
- [飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
