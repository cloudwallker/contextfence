# 验证记录与复现

[English overview](../README.md) | [中文概览](../README_ZH.md) | [API](api.md) | [架构](architecture.md)

## 当前本地运行与验收范围

本次交付汇总已实测的本地部署、权限、可观测性、故障、回滚、安全恢复和容量预探测。36 项正式容量矩阵、9 项单变量对照、长时容量与连续备份观察及云部署列为后续验证，当前没有通过正式容量验收的结果。

本次 **2026-10-05** 本地 SRE 记录使用 WSL 中的 Ubuntu 24.04.3 LTS、原生 Docker Engine 29.1.3 与 Compose 2.40.3。完整 Compose 栈已实际运行：主栈 HAProxy 统一业务入口只绑定 `127.0.0.1:58090`，Prometheus 为 `127.0.0.1:59090`，Grafana 为 `127.0.0.1:53000`。两个 API 实例和 PostgreSQL 均无宿主机业务端口。2026-09-29 的 Docker Engine API HTTP 500 是下方历史运行限制，不是当前状态。

默认启动由 `scripts/start.py` 准备固定摘要的基础镜像，以及经大小和 SHA-256 校验的官方 Grafana 12.2.0 发行包；实际 Grafana 镜像 ID 写入本地运行配置。完整本地启动和默认官方归档分支均已实测通过。首次启动需要网络，已经校验的发行包缓存在忽略目录。启动先关闭入口，运行独立迁移与数据库角色检查，两实例业务安全烟测通过后才放行。

