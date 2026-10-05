# 本地运行手册

本文适用于 Linux Docker Engine 与 Docker Compose 的本地合成演练。两个应用实例共享一个 PostgreSQL；HAProxy、数据库和宿主机仍有单点。Windows 可在 WSL Linux 分发中执行同样命令。监控的主机资源是运行 Docker 的 Linux 环境。

## 启动和停止

在仓库根目录执行：

```bash
python3 scripts/start.py
python3 scripts/ops_smoke.py --instance both
python3 scripts/stop.py
```

启动先关闭持久维护状态，生成或保留本地凭据，构建镜像，运行独立迁移和数据库权限验收；两实例完成就绪和业务安全烟测后才开放入口。停止先关闭两道维护状态，再移除本项目容器，保留数据库卷、备份和当前身份文件。禁止用删除卷代替恢复操作。

默认回环端口为业务 `58090`、Prometheus `59090`、Grafana `53000`。API A/B 与 PostgreSQL 无宿主机业务端口。Grafana 管理用户为 `operator`，管理员凭据由 `.local/secrets/grafana-admin` 保存。脚本直接读取凭据文件；诊断与报告不输出其值。首次启动需要访问固定镜像来源和官方 Grafana 发行包；成功构建后的 Grafana 使用实际镜像 ID。

`.env`、`.local`、数据库归档和原始运行证据保留在忽略目录。配置缺失或损坏时先查明原因，不用示例文件覆盖已初始化环境，不重建丢失的身份文件来临时绕过拒绝。

## 维护和重新放行

```bash
python3 scripts/ops_common.py close
```

该命令先写入口 CLOSED，再写应用 CLOSED。状态保存于 `.local/runtime`，通过目录挂载使原子替换对容器生效。缺少或损坏状态文件时保持关闭。入口关闭期间，应用内部健康和身份验证仍供诊断使用；ready 成功不会自动解除维护。

恢复流程按顺序开放应用内部检查、验收两实例、启用后端、开放入口。`ops_smoke.py --instance both --resume` 仅恢复 HAProxy 后端，不解除持久入口维护。手工放行前必须完成当前操作要求的安全验证；常规放行由 start、deploy 或 restore 流程负责。

HAProxy 重启时两个后端默认禁用，因此代理进程重新出现并不等于业务恢复。重新执行两实例安全烟测并恢复后端，再核对持久入口状态。

## API unavailable

先在 Prometheus 查看 `up{job="contextfence"}` 和 `contextfence_ready`，区分进程不可抓取、数据库检查失败及维护状态关闭。核对本项目容器的状态和退出码，并检查两个 gate。

若另一实例仍健康，确认 HAProxy 已摘除异常实例；保留摘除前后的请求失败和时间记录。随后检查对应实例的结构化日志、JVM 内存、GC、数据库连接池与 PG 状态。修复后重新执行两实例烟测；失败继续保持关闭。

## Unexpected failures

`ServerFailureRate` 统计业务服务端 5xx；`LoadUnexpectedFailureRate` 统计压测器按完整夹具校验后的非预期结果。正常预期的 403/409 不计为非预期失败，期望成功而收到 403/410 则计入。`LoadUnsafeAllow` 表示夹具观察到错误放行，须立即停止验收并保持入口关闭。

用请求 ID、实例、固定操作与结果原因关联 JSON 日志，再核对来源有效期、上下文 cohort 切换、授权 epoch、连接池和事务状态。运行日志禁止正文、凭据、认证头及含敏感 query 的完整 URL。未经清理的输出不进入公开报告。

## Latency

客户端端到端 p95/p99 是容量验收依据。服务端 histogram 用于定位，不平均多个实例或轮次的分位数。先核对实际发出率与未发出迭代，再查看应用 CPU/GC、池等待、数据库锁和磁盘 I/O。

预热和正式测量分开；数据集、镜像、配置和客户端资源须与冻结记录一致。变更连接池或查询方式时只改一个有证据的变量，按相同协议复测，并继续运行权限与并发正确性验证。

