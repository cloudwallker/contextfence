# 问题依据

[English overview](../README.md) | [中文概览](../README_ZH.md) | [架构](architecture.md)

保存过的上下文不一定仍然适合取用。来源可能改版、权限可能撤销，源也可能被删除。缓存或登记的派生摘要仍保留正文时，后端需要对“现在能否再次交付”作出决定。

以下公开一手材料支持这一问题背景。它们不表明相关企业采用了 ContextFence 的版本或锁设计，也不构成客户采用证明。

| 一手来源 | 支持的需求与边界 |
| --- | --- |
| GitHub：[Building an agentic memory system for GitHub Copilot](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)（2026-01-15） | 描述记忆使用前对代码引用与当前分支的验证；持久化之后仍需要检查当前适用性。 |
| Microsoft Digital：[How we're tackling Microsoft 365 Copilot governance internally at Microsoft](https://www.microsoft.com/insidetrack/blog/how-were-tackling-microsoft-365-copilot-governance-internally-at-microsoft/)（2026-05-07） | 描述内部权限、标签、生命周期确认及过度共享治理；企业 AI 需要服从持续变化的资料治理。 |
| AWS：[How AWS Sales uses Amazon Q Business for customer engagement](https://aws.amazon.com/blogs/machine-learning/how-aws-sales-uses-amazon-q-business-for-customer-engagement/)（2024-12-11） | 销售内部案例说明团队之间的信息隔离及遵守资料原有权限，是较早的实际业务依据。 |
| AWS：[Propagate user authorization context in AI agents with Amazon Bedrock AgentCore](https://aws.amazon.com/blogs/security/propagate-user-authorization-context-in-ai-agents-with-amazon-bedrock-agentcore/)（2026-08-19） | 官方参考方案强调向下游传递用户身份，由基础设施或下游执行授权；并非此项目的生产架构复刻。 |

ContextFence 将这个问题缩小为可验证的后端机制：来源正文版本、授权 epoch 与服务端计算的派生依赖，统一进入每次取用的当前状态检查。两个 Java 实例与同一真实 PostgreSQL 主库，使“撤权确认后，热缓存也不能放行新请求”成为可复现的实验。

该定位不要求实现聊天、推理或资料检索，也不宣称首创。工程价值在于明确事务边界、可重放事件、拒绝审计和实际并发/故障证据。依赖完整性由受信生产者保证，上游状态必须同步为本服务的快照；已经交付给模型或客户端的数据无法撤回。具体保证见[架构](architecture.md)，实际结果见[验证](validation.md)。
