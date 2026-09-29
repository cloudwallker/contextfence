# 验证记录与复现

[English overview](../README.md) | [中文概览](../README_ZH.md) | [API](api.md) | [架构](architecture.md)

2026-09-29 的记录运行环境为 Windows、OpenJDK 21.0.1、Maven 3.8.5 与真实 PostgreSQL 17.11。应用使用 Spring Boot 4.1.1、PostgreSQL JDBC 42.7.13 与 Testcontainers 2.0.5。HTTP 演示中的两实例是两个独立 `java -jar` 进程，各自有独立正文缓存。

## 已观察结果

| 检查 | 结果 | 保存的证据 |
| --- | --- | --- |
| Maven `verify` | 39 项通过，0 失败、0 错误、0 跳过；9 单元 + 30 集成 | [JUnit 计数摘要](reports/verification.json) |
| 两独立 JVM HTTP 演示 | 83 次 HTTP 观察断言通过；撤权确认后 50/50 并发取用为 403、无正文 | [元数据](reports/demo.json)、[HTML 报告](reports/report.html) |
| 确定性并发与进程终止 | 7 项通过；两实例都先有实际缓存命中 | [并发实验](experiments/concurrency-results.json) |
| Python 启动/凭据脚本 | 16 项通过；1 项 POSIX 权限测试在 Windows 明确跳过 | `python -m unittest discover -s scripts/tests -v` |
| Compose 与脚本语法 | Compose 配置、PowerShell Parser、Bash `-n` 通过 | 容器运行未实测 |

Docker Engine API 在记录环境返回 HTTP 500，因此镜像构建、容器运行和 Testcontainers 启动分支未经实测。Java 集成测试使用显式配置的真实 PostgreSQL 分支完成，没有替换为 H2，也没有跳过集成检查。Python 的 POSIX 跳过与 Java 测试无关。

## 覆盖关系

| 覆盖 | 主要验证入口 |
| --- | --- |
| 跨租户、跨用户资源不可见；伪造身份头与未知 JSON 字段拒绝 | `SourceIT`、`ContextIT`、`HttpIT` |
| 双 JVM 缓存已热，撤权确认后的新取用拒绝 | `ConcurrencyIT`、HTTP 演示 |
| source → child → grandchild；正文变化后旧条目失效 | `ContextIT`、HTTP 演示 |
| 删除源及派生项拒绝，删除源不能复活 | `SourceIT`、`ContextIT`、HTTP 演示 |
| 同事件重放、同序号异参、旧序号无效且不续期 | `SourceIT`、HTTP 演示 |
| 撤权再授权，旧 epoch 仍失效而新条目可读 | `ContextIT`、HTTP 演示 |
| 等锁期间到期，锁后数据库时间决定拒绝 | `ConcurrencyIT`、`ContextIT` |
| 无效/跨用户父项、期限继承、来源/深度/UTF-8 限制 | `ContextIT` |
| 读先与写先的实际阻塞、确认后 50 个并发请求 | `ConcurrencyIT`、HTTP 演示 |
| 既有 PostgreSQL TCP 连接被切断且新连接拒绝，热缓存不放行 | `DatabaseOutageIT` |
| 提交前终止不半写，提交后终止并重放恢复结果 | `ConcurrencyIT` |
| 混合有效/失效批次不返回部分正文 | `ContextIT`、HTTP 演示 |
| 合成正文标记不进入 receipt、失败 HTTP、捕获日志及报告 | `ContextIT`、`HttpIT`、报告脚本 |

## 并发与故障证据的准确含义

50 个确认后请求从两 JVM 的线程池发起，连接池可能排队，不意味着 50 个数据库连接同时持锁。读先/写先另用确定性的重叠请求复现；通过提交屏障、`pg_stat_activity` 与 `pg_blocking_pids` 观察真实阻塞，不靠固定 sleep 猜顺序。测试挂钩仅存在于测试装配。

承诺针对撤权成功确认后新发起的请求。先获共享锁的重叠读可能成功，其字节可能晚于确认抵达。进程终止实验验证事务结果恢复，不代表所有网络分区，也不承诺 HTTP exactly-once；重试必须携带相同 sequence 与完整快照。

数据库断连使用测试专属 TCP 转发器，不停止共享 PostgreSQL。日志检查覆盖实际执行路径，不等于任意日志配置或恶意运行环境都不会泄露。记录延迟属于本地测量，不是生产 QPS。

## 复现

先按 README 配置 Docker/Testcontainers，或名称以 `_test` 结尾的独立 PostgreSQL 测试库。

```powershell
.\scripts\maven.ps1 verify
python -m unittest discover -s scripts/tests -v
python scripts/collect-results.py

# 两个 HTTP 实例就绪后
python scripts/demo.py
python scripts/report.py
```

```bash
mvn verify
python3 -m unittest discover -s scripts/tests -v
python3 scripts/collect-results.py

# After both HTTP instances are ready
python3 scripts/demo.py
python3 scripts/report.py
```

单独定位可用 `mvn -Dtest=SourceIT test`、`mvn -Dtest=ContextIT test`、`mvn -Dtest=HttpIT test`、`mvn -Dtest=ConcurrencyIT test` 或 `mvn -Dtest=DatabaseOutageIT test`。PowerShell 使用 `.\scripts\maven.ps1 '-Dtest=ConcurrencyIT' test` 等对应命令。

测试在随机租户下留下合成数据，需按测试库保留策略清理。原始 JUnit XML 含本机环境属性，不适合直接公开；`collect-results.py` 仅提取套件名、时间及计数。保存的报告对应记录运行，重新运行后应重新生成，不能把旧报告当成新提交的验证。
