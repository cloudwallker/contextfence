# 本地容量与故障验证方法

本文件定义可复现的方法与工具入口。当前公开交付包含容量预探测与档位冻结；36 项正式矩阵、9 项单变量对照和长时容量结论列为后续验证。各项故障与恢复结果以[实测运行证据](runtime-evidence.md)为准。只有完整原始样本、运行退出码、资源记录和验收报告同时存在，才能报告相应容量结论。

2026-10-05 较早 Windows 挂载目录阶段的 probe v1 失败，v2 已停止且未完成；当前 ext4 阶段的 v3 RSS 读取失败和 v4 完成时间不一致也分别保留。最新完整 v5 必需 15 项预探测已通过原始请求、元数据和资源审查，追加 400 RPS 三项均完成但均不具容量验收资格。09:15 UTC 实际冻结为 **低档 25 / 额定档 200 / 过载观察档 400 RPS**，p95 回归上限 **18 ms**、p99 **102 ms**，实际指标读回 **0.102 秒**。36 项矩阵、9 项对照及长期备份观察列为后续验证，冻结不是正式容量完成；告警链路按独立运行证据报告。具体范围见[完整预探测与实际冻结](runtime-evidence.md#完整预探测与实际冻结)和[历史失败与代理观测](runtime-evidence.md#正式探测-v1-失败与代理观测)。

最终双平台完整脚本回归为 243 项，07:31 UTC 的 216 项保留为历史，细节见[测试与镜像对应](runtime-evidence.md#测试与运行镜像的对应)；后续源码调整不能继承旧结果。五秒 RSS CLI 时限与统一完成时间已在 v5 实际运行。公共 merge 已对既有 18 项探测执行通过，保留失败观察且未重跑负载；新增 run 入口的协议和拒绝行为由代码回归覆盖，不能把 merge 或代码检查称为新一轮负载运行。报告工具的纯 guard 检查也不能代替真实容量报告。当前环境四份调度归档已通过，实际快照间隔 3,697.525889 / 3,680.996285 / 3,667.114568 秒；不能宣称精确每小时或一小时 RPO 保证。

## 固定环境和运行前条件

主项目所有业务请求经 `http://127.0.0.1:58090` 的 HAProxy。正式负载使用独立 `contextfence-bench` Compose project，其 HAProxy 为 `http://127.0.0.1:58095`，Prometheus 为 59095，Grafana 为 53005；工具只接受这两组预定回环入口，独立端口还必须对应 bench 项目名。API 与 PostgreSQL 不应有宿主机业务端口。压测由固定 Linux 客户端执行；单机 WSL 运行时，把共享 CPU、内存与 I/O 干扰写入结果的适用范围。正式压测期间不构建镜像、不调整资源、不运行故障或恢复演练。

当前实际高频运行目录在 WSL/Linux ext4，公开不披露私有绝对路径。复现时建议将仓库克隆到 Linux home 目录的本地文件系统，可参考 Docker 的 [WSL bind mount 指南](https://docs.docker.com/desktop/features/wsl/best-practices/)；独立保存 Windows 挂载目录阶段的失败记录，两个阶段的资源与调度时间序列不能混合验收，也不以迁移推定根因。

受测 bench 代理配置显式 `nbthread 1`，保留实时文件门禁；受控应用核验镜像和 CPU/内存限制相同，并验证关闭态 503 与恢复后安全烟测。配置应用成功与容量验收分别记录，不能由健康恢复推定容量改善或故障根因。恢复后的启动路径已改为当前数据库服务并使用 `--no-deps`，其代码检查已通过；真实恢复后 stop→start 列为后续验证。bench 已完成停止交接，后续实验需重新取得正常基线。

先完成双实例权限烟测、监控抓取与告警验收。`.local/identities.json` 必须包含至少两个完整合成租户，默认使用 acme、beta、gamma、delta 四个租户的 writer 与 reader/producer。工具读取已有凭据，不生成或猜测 token。实际身份、能力令牌、原始数据、私有工具输出和备份均留在忽略目录。

独立 bench 可放在主项目忽略的 `.local/projects/bench`，或 Linux 原生文件系统上的独立工作目录；当前实际运行采用后者，私有绝对路径不公开。只复制必要公开 Compose、Dockerfiles、ops、scripts、load、pom/src 和已校验的 k6 可执行文件；首次 initialize 生成独立身份、数据库与 Grafana 凭据，不能复制主项目 `.env` 或 `.local`。复用已实测 API image ID 与 Grafana 镜像摘要，不重建应用作为基线。当前冻结前 VM 实际为 **20 CPUs、8,162,697,216 字节（7.602104 GiB）内存**，两 API 连接池均为 16；正式启动前确认其他服务栈已停，避免共享资源干扰。以下负载命令均应在 bench 根目录执行，结果进入 bench 自己的 `artifacts/local`；负载生成的来源不应混入主项目恢复权威账本。

压测器固定使用 [Grafana 官方 k6 v1.3.0 发布](https://github.com/grafana/k6/releases/tag/v1.3.0)。本项目选定 Linux amd64 包的 SHA-256 为 `84d26fc1f7bc03e02f2e016b3b1b20c032e05dfe461fca82de4e3a6ebe72ddbd`。下载与校验完成前不得执行该文件；执行版本和应用、基础设施摘要写入运行环境清单。

```bash
mkdir -p .tools/k6
curl --fail --location --proto '=https' --tlsv1.2 --output .tools/k6-v1.3.0-linux-amd64.tar.gz https://github.com/grafana/k6/releases/download/v1.3.0/k6-v1.3.0-linux-amd64.tar.gz
printf '%s  %s\n' '84d26fc1f7bc03e02f2e016b3b1b20c032e05dfe461fca82de4e3a6ebe72ddbd' '.tools/k6-v1.3.0-linux-amd64.tar.gz' | sha256sum --check --status
tar --extract --gzip --file .tools/k6-v1.3.0-linux-amd64.tar.gz --to-stdout k6-v1.3.0-linux-amd64/k6 > .tools/k6/k6
chmod 0755 .tools/k6/k6
.tools/k6/k6 version
```

## 负载与夹具

[k6 的 constant-arrival-rate](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/) 固定每秒启动的业务迭代数。一迭代恰好预约一次业务请求，不以响应速度闭环调低到达率。

| 场景 | 业务比例 | 数据与实例状态 |
| --- | --- | --- |
| cold | 100% assemble | seed 在重启前完成；重启两 JVM 清缓存，等待 ready，不做 assemble 预热；测量 60 秒 |
| hot | 100% assemble | 单租户、同一 SOURCE+DERIVED 批次，稳定缓存热点 |
| single | 90% assemble、10% source-event | 同一租户的八个来源，全部争用其完整租户锁 |
| multi | 同一总 RPS 下 90% assemble、10% source-event | 默认四租户均匀分摊总请求，每个租户也保持 90/10 |

固定合成夹具版本为 synthetic-v1：每租户八来源、正文 1024 字节、派生深度一、assemble 批次两个上下文。热点写使用相同 content/readers/state 的完整来源快照，以新 sequence 刷新 fresh_until；这是执行真实租户写锁的写负载，正文版本与授权 epoch 保持不变。该结果不能外推到正文更新、ACL 撤销或删除比例不同的业务。

feeder 每 60 秒以新 sequence 续期，fresh_until 固定为预约时刻后 240 秒，给数据库允许的 300 秒上限留余量。每 300 秒注册完整 SOURCE 与 DERIVED 新 cohort，全部成功后原子发布新 handles。context TTL 为 900 秒，旧 cohort 在正常轮换时仍有余量。每 VU 缓存本机 fixture 快照最多一秒。

每个 k6 写迭代先向本机 fixture 服务预约唯一 sequence 和 fresh_until；周期续期与业务写共享同一分配器。同序异参会造成 EVENT_CONFLICT，因此任何未经完整 fixture 校验的 409 都计为失败。乱序写仅接受与该请求的 source_id、sequence、content_version、auth_epoch 精确对应的 APPLIED 或 IGNORED_STALE。

fixture 服务只监听回环地址，使用临时能力令牌保护。辅助 snapshot/reserve 请求与 feeder 请求单独记录，不进入业务 RPS、失败率和业务 E2E 分位数。辅助流量仍占用客户端或服务资源，应随报告披露。

响应契约按递归语义等值比较对象，忽略对象键的插入顺序，同时严格比较参与校验的全部字段、值类型和数组顺序；来源绑定使用既有来源规范化规则。不能用 `JSON.stringify` 字符串顺序是否相同替代这个契约。2026-10-05 的真实 k6 诊断曾观察到语义全部正确而旧分类器误判；修订、旧失败与新版开发复验分别保留在上述运行证据中。

## 先约定政策，再探测和冻结

先建立不可覆盖的 policy。本方法的默认分位数回归容差为 20%；如选择不同值，必须在第一次 probe 之前写入政策并保持不变。默认预分配 512 VU、最多 2048 VU；客户端受限或 dropped iterations 不能解释为服务容量。

```bash
python3 -m load.runner policy --output artifacts/local/load-policy.json --tolerance 0.20 --vus 512 --max-vus 2048 --tenants 4
python3 -m load.runner probe --policy artifacts/local/load-policy.json --k6 .tools/k6/k6 --output artifacts/local/probes
```

probe 对 hot/single/multi 各执行 10、25、50、100、200 RPS，每项预热 30 秒、测量 60 秒，共 15 项。开发时可以运行以下短探测；它保存 `kind=shortprobe`，不允许替代正式窗口或用于冻结。

```bash
python3 -m load.runner shortprobe --policy artifacts/local/load-policy.json --k6 .tools/k6/k6 --scenario multi --rps 10 --seconds 10 --output artifacts/local/development-probe
```

额定 L 必须在三个场景都满足非预期失败率不超过 1%、零错误放行、无 dropped iterations、业务实际到达率至少计划值的 99%、无客户端或采样失败。低档也必须通过。结合资源与等待曲线确认没有持续等待恶化。原始 policy 预先固定持续等待增长审阅界限为 1.20：对测量窗口首尾各三分之一比较 Hikari pending 与 PG 锁等待，持续非零并且后段平均值高于前段 20% 应拒绝冻结；前段为零、后段持续非零也应拒绝。保留人工审阅的依据与时间，不因目标吞吐难达而事后更改门槛。

把实际资源审阅写入 `artifacts/local/resource-review.json`，只有核对原始曲线后才能把相应字段设为 true。例如初始待审状态：

```json
{
  "resource_stable": false,
  "client_not_limited": false,
  "no_sustained_wait_growth": false,
  "reviewed_before_formal_matrix": false,
  "evidence": [],
  "reviewed_at": null
}
```

低、额定与过载观察档必须来自已观测 probe；过载观察档用于观察退化，不要求通过容量门槛。当前必需探测全部通过到 200 RPS，追加 400 RPS 三项均不具容量资格。single 在 400 RPS 的非预期响应为 324/23,693（1.36749%），PG CPU 均值 97.34%、锁等待首尾 17→24.5；hot/multi 另有客户端、feeder 与资源例外。因此 400 是本轮观察档，不是绝对最大容量。

当必需阶梯直到 200 RPS 均通过时，使用公共入口追加更高档，而不是由尚未观察到的负载推定过载档。以下命令在新的演练环境中追加 400 RPS 的 hot/single/multi 三项，每项仍为 30 秒预热、60 秒测量，再合并初始与追加索引：

```bash
python3 scripts/probe_step.py run --policy artifacts/local/load-policy.json --initial-probes artifacts/local/probes/index.json --rps 400 --k6 .tools/k6/k6 --output artifacts/local/additional-probes-400
python3 scripts/probe_step.py merge --policy artifacts/local/load-policy.json --inputs artifacts/local/probes/index.json artifacts/local/additional-probes-400/index.json --output artifacts/local/probe-freeze-evidence-v1.json
```

run 要求 Linux、相同政策和受测环境、新输出目录，并将追加值限制在已观察最高阶梯之上且不超过 3200 RPS。merge 核对原请求计数、完成元数据、资源和源文件摘要，保留失败观察，只调整相对证据目录引用。协议执行完成与容量合格分别报告；工具不代替人工资源审阅，也不自动批准或冻结档位。输出只允许位于忽略的 `artifacts/local`，已有文件不能覆盖。

本次公共 merge 已对留存的 18 项探测实际执行通过，生成的新合并索引 SHA-256 为 `c4dba6b725dd25d58af4bbdb4032f21fa50f1972b52ae95095b1a90a4ef44d73`。原文件与原冻结文件保持不变，非零 k6 退出及 400 RPS 容量失败保留；此操作没有重跑负载，也未重新冻结目标。

当前实际冻结使用包含必需 15 项和追加 3 项的合并 probe 索引；下列命令的输入必须指向核验过的真实合并索引，不能把仅含最高 200 RPS 的旧索引作为 400 RPS 的证据：

```bash
python3 -m load.runner freeze --policy artifacts/local/load-policy.json --probes artifacts/local/probe-freeze-evidence-v1.json --resource-review artifacts/local/resource-review.json --low 25 --rated 200 --overload 400 --output artifacts/local/frozen-capacity.json
python3 -m load.runner plan --freeze artifacts/local/frozen-capacity.json --output artifacts/local/formal-plan.json
```

freeze 不覆盖已有文件，以额定档三个场景中较大的原始 p95/p99 作为基线，按政策容差冻结回归上限，并写入 node-exporter 的 p99 告警阈值。当前 18 次预探测的 72 份原始文件摘要已在冻结前重新核验；额定基线最大 p95/p99 为 15/85 ms，按预先固定的 20% 容差得到 18/102 ms，实际阈值读回为 0.102 秒。修改目标后需要新的政策、探测与独立结果组，不能覆盖原基线。

## 后续正式矩阵与续跑

正式矩阵固定为三个稳态场景 × 三档 × 三轮，共 27 项，每项预热 300 秒、测量 600 秒；冷启动为三档 × 三次，共九项，每次独立重启且测量 60 秒。36 项纯协议合计 24,840 秒，即 6 小时 54 分钟；seed、启动与证据收集另计。不存在缩短正式窗口的参数。

每个到达率场景固定 `gracefulStop: '40s'`，让已发出的请求在阶段停止后完成或超时；其请求仍按原阶段标记进入 E2E 样本。该宽限不延长测量启动窗口，也不将 300 秒预热、600 秒测量或冷启动的 60 秒改成其他时长。40 秒宽限及环境准备、采样和收集耗时另行记录，不包含在上述纯协议时间内。

```bash
python3 -m load.runner matrix --policy artifacts/local/load-policy.json --freeze artifacts/local/frozen-capacity.json --k6 .tools/k6/k6 --output artifacts/local/formal-matrix
```

中断后，在环境与脚本保持一致的情况下，用同一命令加 `--resume`。工具仅接受与冻结矩阵匹配的已完成前缀；未完成的单轮从头重跑并保留旧尝试。不得拼接一轮里不同时期的部分样本。`protocol_complete=true` 只表示全部协议执行过，不等于所有档位通过容量门槛；必须逐轮报告容量、回归与过载结论。

## 证据与分析

每轮保存 k6 原始 JSON 流、独立 feeder 元数据、压测器 CPU/RSS、宿主机 CPU/内存/磁盘/I/O、两 API 的 Hikari/JVM/GC 指标和独立 Java 子进程 RSS，以及 PG 连接与锁等待样本；保存实际到达率、dropped iterations、VU 上限、退出码、脚本与镜像摘要。采样周期以 5 秒为目标，计算本轮工作耗时后只等待剩余时间；超预算间隔及观测失败按实际时间戳保留，不能宣称每个样本间隔都恰好五秒或补造缺失样本。sampler 只记录聚合元数据，不记录数据库 SQL、应用正文、token 或进程环境。

采样器将两 API Java RSS、容器资源、数据库聚合与 Prometheus 查询组成五组固定只读 I/O 并行执行，等待所有 future 完成后由协调线程统一组装和写入样本。实际旧串行诊断为 6.832385–7.558658 秒；新版完整 v5 预探测通过其实际窗口的资源检查，正式长窗口仍需核对全部原始时间戳、覆盖与缺口。原始 k6 时间戳具有显式时区偏移时按同一时刻分析，导出可统一为 UTC；不修改原始字节或截断时间精度，自产执行、索引与资源记录仍按 UTC 契约核验。

API RSS 由固定 Compose project 内的 exec 观察，目标只允许 api-a、api-b。读取 docker-init 的唯一 Java 子进程状态，核对选择标记、父子关系和进程名，再提取正值 RSS 字节，标注 `source=java-init-child-proc-status`；只读进程观察不依赖 Prometheus 已导出 RSS。无法唯一选择或状态字段不合法时保留观测失败。公开报告仅保留 RSS 数值及来源，不公开实际进程标识、父进程标识、私有路径或原始状态。

受测身份关联实际镜像、`application_source_sha256` 和负载脚本摘要。Git 基准提交及工作树是否有变更只作为环境元数据；有未提交变更时，源码摘要用于识别实际受测内容，不将基准提交冒称为已通过的新提交。

业务 `cf_e2e_ms` 从发送调用前到完整响应或超时后计时，包含连接、请求、响应和等待。分析保留超时原始时长。分别报告总体、成功、失败以及预期拒绝的 p50/p95/p99/max；client_failure（未发出业务请求）计入失败率，但不虚构其网络 E2E。

对所分析的每个阶段，逐 raw 文件和 read/write 操作分别累计 `cf_business_started`、带 `traffic=business` 的 `http_reqs` 与 `cf_e2e_ms` 中 `issued=1` 的样本数，三者必须严格相等。缺少最后一条结果、非法标签或不完整计数直接拒绝证据；其他文件、阶段或操作的样本不能补齐缺口。这里的证据完整性门槛不使用 1% 到达率容差，1% 门槛仅用于已完整请求证据上的容量判定。

403/409 只有 status、body code、receipt decision、context IDs、来源绑定与 fixture 一致，且正文为空、无合成正文标记泄露时，才独立分类为 expected_denial。错误放行独立计数并使容量验收失败。稳态场景的正常夹具预期是 ALLOWED，突然过期或冲突的 409 属于非预期失败。

合并三轮必须重新读取各轮原始请求样本，不能平均三轮 p95/p99。例如三轮稳态的合并测量时长是 1800 秒：

```bash
python3 -m load.analyze artifacts/local/formal-matrix/RUN_ONE/raw.jsonl artifacts/local/formal-matrix/RUN_TWO/raw.jsonl artifacts/local/formal-matrix/RUN_THREE/raw.jsonl --seconds 1800 --rps "${RATED_RPS:?}" --output artifacts/local/merged-rated.json
```

## 客户端结果告警

`ServerFailureRate` 只观察服务端 5xx。正常夹具意外收到 403、409、410 等响应，也必须按客户端已验证契约判为异常，而不能因为不是 5xx 而消失。

runner 每秒读取 k6 原始 `cf_e2e_ms`，仅计入已发出的业务请求 `issued=1`，累计导出 `contextfence_load_requests_total`、`contextfence_load_unexpected_total` 和 `contextfence_load_unsafe_allow_total`。来源为实际 classifier 的结果，计数包括预热与测量；容量分位数仍只分析测量窗口。辅助 http_reqs、feeder 请求和未发出的 client_failure 不进入这些已发出业务计数，未发出的失败仍阻止容量验收。

403/409 通过完整 expected_denial 校验时进入业务总数但不进入 unexpected；正常 ALLOWED 夹具意外返回 410 时进入 unexpected。textfile 和状态只使用 cold/hot/single/multi 四个固定 scenario 标签，不记录 context/source ID、正文、token 或 URL。计数跨独立轮次累积，单轮 raw 的半行等待完整换行后才计数，尾部只读一次；导出失败在每轮报告中明确记录并阻止容量声明。

`LoadUnexpectedFailureRate` 在两分钟窗口的业务增量至少 100、非预期比例超过 1% 时触发；`LoadUnsafeAllow` 在同窗口观察到任意一次错误放行时触发。停止事件后告警按窗口和 Alertmanager 的配置恢复。修改规则后须重新加载 Prometheus，实际故障记录仍要保存触发与恢复时间，不能仅靠规则表达式或单元测试宣布告警链路通过。

## 后续单变量优化对照

根据真实 Hikari pending、PG 锁与资源证据选择一个瓶颈。现有 `compare` 支持仅改变两实例连接池上限：同一镜像、CPU、内存、脚本、客户端、数据集和冻结档位保持不变；从 Prometheus 实测两实例 pool max，而不是只相信声明。资源、镜像或客户端同时变化会被拒绝。

修改前保存配置 diff 和瓶颈证据，并写入 change 文件，例如从实际基线 16 到经审阅候选 8 时：

```json
{"parameter":"connection_pool_maximum_size","before":16,"after":8,"configuration_reviewed":true,"evidence":["reviewed-config-diff","baseline-pool-and-lock-samples"]}
```

完成配置更新后，重做权限、幂等和并发正确性验收，再运行同一冻结额定档的三个稳态场景，各三轮 300+600 秒，共九项。若实测没有改善，应如实报告，不能称为性能优化成功。

```bash
python3 -m load.runner compare --policy artifacts/local/load-policy.json --freeze artifacts/local/frozen-capacity.json --change artifacts/local/pool-change.json --k6 .tools/k6/k6 --output artifacts/local/pool-comparison
```

## 独立故障演练

一次只运行一项，避开正式负载和其他演练。每项使用新合成来源 ID 并先检查 Compose project 标签、无 API/PG 宿主机端口及正常入口。结果文件必须为新的 `artifacts/local` 路径；失败保留记录并关闭入口。

```bash
python3 scripts/fault_drill.py instance-kill --instance api-a --output artifacts/local/drill-instance-a.json
python3 scripts/fault_drill.py instance-kill --instance api-b --output artifacts/local/drill-instance-b.json
python3 scripts/fault_drill.py database-disconnect --output artifacts/local/drill-db-network.json
python3 scripts/fault_drill.py commit-response-loss --output artifacts/local/drill-response-loss.json
python3 scripts/fault_drill.py hotspot-lock --output artifacts/local/drill-hot-lock.json
python3 scripts/fault_drill.py bad-release --candidate "${BAD_IMAGE:?}" --output artifacts/local/drill-bad-release.json
python3 scripts/fault_drill.py backup-failure --output artifacts/local/drill-backup-enospc.json
python3 scripts/fault_drill.py material-anomaly --backup "${VERIFIED_BACKUP:?}" --ledger "${COMPLETE_LEDGER:?}" --fixture "${RECOVERY_FIXTURE:?}" --output artifacts/local/drill-material-anomaly.json
```

数据库网络演练需要先构建 profile tools 的 db-fault-proxy。它的业务 TCP 仅在项目 backend 内转发；控制 HTTP 只监听该容器回环地址，使用项目限定的 docker exec 操作。演练先保存原 DB_HOST、把两 API 和 PG exporter 改经代理、重建 JVM 并预热。cut 同时关闭所有已建立的双向 socket 并停止转发新连接；报告必须证明至少两个旧连接已关闭、一次新 psql 连接失败、两个 ready 失败，以及暖缓存请求无正文。恢复代理后重做双实例安全烟测，最后恢复原 DB_HOST。额外 NOLOGIN 机制不替代此网络演练。

实例故障测量 HAProxy 15 秒摘除、60 秒告警与恢复告警，保留故障窗口内失败。提交后丢响应通过本机 shim 收到上游已提交响应后丢弃最后一跳，确认相同完整快照重放结果相同、同序异参 EVENT_CONFLICT、数据库只有一条事件；不宣称 HTTP exactly-once。热点锁为一个可识别事务持有 acme 的真实 tenant_guard 写锁，保存 PG blocker 与请求尾延迟。

实例注入使用项目限定的 `docker compose kill --signal SIGKILL`，保留原重启策略；报告记录独立 HAProxy 状态轮询，业务请求在单独线程持续进行，其连接超时不能占用 15 秒摘除观察期限。firing 的接收时间必须在实际注入 UTC 时间后 60 秒内，迟到记录不能通过较晚启动的 lookup 获得新期限；resolved 从开始恢复时另计期限。finally 启动被杀实例，随后双实例权限烟测。主项目每次正常合成 source 写入前先追加完整恢复 ledger；相同事件重放和故意冲突不追加重复或无效事件。所有报告仅使用固定的本地验证决策，候选响应的任意 code、正文和未知字段不会复制进报告。

坏版本调用 `scripts/deploy.py` 的完整测试、迁移与部署入口，保留真实退出码及独立 pipeline 证据。演练候选从当前合法镜像 digest 构建，迁移入口保持可运行而业务启动失败；接受条件为 ROLLED_BACK、两 API 的实际镜像恢复原 digest、双实例权限烟测通过，300 秒部署与回滚总门槛保持不变。

备份异常在真实 pg_dump 后的 host copy 边界写入 Linux `/dev/full`，注入实际 ENOSPC，验证 latest 没有替换、BackupFailed firing/resolved 与下一次成功备份。资料异常只损坏演练复制的 ledger checksum，调用恢复编排并验证两 gate 持续关闭；该项成功后入口仍关闭，必须通过完整恢复验收才可重新开放。

数据库真实恢复由 `scripts/restore.py` 与独立合成账本流程完成。故障验收和完整复盘还需要实际运行的七个时点、影响请求统计、定位样本、直接原因、放大因素、整改负责人/期限/复验；不得把上述单元测试或工具存在当作故障实测结果。