| 当前检查 | 已观察结果 | 范围 |
| --- | --- | --- |
| Java `verify` | 77 项通过：32 单元 + 45 集成；0 失败、0 错误、0 跳过 | 最新完整发布管线的实际 Maven 输出；不是下方旧 39 项报告 |
| 运行镜像与受测代码对应 | 全部 71 个生产 class 逐字节一致，0 不匹配 | `runtime-all-classes-provenance.json` 与发布/恢复受测 class 的交叉核验；见[摘要](operations/runtime-evidence.md#测试与运行镜像的对应) |
| 最终 Windows Python 脚本回归 | 243 项中 242 通过，1 项 POSIX 权限检查明确跳过；0 失败/错误 | Python 3.9.25；2026-10-05 10:09:20.301696–10:10:16.844681 UTC |
| 最终 Linux Python 脚本回归 | 243 项中 234 通过，9 项 Windows 专属检查明确跳过；0 失败/错误 | WSL Python 3.12.3；10:13:16.869758–10:13:39.693288 UTC，同一源码摘要，检查前后未变 |
| JavaScript 契约 | 通过 | Windows Node v24.15.0；Linux Python 经 WSL interoperability 调用 Windows Node，未验收原生 Linux Node |
| 主项目运行 | 双实例安全烟测通过，Prometheus 6 个目标均正常 | 两个 API、PG exporter、node exporter、HAProxy 和 Prometheus 自身 |
| 实际快照备份 | 通过 | 导出快照、custom pg_dump、归档检查、复制和 manifest 校验 |
| API A 实际终止 | 通过；HAProxy 摘除 8.418 秒，告警接收 47.316 秒 | 15 秒摘除与故障注入后 60 秒告警目标 |
| API B 实际终止 | 通过；HAProxy 摘除 9.348 秒，告警接收 41.569 秒 | 同上 |
| 数据库网络断连 v5 | 通过；13 条既有连接切断，新连接拒绝；两实例 ready 与预热取用均为 503，取用无正文 | 主要 firing 52.101 秒、resolved 19.689 秒；[七时点与完整复盘](operations/postmortem-db-disconnect.md#七个实际时点) |
| 坏版本完整发布管线 | 故障验收通过；ready 失败 118.395 秒 ≤ 120；回滚验收 201.782 秒 ≤ 300 | 真实 `scripts/deploy.py` 测试、构建、迁移与发布路径；整个演练 681.265 秒，保留 9 次资源采样错误 |
| 独立新数据库卷恢复 | 通过；RTO 44.006 秒；故障时快照年龄 21.684 秒 | 5 个确认 context 缺 1、8 条 receipt 缺 4；6 条 source_event 缺 3，由账本重建 3，原应用时间未恢复 |
| 恢复后安全与兼容 | 通过；旧 epoch/token 拒绝，混合批次无部分正文；4 项角色权限检查、6 个监控目标与新备份通过 | 真实旧二进制参与恢复前后检查；源数据库卷保留，入口仅在安全验收后开放 |
| 受控指标告警链路 | `LoadUnsafeAllow`、`LoadUnexpectedFailureRate`、`BackupExpired` 的实际 firing/resolved 均通过 | Prometheus、Alertmanager 和接收器按同一 fingerprint/startsAt 关联；使用合成计数和临时老化指标，不是服务错误放行或真实备份过期 |
| 有界磁盘告警 v2 | 通过；实际填充 89,920,512 字节，专属 ext4 有效容量 108,974,080 字节，余量约 14.9972%；全流程 149.673 秒 | `HostDiskLow` 同身份 firing/resolved 在 Prometheus、Alertmanager、接收器关联；清理核验通过，128 MiB 镜像保留；[v1 失败与 v2 实测范围](operations/runtime-evidence.md#有界磁盘的实际告警交付与清理) |
| 持续池等待告警 v2 | 实际执行退出 0，四类故障率/池等待/池超时/尾延迟告警 firing/resolved 通过，最终两平面所有状态清空 | 本轮 fingerprint/startsAt 关联；首轮恢复验收失败保留，见[实际告警交付](operations/runtime-evidence.md#持续池等待的实际告警交付) |
| 有界应用日志核验 | 8,070 行捕获中有 5,075 条合法结构化请求记录，非法与重复请求记录均 0；整体隐私检查 FAIL | 1,767 行命中保守泄露规则，一般 SQL/数据库 URL 仍只留在私有诊断日志；窗口起点未覆盖，不作全历史无泄露或精确 SQL 请求因果结论，见[日志限制](operations/runtime-evidence.md#有界应用日志核验与隐私限制) |
| bench 运行前预检 v3 | 通过；仅 bench 的 10 个容器运行，6 个目标正常，两 API 连接池上限均 16，4 项角色检查通过 | 04:57:58.415230 UTC；`bench-readiness-before-probes-v3.json`，尚不构成容量测量结果 |
| 实际 API RSS | A 为 326,459,392 字节，B 为 325,533,696 字节 | `java-init-child-proc-status`；实际 Java 子进程状态来源，Prometheus RSS 指标尚未导出 |
| 较早环境两次调度备份 | 快照 04:31:33.571204 / 05:33:27.418835 UTC，完成 04:31:38.150873 / 05:33:31.851628 UTC；实际快照间隔 3,713.847631 秒 | 归属、存活及固定 3,600 秒配置当时已核验，两份归档通过；旧调度器已停，不证明精确小时、长期连续或全程一小时 RPO |
| 首轮开发短测 | 10 秒 multi / 10 RPS；101 issued，44 success / 57 unexpected，业务 HTTP 全部 200；开发验收失败 | read 93 / write 8 的 started、HTTP 与 issued E2E 严格一致；unsafe、drop、采样错误计数均 0，实际采样间隔中位数 8.897779 秒；[保留的失败与诊断](operations/runtime-evidence.md#开发短测失败与后续诊断) |
| 开发短测 v2 | 10 秒 multi / 10 RPS；100 issued / 100 success，unexpected、unsafe、drop、采样错误均 0，k6 退出码 0；开发验收通过 | 05:29:10.888153 UTC；read 92 / write 8 三计数严格一致；3 个资源样本，间隔中位数 5.000168 秒、最大 5.000169 秒；仍是 `formal_protocol=false` |
| 正式探测 v1 | 失败、未完成，整体退出 1；hot10 的 600 次业务请求成功但结果导出失败；hot25 有 912 timeout / 93 client_failure / 120 dropped，feeder 失败；hot50 初始 seed 失败 | 两份已保存报告均不具容量验收资格；[失败和代理观测](operations/runtime-evidence.md#正式探测-v1-失败与代理观测)保留，未冻结容量 |
| HAProxy 配置受控应用 | 05:52:30.989423–05:53:56.839817 UTC 通过；实际版本 3.2.6，两个进程线程数均为 1，镜像与 CPU/内存限制不变 | 离线配置检查、关闭入口时 503、双实例权限烟测、4 项角色与 6 个监控目标通过；仅证明配置应用和当前健康恢复，不证明容量或根因 |
| 正式探测 v2 | 未完成并于 06:02 UTC 停止；hot10 的 601 次业务成功，但 raw-scan 实际捕获 OSError / errno 61，导出失败；hot25 的 1,501 次业务成功且单项有验收资格 | 原失败诊断与单项结果保留；单项通过不能替代 15 项完整探测或冻结 |
| 当前 ext4 bench 启动与预检 | 完整启动 06:07:27.540145–06:09:18.088369 UTC 通过；06:11:06.967552 UTC 预检通过：仅 10 个 bench 容器、6/6 目标、pool max 16/16、4 项角色及正值 Java RSS | 原 Windows 挂载目录 bench 实际停止、入口关闭、4 份卷保留；当前为 WSL/Linux ext4，不据迁移推断旧失败根因 |
| 当前环境实际调度备份 | v4 审查核验四份归档、latest 引用与专属进程归属；实际快照间隔 3,697.525889 / 3,680.996285 / 3,667.114568 秒 | 调度器固定配置 3,600 秒，但实际间隔超出 97.525889 / 80.996285 / 67.114568 秒；不证明精确小时或全程一小时 RPO |
| 开发短测 v3 | 10 秒 multi / 10 RPS，101 issued / 101 success；unsafe/drop/业务与采样错误均 0，k6 退出 0；开发验收通过 | 06:12:41.089004 UTC，read 93 / write 8 三计数严格一致；3 个合法 RSS 样本，间隔中位数 / 最大值 5.485785 / 5.971443 秒，真实间隔保留 |
| 预探测 v3 首组历史例外 | hot10 的 600 次业务请求成功，但 16 行资源有 11 行 API 进程读取失败标记（A 11 / B 10），最大采样间隔 9.227113 秒 | 旧标记只能证明 RSS 读取失败，不能确认为超时；原失败保留，不参与新版冻结 |
| 预探测 v4 元数据拒绝 | 15 项的 execution 与 summary/index 完成时间均不一致 | 07:24:27.352204 UTC 的原字节复验；不修改原报告，单项业务成功不能消除元数据拒绝 |
| 完整预探测 v5 | hot/single/multi × 10/25/50/100/200 RPS 的 15 项均有验收资格；原请求计数、完成时间、资源覆盖和等待审查通过；错误、drop、feeder、导出与采样失败均 0 | 08:37:03.058287 UTC 严格复验，政策摘要未变；每项预热 30 秒、测量 60 秒，不是正式 300+600 秒矩阵 |
| 追加 400 RPS 预探测 | 三项均完成、均不具容量验收资格；single 非预期响应为 324/23,693（1.36749%）、drop 308、PG CPU 均值 97.34%、锁等待首尾 17→24.5（增长 44.12%） | 09:02:08.388258 UTC 严格复验；hot/multi 保留未发出客户端失败、feeder、drop 与资源例外，不能据此确认绝对最大容量 |
| 实际档位及回归阈值冻结 | 09:15:23.769458–09:15:23.908804 UTC，退出码 0；低档 25、额定档 200、过载观察档 400 RPS；p95 18 ms / p99 102 ms，指标实际读回 0.102 秒 | 18 次预探测的 72 份原始文件摘要重新核验；`formal_capacity_not_yet_verified=true`，36 项矩阵与 9 项对照列为后续验证 |
| 恢复后重启路径修复 | 当前数据库服务选择与无依赖启动通过独立代码复核，4 项定向检查 RED→GREEN，已纳入最终完整回归 | 实际恢复后的 stop→start 列为后续验证；不扩展已完成的恢复停止交接结论 |
| bench 最终停止交接 | 10:17:35.472745–10:17:53.930395 UTC 实际通过：专属调度器停止、容器 0、两层入口关闭、数据库卷保留 | 按进程身份核验停止归属，未运行正式矩阵或对照 |

最终 243 项脚本回归的源码摘要为 `1be7df3a06fddbf68dbe3444b48537cd7916ecb8c82e7934dfd48f684882f2ee`，两平台各自检查前后相同，且跨平台一致。后续源码改动须有新的完整回归记录。约 07:31 UTC 的 216 项验证点、发布管线较早的 125 项及后来 165、183、188、197、212、215 项均保留当时范围，见[历史验证点](operations/runtime-evidence.md#历史脚本验证点)。五秒 RSS CLI 时限与统一完成时间已在新版 v5 实际运行；固定失败类别只表示异常家族。公共追加探测 merge 入口已对既有 18 项观察实际执行通过，原文件未改，400 RPS 的容量失败保留，未重新发起负载。告警 mock 与纯报告生成器 guard 检查只覆盖对应工具分支；独立代码审阅通过也不能代替真实运行报告。

v1/v2 失败或未完成记录、v3 RSS 读取失败及 v4 元数据拒绝均保留。当前 WSL/Linux ext4 环境的 v5 必需预探测和追加 400 RPS 观察已完成，并在任何正式运行之前冻结 25/200/400 RPS；冻结只建立实验档位与阈值。容量矩阵、单变量对照与长期备份观察列为后续验证，当前没有正式容量结果。后续正式矩阵为 27 项稳态与 9 项冷启动，纯协议时间共 24,840 秒（6 小时 54 分钟）；9 项对照另需 8,100 秒（2 小时 15 分钟）。40 秒 `gracefulStop` 只允许已发出请求收尾，不改变 300 秒预热和 600 秒测量。每个原始文件、阶段与操作的 started、业务 HTTP 完成和 issued E2E 数量须严格一致；缺失直接拒绝，不借用 1% 到达率容差。预探测不能替代正式窗口；数据库断连中实际交付的池超时告警不能替代持续池等待告警的独立演练。

2026-10-05 的留存状态为：主栈停止并保留数据库卷及凭据；独立恢复栈使用 `58096` / `59096` / `53006`，在 04:20:34.355803 UTC 的最终交接中完成双实例烟测、4 项角色检查、最终备份与 6 个监控目标验收后，实际运行 `stop.py` 并以 0 退出，运行容器为 0、两层入口关闭、两份数据库卷保留。容量预探测项目使用 `58095` / `59095` / `53005`；旧 Windows 挂载目录阶段已停止并保留 4 份卷，ext4 bench 在 10:17:53.930395 UTC 完成停止交接。其冻结前环境审查为 09:09:09.822925 UTC：当时仅此运行栈，6/6 监控目标、4 项角色及 pool max 16/16 通过，实际 VM 为 20 CPUs、8,162,697,216 字节（7.602104 GiB）内存。档位已冻结，正式矩阵与对照属于后续验证。最终恢复交接依据为本地 `recovery-final-stop-handoff.json`，与各阶段记录分别留存。

原始输出、配置、完整账本、数据库归档和日志保留在忽略的 `artifacts/local` 与 `.local`。公开内容只提供合成统计、固定结果、必要镜像/代码摘要及时间，不公开 UUID、来源/上下文标识、正文、token、密码、私有主机路径或原始备份和日志。见[当前脱敏运行证据](operations/runtime-evidence.md)与[数据库断连复盘](operations/postmortem-db-disconnect.md)。下方已保存的历史报告保留其原日期与统计。

### 当前复现入口

在仓库根目录、连接上述 Linux Docker 引擎的环境中执行：

```bash
python3 scripts/start.py
python3 scripts/ops_smoke.py --instance both
python3 scripts/backup.py
# 检查本地监控和输出后停止；数据库卷与凭据保留
python3 scripts/stop.py
```

Windows PowerShell 对应使用 `python`。双实例烟测输出为 `artifacts/local/ops-smoke.json`，只包含固定决策和检查结果。发布使用 `scripts/deploy.py`，每次先运行完整 Java 与 Python 检查；小时备份使用前台常驻 `scripts/backup_scheduler.py`。完整故障恢复入口 `scripts/restore_demo.py` 会停止源 PostgreSQL，须先满足完整可信账本与隔离恢复前提。

详细流程见[本地运行手册](operations/runbook.md)、[数据库备份与安全恢复](operations/recovery.md)和[容量与故障验证方法](operations/capacity-method.md)。两个 API 实例共享单主库，代理、数据库和宿主机仍有单点；本地观察不构成生产高可用、云部署、CI/CD 或异地容灾验收。

## 2026-09-29 历史记录

2026-09-29 的记录运行环境为 Windows、OpenJDK 21.0.1、Maven 3.8.5 与真实 PostgreSQL 17.11。应用使用 Spring Boot 4.1.1、PostgreSQL JDBC 42.7.13 与 Testcontainers 2.0.5。HTTP 演示中的两实例是两个独立 `java -jar` 进程，各自有独立正文缓存。

### 历史已观察结果

| 检查 | 结果 | 保存的证据 |
| --- | --- | --- |
| Maven `verify` | 39 项通过，0 失败、0 错误、0 跳过；9 单元 + 30 集成 | [JUnit 计数摘要](reports/verification.json) |
| 两独立 JVM HTTP 演示 | 83 次 HTTP 观察断言通过；撤权确认后 50/50 并发取用为 403、无正文 | [元数据](reports/demo.json)、[HTML 报告](reports/report.html) |
| 确定性并发与进程终止 | 7 项通过；两实例都先有实际缓存命中 | [并发实验](experiments/concurrency-results.json) |
| Python 启动/凭据脚本 | 16 项通过；1 项 POSIX 权限测试在 Windows 明确跳过 | `python -m unittest discover -s scripts/tests -v` |
| Compose 与脚本语法 | Compose 配置、PowerShell Parser、Bash `-n` 通过 | 容器运行未实测 |

上述 2026-09-29 记录环境的 Docker Engine API 返回 HTTP 500，因此当时的镜像构建、容器运行和 Testcontainers 启动分支未经实测。该次 Java 集成测试使用显式配置的真实 PostgreSQL 分支完成，没有替换为 H2，也没有跳过集成检查。Python 的 POSIX 跳过与 Java 测试无关。

### 历史覆盖关系

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

### 历史并发与故障证据的准确含义

50 个确认后请求从两 JVM 的线程池发起，连接池可能排队，不意味着 50 个数据库连接同时持锁。读先/写先另用确定性的重叠请求复现；通过提交屏障、`pg_stat_activity` 与 `pg_blocking_pids` 观察真实阻塞，不靠固定 sleep 猜顺序。测试挂钩仅存在于测试装配。

承诺针对撤权成功确认后新发起的请求。先获共享锁的重叠读可能成功，其字节可能晚于确认抵达。进程终止实验验证事务结果恢复，不代表所有网络分区，也不承诺 HTTP exactly-once；重试必须携带相同 sequence 与完整快照。

数据库断连使用测试专属 TCP 转发器，不停止共享 PostgreSQL。日志检查覆盖实际执行路径，不等于任意日志配置或恶意运行环境都不会泄露。记录延迟属于本地测量，不是生产 QPS。

### 历史 JVM 演示复现

以下命令用于历史测试与两个独立 JVM 的演示。`demo.py` 默认直接连接 JVM 的 `58091`/`58092`，不适用于当前无 API 宿主机端口的 Compose 栈；当前启动与验收使用上方 `start.py` 和 `ops_smoke.py --instance both`。Java 测试可使用 Docker/Testcontainers，或名称以 `_test` 结尾的独立 PostgreSQL 测试库。

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