## Database pool

`hikaricp_connections_pending` 持续大于零或连接获取超时，可能来自数据库断连、租户锁竞争、长事务或连接预算。通过监控账户读取 `pg_stat_activity` 的聚合状态及 `pg_blocking_pids` 定位真实阻塞，私下保留必要样本，不公开 SQL 正文与身份资料。

两个应用默认各 16 个连接，数据库默认 80 个连接，还需给迁移、备份、监控和管理留额度。增加连接数前确认瓶颈；扩大连接池不能修复热点锁串行化。

监控角色的有效权限检查使用 `pg_has_role(..., 'USAGE')`。PostgreSQL 17 的角色成员身份可以存在而未继承权限，因此初始化及迁移后的权限脚本显式设置 `GRANT pg_monitor TO cf_monitor WITH INHERIT TRUE`。监控账户仍没有业务表读取权或 DDL 权限；不能通过管理员身份代替监控验收。[角色继承选项](https://www.postgresql.org/docs/17/sql-grant.html)。

## Database unavailable

数据库不可用时，缓存命中也不得返回正文。检查 ready、PG exporter、数据库容器状态和项目专属网络路径。故障代理用于合成实验；中断现有连接并拒绝新连接后，两实例须 fail closed。

恢复连接后检查事务和幂等事件状态，再完成双实例业务与权限烟测。重复来源事件必须携带相同 sequence 与完整快照；不要改序号或载荷来绕过冲突。不承诺 HTTP exactly-once。

## 发布与回滚

```bash
python3 scripts/deploy.py
```

发布先运行完整 Java 和 Python 验证，再构建及解析候选镜像；记录上一版本、资源及配置清单，独立迁移后逐个排空、更新、烟测和恢复实例。候选前向流程预算为 120 秒，总计 300 秒内保留回滚时间。失败恢复上一镜像并重新做权限、幂等与业务验收，失败持续关闭入口。

数据库不随镜像回滚。只接受已验证的兼容迁移；无法兼容当前 schema 的旧镜像不能强制放行。未知未来 schema 由当前应用和迁移 CLI 拒绝。部署结果见 `artifacts/local/deployment.json`；候选失败、回滚和实际镜像记录必须一并保留。

## Backup

```bash
python3 scripts/backup.py
python3 scripts/backup_scheduler.py
```

第二个命令为前台常驻的小时调度器，需保持其运行并检查最近尝试指标。一次成功备份保持导出快照事务直到 custom pg_dump、归档检查及复制完成，再原子登记 manifest 和 latest。失败不能替换最后已验证的备份；重叠任务由锁阻止。

`BackupFailed` 检查最近尝试，`BackupExpired` 检查最近成功快照时间。检查目录权限、容量、锁及最后有效副本；先确认没有运行中的任务，再处理残留锁。保留策略为最近 24 份小时备份及 7 份日备份，保留 latest，异常证据不自动删除。

恢复操作见[安全恢复手册](recovery.md)。使用新实例和新卷，保留故障源卷；当前身份配置不从旧备份回退。完整权威合成账本必须覆盖所有恢复来源和历史水位，不能从数据库投影拼出一份“权威”账本。任何缺口、校验失败或身份不一致均保持入口关闭。

## Disk

检查 Linux 文件系统、Docker 数据位置、日志轮转、Prometheus 保留空间和数据库卷之外的备份目录。不要用全局 prune 或删除其他项目的数据处理本项目告警。先记录增长来源，再依据本项目保留策略清理已核验的可替代文件。

## 证据和能力边界

原始结果保存在 `artifacts/local`，恢复账本、凭据与备份保存在 `.local`。公开报告只提取必要合成统计，排除凭据、正文、私人路径和完整环境转储。每个成功结论关联实际版本、镜像、配置、开始结束时间、退出码和原始证据；失败及复验均保留。

本地双实例与单库演练不证明生产高可用、宿主机损毁恢复或异地容灾。数据库历史缺失、合成来源与权限重建、主动退役的旧上下文分别报告，不写成零数据丢失。
