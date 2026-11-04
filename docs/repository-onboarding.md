# 接入一个仓库

把一个 GitCode 仓库接入 Bug Issue 关闭审计。服务和 `msbot` 命令已经可用，并且命令所在环境已经设置 `MSBOT_URL` 与 `MSBOT_ADMIN_TOKEN`。`MSBOT_URL` 必须是该实例的 HTTPS 地址。

每个仓库只能绑定一个 GitCode Token。这个 Token 同时用于读取仓库和评论、创建 Webhook，以及删除 `resolved`。服务不核对该账号是否为仓库管理员；接入时使用目标仓库管理员自己的个人访问令牌。

## 1. 提交仓库内配置

把 `examples/managed-repo/.msbot/` 提交到目标仓库的默认分支：

```text
.msbot/config.yaml
.msbot/template/bug-close.yml
```

`config.yaml`：

```yaml
version: 1
audits:
  bug_issue_close:
    enabled: true
    template: .msbot/template/bug-close.yml
    # authors:
    #   - alice
```

模板路径必须位于 `.msbot/` 下，扩展名是 `.yml` 或 `.yaml`。接入时服务读取默认分支上同一次提交的配置和模板。配置非法会使接入失败，并且不会创建 Webhook。

可选的 `authors` 再从实例配置的 `tester_authors` 里缩小范围，有效名单是两者的交集。只有交集内的 GitCode 登录名提单会做关闭审计。省略 `authors` 时，仓库启用审计即审计全部测试同学。实例名单之外的账号不会被审计。

## 2. 为该仓库配置管理员 Token 和 Webhook 密码

用目标仓库管理员的 GitCode 账号创建个人访问令牌。该账号需要能读取仓库和 Issue 评论、删除 Issue 标签，以及管理 Webhook。

再为这个仓库单独生成 Webhook 密码：

```sh
openssl rand -hex 32
```

两个值只写入实例的凭据文件 `/etc/msbot/secrets.env`，不进入 Git 仓库，也不写入运行 `msbot` 的环境。变量名由字母、数字和下划线组成。接入 `owner/repo` 时可以写成：

```sh
sudo tee -a /etc/msbot/secrets.env >/dev/null <<'EOF'
OWNER_REPO_GITCODE_TOKEN=管理员的GitCode令牌
OWNER_REPO_WEBHOOK_SECRET=上一步生成的密码
EOF
sudo chmod 600 /etc/msbot/secrets.env
```

服务进程只在启动时读取该文件。增加变量后重启服务：

```sh
sudo systemctl restart msbot
sudo systemctl is-active msbot
```

已有仓库的接入记录保存在实例数据库中，重启不会删除这些记录。

## 3. 接入并创建 Webhook

```sh
msbot repo add owner/repo \
  --token-env OWNER_REPO_GITCODE_TOKEN \
  --webhook-secret-env OWNER_REPO_WEBHOOK_SECRET
```

命令会依次完成这些事：

1. 用该仓库的管理员 Token 读取仓库，并记录稳定的项目 ID。
2. 读取默认分支上的 `.msbot/config.yaml` 和模板。
3. 在 GitCode 创建 Webhook。回调地址是 `{public_url}/hooks/gitcode/{仓库绑定ID}`，只订阅 Issue 事件；密码是 `OWNER_REPO_WEBHOOK_SECRET` 的值。
4. 确认返回了 Webhook ID、回调地址一致且 Issue 事件已启用，然后把绑定标为 `active`。

默认使用密码模式。需要 GitCode 签名时增加 `--signature`，此时上面的密钥作为签名密钥。

命令开始时在 stderr 打印 `operation_id`，并在本机用户缓存的 `msbot/operations/` 保存该 ID 和非敏感参数。连接中断时查询原来的 ID，不要更换 ID 再次接入：

```sh
msbot operation get <operation_id>
```

相同 ID 和相同参数返回原来的操作，不会再次创建 Webhook。参数不同则返回冲突。创建 Webhook 的请求已经发出但结果无法确认时，绑定停在 `blocked`。

没有 Webhook 管理权限时，改为手工创建：

```sh
msbot repo add owner/repo \
  --token-env OWNER_REPO_GITCODE_TOKEN \
  --webhook-secret-env OWNER_REPO_WEBHOOK_SECRET \
  --manual-webhook
```

把返回的 `webhook_url` 填入 GitCode 项目设置中的 WebHook，内容类型选择 JSON，事件只选 Issue，密码或签名密钥与实例中的变量一致。自动创建和手工接入不能共用同一个 `operation_id`。

## 4. 确认接入结果

```sh
msbot repo list
```

该仓库需要同时满足：

- `status` 为 `active`
- `webhook_id` 有值；手工接入时，GitCode 项目设置中已经存在对应 Webhook
- `audit_enabled` 为 `true`
- `config_error` 为 `null`

## 5. 验收

找一个带有 `bug` 标签的 Issue，给它加上 `resolved`。至少一条评论独立符合关闭模板时保留该标签；全部不合格时，服务使用这个仓库的管理员 Token 只删除 `resolved`。

```sh
msbot task list
```

创建 Issue、关闭 Issue，以及没有修改标签的更新，都会记为 `ignored`。

已经处于 `active` 的绑定不能再通过 `repo add` 更换 `--token-env` 或 `--webhook-secret-env`。
