# 标签规范

本仓库的标签用于 Issue / PR 分诊，命名遵循 `类别/值` 的形式；类型标签保留 GitHub 标准名称，便于社区工具与搜索识别。

> 标签目前由维护者人工维护。后续自动化接入后，部分标签（如 `status/stale`）可能由机器人流转，届时以实际行为为准。

## 命名约定

| 前缀 | 用途 | 示例 |
| --- | --- | --- |
| 无前缀 | 类型 | `bug`、`enhancement`、`question`、`documentation` |
| `priority/` | 优先级，数字越小越紧急 | `priority/P0` |
| `status/` | 处理状态 | `status/needs-info` |
| `area/` | 涉及范围 | `area/rules` |

修改标签名称会影响后续自动化脚本与历史检索，改名或删除前请先讨论。

## 优先级

| 标签 | 判定标准 |
| --- | --- |
| `priority/P0` | 阻塞发布、线上不可用，或涉及安全（例如凭据泄露） |
| `priority/P1` | 主要流程不可用，且没有绕过方式 |
| `priority/P2` | 功能异常但有临时绕过方式，或一般改进需求 |
| `priority/P3` | 体验优化、文档措辞、不影响使用的建议 |

约定：

- 分诊后每个 Issue 应有且仅有一个优先级标签。
- 缺陷模板会默认带上 `priority/P2`，分诊时按实际情况上调或下调。
- 拿不准时倾向低一档，除非有明确的阻塞证据。

## 类型

| 标签 | 含义 |
| --- | --- |
| `bug` | 功能异常或运行错误 |
| `enhancement` | 新功能或改进建议 |
| `question` | 使用咨询 |
| `documentation` | 文档补充或修正 |

通常只保留一个类型标签；确实跨类时可并存，但需在正文说明。

## 状态

| 标签 | 含义 | 何时移除 |
| --- | --- | --- |
| `status/needs-info` | 缺少复现步骤或环境信息，等待作者补充 | 信息补齐后 |
| `status/blocked` | 依赖外部修复、上游变更或其他 Issue | 依赖解除后 |
| `status/stale` | 长时间无活动，可能被关闭 | 有新的进展时 |

关闭 Issue 时使用结论标签（沿用 GitHub 标准命名，不加 `status/` 前缀）：

- `duplicate`：与已有 Issue 或 PR 重复
- `invalid`：描述不成立或无法复现
- `wontfix`：经评估不修复

## 范围

| 标签 | 含义 |
| --- | --- |
| `area/issue` | Issue 处理相关 |
| `area/pr` | PR 处理相关 |
| `area/rules` | 规则引擎与配置 |
| `area/docs` | 文档 |
| `area/deploy` | 部署与运维 |

## 社区

| 标签 | 含义 |
| --- | --- |
| `good first issue` | 适合新贡献者上手，改动范围明确 |
| `help wanted` | 需要社区协助 |

这两个标签保持英文原名，以满足 GitHub 社区概览的识别要求。

## 与 GitCode 的差异

以上标签仅作用于本仓库（GitHub）。GitCode 上的 MindStudio 社区仓库标签需单独配置，两边目前没有同步机制；若后续统一，需要额外的映射规则。
