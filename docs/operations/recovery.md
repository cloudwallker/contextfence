# 数据库备份与安全恢复

本流程用于本地合成资料演练。PostgreSQL 是单主库，备份位于数据库卷之外的 `.local/backups`；同一物理磁盘损坏和整机故障仍需异机副本。本流程不能证明生产高可用。

备份使用 PostgreSQL 导出的实际事务快照。导出连接保持存活，直到 custom-format `pg_dump`、归档目录检查和文件复制完成。成功文件经校验后原子发布 manifest 和 latest 引用；失败备份不替换已有成功备份。manifest 保存快照 ID、快照时间、完成时间、schema 版本、数据库版本、上下文数量、来源数量、应用镜像摘要和归档 SHA-256。

```sh
python scripts/backup.py
python scripts/backup.py --verify .local/backups/<backup-id>
python scripts/backup_scheduler.py
```

第三条命令立即尝试一次备份，然后每小时尝试一次。使用宿主机进程管理器保持调度器运行；停止进程不会启动后续任务。任务失败后下一轮仍会执行，失败与快照时间写入 `.local/metrics/backup.prom`，Prometheus 对任务失败和快照超过 70 分钟告警。保留最近 24 份小时备份和 7 个不同日期的日备份，清理前必须确认 latest 仍校验通过；损坏文件留作诊断。

独立合成上游生产者先原子保存完整事件账本，再调用来源 API。账本带完整来源快照、ACL、删除和每个来源的连续序号，保存在 `.local/recovery/ledger.json`；不能从恢复出的 `source_events` 生成“独立”账本。SHA-256 用来检测损坏，不证明生产者身份；本地文件的写入权限承担演练中的可信来源边界。每个事件恰好包含 `tenant`、`source_id`、`sequence`、`content`、`readers`、`state`、`fresh_until` 七个字段；删除后不允许复活。

恢复必须使用当前数据库角色密码和当前身份映射。数据库备份不包含集群身份配置，流程不恢复旧 token 文件。`.local`、日志、账本、身份映射与备份都不得公开上传。

```sh
python scripts/restore.py \
  --backup .local/backups/<backup-id> \
  --ledger .local/recovery/ledger.json \
  --fixture .local/recovery/fixture.json \
  --failure-at <UTC-fault-injection-time>
```

恢复按下列顺序执行：

1. 在数据库之外原子关闭 HAProxy 和应用 gate，验证入口关闭，停止两个应用实例。
2. 验证备份 manifest、归档 SHA-256、独立账本及完整历史。故障前已确认的来源事件必须全部出现在账本截止水位内；错误、序号缺口和缺失来源均保持关闭，不能先创建恢复库再补齐材料。
3. 创建独立 PostgreSQL service 和全新 named volume。`pg_restore` 使用当前管理凭据、单事务、`--no-owner --no-privileges`，对象归当前 migration 角色。源库与源卷保留，流程没有删除源卷的步骤。
4. 运行 HTTP-free `--migrate-only`，再运行 `--reconcile-source-ledger`。离线 job 使用 migration 数据库角色；只在读取受限账本文件的容器文件系统层使用 root。校正事务清空所有旧上下文正文、保留墓碑与审计、完整重建来源，选择高于旧来源和所有旧 binding 的恢复 epoch。
5. 核对全部来源的 sequence、内容版本、ACL、删除状态、fresh_until、恢复 epoch，确认所有旧上下文已退休且正文为空、旧 binding 与当前来源 epoch 都不同。再次查询实际来源事件记录，逐个确认备份中缺失的已确认事件已经重建，再登记重建数量。
6. 重建两个应用容器，清空两份进程缓存并重新加载当前 token 映射。HAProxy gate 保持 CLOSED，仅应用 gate OPEN，进行内部检查。CLI 保留原 fresh_until；需要续期时，由可信生产者显式产生新事件。
7. 两实例均通过旧 SOURCE、DERIVED、混合批次及旧 parent 拒绝，旧收据仅有元数据，撤权与删除拒绝，新来源和新上下文可用，跨租户不可见，旧旋转 token 返回 401 后，才恢复后台并开放入口。任何失败重新关闭两道 gate。

持久 `.local/runtime/database.env` 记录当前恢复库，`.local/runtime/recovery.compose.yaml` 保留隔离 service/volume；后续操作自动加载这两个文件。故障源库无需重新启动来提供数据库客户端。不要执行 `compose down --volumes`，不要把当前身份映射替换成备份中的版本。

完整演练命令：

```sh
python scripts/restore_demo.py
```

演练需要独立项目或覆盖全部已有来源的可信生产者账本。它先创建 SOURCE/DERIVED 并预热两实例，做实际快照备份，再提交撤权、删除、备份后新来源及新上下文，轮换 reader token，停止源 PostgreSQL，最后执行隔离恢复与安全放行。缺失的已有来源不会被静默补成账本。

RTO 从故障注入开始计时，到安全验收后开放入口为止，包含新库准备、归档恢复、权限重建、重启和内部检查。RPO 使用真实导出快照时间，并在校正事务前核对恢复库中的上下文、收据和来源事件历史：报告三类故障前确认提交的计数、缺失记录与最早缺失提交的时间，旧 fixture 缺少任何一类确认历史都会拒绝恢复。小时调度本身不证明一小时 RPO。来源事件可由完整账本重建，但备份后事件的原数据库应用时间没有随之恢复；报告明确列出重建数量和原时间是否丢失。来源和 ACL 追平账本、主动退休的旧上下文、数据库历史损失分别报告。不要把来源重建写成数据库零丢失。

私有机器证据位于 `artifacts/local/recovery.json`；公开验收报告只能使用清理后的计数、时间和判定，不能附带正文、token、口令、数据库连接串或本地绝对路径。尚未运行真实演练的环境不得引用脚本测试作为 RPO/RTO 实测结果。
