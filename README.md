# ContextFence

### A Java backend for context validity before agent use

**Keep managed context and its registered derivatives tied to current permissions, source versions, and expiry. Recheck every retrieval, even when another service instance already holds the body in cache.**

English | [中文](README_ZH.md)

[Quick Start](#quick-start) · [Features](#features) · [Local Operations](#local-operations) · [Validation](#validation) · [Documentation](#documentation)

![ContextFence checks current source state under a shared database lock before retrieving immutable cached bodies; source updates use an exclusive lock.](docs/images/contextfence-overview.svg)

*Architecture diagram: source updates and context retrieval share a PostgreSQL lock boundary. This is a flow illustration, not a runtime screenshot.*

![contextfence](docs/images/cartoon-infographic.png)

*Concept illustration: the cartoon explains context invalidation; it is not a screenshot of the running stack.*

Alice reads a pricing policy and a trusted producer registers a summary derived from it. Instance A caches that summary. An administrator revokes Alice through instance B. Once the revocation is acknowledged, a new retrieval through either instance rejects the summary. Regranting access does not revive old context: its authorization epoch is stale.

## Features

- **Validity on every retrieval:** current source ACL, content version, authorization epoch, deletion state, and expiry are checked against the primary database.
- **Registered dependency tracking:** derived items inherit their parents' leaf sources and earliest expiry. A source change invalidates old registered descendants at retrieval time.
- **Multi-instance consistency:** shared read locks and exclusive source-update locks define the revocation boundary; body caches never store permission decisions.
- **Recoverable source events:** complete snapshots use monotonic sequence numbers and normalized hashes; replay recovers the original result, while conflicting duplicates return 409.
- **All-or-nothing results:** a mixed valid/invalid batch returns no bodies. Decision receipts contain metadata and reasons, without source bodies or credentials.
- **Local operating stack:** HAProxy fronts both API instances; Prometheus, Grafana, Alertmanager, and PostgreSQL/host exporters provide local monitoring. Startup and recovery open ingress after safety checks; deployment verifies each candidate before returning it to traffic, and backup verifies snapshots before publication.

The project includes HTTP APIs, synthetic fixtures, two-instance scripts, and failure experiments. It does not require a model API key. There is no model invocation, chat UI, agent loop, or vector search.

## Quick Start

Run from the repository root with Python 3, a Linux Docker Engine, and the Docker Compose plugin. Windows can use a Docker CLI connected to the WSL Linux engine, or run the Linux commands inside WSL. The first startup needs network access to the pinned base images and the official Grafana 12.2.0 archive. `start.py` downloads and verifies that archive's size and SHA-256, builds Grafana, and records its immutable image ID automatically.

For Linux/WSL runs with frequent file access, clone the repository into the Linux home directory on its native filesystem; see Docker's [WSL bind-mount guidance](https://docs.docker.com/desktop/features/wsl/best-practices/). The current bench runs on WSL/Linux ext4; earlier Windows-mounted filesystem attempts remain separate evidence. The move does not establish the cause of those failures or a capacity improvement.

```powershell
# Windows PowerShell
python scripts/start.py
python scripts/ops_smoke.py --instance both
# Inspect Prometheus and Grafana while the stack is running
python scripts/stop.py
```

```bash
# Linux / WSL
python3 scripts/start.py
python3 scripts/ops_smoke.py --instance both
# Inspect Prometheus and Grafana while the stack is running
python3 scripts/stop.py
```

Startup generates or preserves random local credentials in ignored `.env`, `.local/identities.json`, and `.local/secrets`. It closes persistent ingress, runs the separate migration and database-role checks, then checks both API instances before opening traffic. Stop preserves database volumes, backups, and credentials.

| Local endpoint | Purpose |
| --- | --- |
| [127.0.0.1:58090](http://127.0.0.1:58090) | Unified business ingress through HAProxy |
| [127.0.0.1:59090](http://127.0.0.1:59090) | Prometheus targets, metrics, and alerts |
| [127.0.0.1:53000](http://127.0.0.1:53000) | Provisioned Grafana dashboard; user `operator`, password in `.local/secrets/grafana-admin` |

All three host endpoints bind to loopback. API A/B and PostgreSQL have no host business ports. The current Compose workflow uses the unified ingress; the older `demo.py` defaults for directly exposed JVMs are documented only in the historical validation record.

The explicit two-instance smoke warms caches and checks permission revocation, stale authorization epochs, tenant isolation, identical replay, and sequence conflicts. Its output is `artifacts/local/ops-smoke.json`, containing decisions rather than protected bodies or credentials. See the [local runbook](docs/operations/runbook.md) for maintenance gates and diagnostics.

## Local Operations

Run these commands from the repository root with `python3` on Linux/WSL (`python` on Windows):

- **Deploy a candidate:** run `python3 scripts/deploy.py` to test, build, migrate, and roll out the image. Read [deployment and rollback](docs/operations/runbook.md#发布与回滚) and install the build/test tools described below.
- **Create a verified snapshot backup:** run `python3 scripts/backup.py`; see [backup and safe recovery](docs/operations/recovery.md).
- **Run hourly backup attempts:** keep `python3 scripts/backup_scheduler.py` running in the foreground and inspect backup metrics.
- **Perform a full recovery drill:** run `python3 scripts/restore_demo.py` in an isolated project or with a complete trusted source ledger. Read the [recovery prerequisites and safety checks](docs/operations/recovery.md) first.
- **Reproduce capacity and fault experiments:** follow the [capacity and fault method](docs/operations/capacity-method.md), including the fixed environment, evidence, formal windows, and comparison rules.

Deployment records the actual image IDs, drains and verifies instances in turn, and restores the previous images on failure when safety checks pass. Database schema is not rolled back with an image. Backup uses a live exported PostgreSQL snapshot and verifies the custom archive before publishing it. Recovery uses a new database volume, current identities, and a complete independent source ledger; the source volume is retained. These scripts and protocols are local operating tools, not evidence that every deployment, recovery, or capacity acceptance has passed.

## Build and Test

For a native build, install JDK 21 and Maven 3.6.3+. By default, integration tests start PostgreSQL with Testcontainers and require a working Docker engine.

```powershell
.\scripts\maven.ps1 verify
python -m unittest discover -s scripts/tests -v
```

```bash
mvn verify
python3 -m unittest discover -s scripts/tests -v
```

Alternatively, use a dedicated real PostgreSQL test database whose name ends in `_test`. Set the following variables in your local environment, replacing the placeholders:

```powershell
$env:TEST_DATABASE_URL='jdbc:postgresql://127.0.0.1:5432/contextfence_test'
$env:TEST_DATABASE_USER='your_test_user'
$env:TEST_DATABASE_PASSWORD='your_test_password'
.\scripts\maven.ps1 verify
python -m unittest discover -s scripts/tests -v
```

```bash
export TEST_DATABASE_URL='jdbc:postgresql://127.0.0.1:5432/contextfence_test'
export TEST_DATABASE_USER='your_test_user'
export TEST_DATABASE_PASSWORD='your_test_password'
mvn verify
python3 -m unittest discover -s scripts/tests -v
```

Tests use randomly named tenants and leave synthetic data in the test database. They do not delete the database. Missing PostgreSQL is a failure; tests do not fall back to H2 or silently skip integration checks.

To package only, run `mvn -DskipTests package` (PowerShell: `.\scripts\maven.ps1 '-DskipTests' package`). The artifact is `target/context-fence.jar`. Native startup uses `CONTEXTFENCE_DATABASE_URL`, `CONTEXTFENCE_DATABASE_USER`, `CONTEXTFENCE_DATABASE_PASSWORD`, and `CONTEXTFENCE_IDENTITIES_FILE`, then `java -jar target/context-fence.jar`. A second process uses a different `SERVER_PORT` with the same database and identity mapping. Keep actual credentials in ignored local configuration or environment variables. Request shapes and error semantics are in the [API reference](docs/api.md).

## Validation

The 2026-10-05 local SRE record uses Ubuntu 24.04.3 LTS under WSL with Docker Engine 29.1.3 and Compose 2.40.3. The complete Compose stack and the default verified official Grafana archive startup path have run successfully; the older Docker API HTTP 500 limitation belongs to the historical run below.

This delivery covers the observed local deployment, permissions, monitoring, fault drills, rollback, safe database recovery, short capacity probes, and frozen experiment settings. The 36-run formal capacity matrix, nine-run comparison, longer continuous observations, and cloud deployment are follow-up validation work. No accepted formal capacity result is published.

| Check | Current observed result |
| --- | --- |
| Java `verify` and runtime provenance | 77 passed: 32 unit + 45 integration; no failures, errors, or skips. All 71 production classes match the tested classes byte for byte in the running image |
| Final Python script regression | 243 discovered on each platform: Windows Python 3.9.25, 242 passed / 1 POSIX skip; Linux Python 3.12.3, 234 passed / 9 Windows-specific skips; no failures or errors |
| Main Compose stack | Both instances passed safety smoke; all 6 Prometheus targets were up; a real snapshot backup passed |
| API A/B termination drills | Both passed the 15-second HAProxy removal and 60-second alert targets: A 8.418 / 47.316 seconds; B 9.348 / 41.569 seconds |
| Database network disconnect | v5 passed: 13 existing connections cut; new connections rejected; both warmed-cache retrievals returned 503 without bodies; actual firing/resolved delivery observed |
| Bad-release deployment and rollback | The real `scripts/deploy.py` pipeline passed its fault acceptance: readiness failure at 118.395 seconds (budget 120), rollback accepted at 201.782 seconds (budget 300). The entire drill took 681.265 seconds |
| Independent new-volume recovery | Passed: RTO 44.006 seconds; snapshot age at failure 21.684 seconds. 1 of 5 confirmed contexts and 4 of 8 receipts were missing; 3 of 6 source-event records were rebuilt from the independent ledger, without their original application timestamps |
| Mixed-version safety | The actual legacy binary passed revocation, regrant, replay, conflict, and rollback checks; after recovery it rejected unexpired old source/derived contexts, rotated tokens, and partial mixed-batch results |
| Controlled metric alert delivery | Actual Prometheus → Alertmanager → receiver firing/resolved passed for unsafe-input, unexpected-result, and backup-age alerts; inputs were synthetic, with no observed service unsafe admission |
| Bounded disk alert drill | v2 passed: 89,920,512 bytes filled in an owned ext4 loop filesystem, leaving about 14.9972% available; actual `HostDiskLow` firing/resolved delivered. Cleanup passed; the 128 MiB image was retained |
| Sustained pool-waiting alert drill | v2 passed: four real failure, waiting, timeout, and tail-latency alert classes delivered firing/resolved; both alert planes were clear in all states at final acceptance. The first recovery-acceptance failure is retained |
| Bounded runtime log check | 8,070 captured lines / 5,075 valid structured request records; no invalid request records or duplicate request IDs. The overall privacy check failed because diagnostic SQL/database URLs remain in private logs; the beginning of the fault window was not covered |
| Bench preflight at 04:57 UTC | Passed: only the 10 bench containers running; 6 monitoring targets up; both observed pool maxima 16; 4 database-role checks passed. Actual Java-child RSS was read directly from process status |
| Scheduled snapshot backups | The current ext4 environment has four verified scheduled archives, with actual snapshot intervals of 3,697.525889 / 3,680.996285 / 3,667.114568 seconds. This verifies those attempts and their archives; it does not establish exact hourly timing or a one-hour RPO guarantee |
| Development short probes | First run failed: 101 issued, classified as 44 success / 57 unexpected, with all business HTTP statuses 200. v2 passed at 05:29 UTC: 100 issued / 100 success, exact request-count reconciliation and three resource samples about five seconds apart; first failure retained |
| Formal probes v1 | Failed and incomplete, process exit 1: hot10 had 600 successes but verdict export failed; hot25 recorded 912 timeouts, 93 client failures, 120 dropped iterations and feeder failure; hot50 initial seed failed. No capacity freeze |
| Controlled HAProxy configuration application | Passed at 05:53 UTC: HAProxy 3.2.6, two processes each with one thread, unchanged image and CPU/memory limits; closed-gate 503, both permission smokes, four role checks and all six monitoring targets passed. Capacity effect and root cause remain unproven |
| Current native-filesystem bench | Literal startup passed at 06:09 UTC; 06:11 preflight passed with 10 bench containers, six targets, pool maxima 16/16, four roles and observed Java child RSS. Development short probe v3 passed: 101 successes, no unsafe/drop/sampling errors, exact read/write counts; three samples had median / maximum intervals of 5.485785 / 5.971443 seconds |
| Complete pre-freeze probes v5 | All 15 hot/single/multi × 10/25/50/100/200 RPS probes passed the original-request, completion-metadata and resource checks; no business, unsafe, drop, feeder, export or sampling errors. Each used 30 seconds of warmup and 60 seconds of measurement |
| Additional 400 RPS probes | All three completed but were ineligible for capacity acceptance. Single recorded 324 unexpected responses / 23,693 issued requests (1.36749%), 308 drops, PostgreSQL CPU mean 97.34%, and sustained lock-wait growth from 17 to 24.5. Hot/multi also retain client failures, feeder failures and resource exceptions |
| Actual capacity freeze at 09:15 UTC | Low/rated/overload-observation steps are 25/200/400 RPS; regression limits are p95 18 ms / p99 102 ms. All 72 original request/resource/metadata files were reverified; the actual p99 metric readback is 0.102 seconds. The formal 36 + 9 runs are follow-up validation |

The final Python runs completed at 2026-10-05 10:10:16.844681 UTC on Windows and 10:13:39.693288 UTC on Linux using the same unchanged source digest, [recorded in validation](docs/validation.md). The earlier 216-test checks at about 07:31 UTC remain dated history in the [runtime evidence](docs/operations/runtime-evidence.md#历史脚本验证点). Further source changes require a fresh regression record. The JavaScript contract checks used Windows Node v24.15.0, including WSL interoperability for the Linux Python run; native Linux Node was not validated. The five-second RSS command bound and the single completion timestamp were exercised in the complete v5 probes. The public supplemental-probe merge also ran successfully against all 18 existing observations, retaining the failed 400 RPS results without rerunning load. Mock-based alert checks and pure report-generator guard checks retain their code-check scope and do not establish runtime alert delivery or a completed capacity report.

Earlier v1/v2 failures and incomplete attempts, v3 RSS-read failures, and v4's 15 inconsistent completion timestamps remain preserved. The WSL/Linux ext4 bench used an observed 20 CPUs and 8,162,697,216 bytes (7.602104 GiB) of VM memory, with no second operating stack. Its v5 probes and additional 400 RPS observations support the frozen experiment steps, not a maximum-capacity claim. The 36-run capacity matrix (6 hours 54 minutes), nine-run single-variable comparison (2 hours 15 minutes), and longer backup observation are documented follow-up validation. No accepted formal capacity result is published. Fault resource records retain sampling gaps; recovery does not claim zero data loss. See the [current validation scope](docs/validation.md), [sanitized runtime evidence](docs/operations/runtime-evidence.md), [database-disconnect postmortem](docs/operations/postmortem-db-disconnect.md), and [capacity method](docs/operations/capacity-method.md).

The bench handoff passed at 10:17 UTC: its owned scheduler stopped, no bench containers remained, both ingress gates were closed, and the database volume was retained. The main and recovery stacks also remain stopped with volumes and credentials retained. The restore-aware startup fix passed code checks; an actual post-recovery stop/start run is follow-up validation. Local results do not establish production HA, cloud deployment, CI/CD, or off-host disaster recovery. The bounded runtime log privacy check remains failed, with raw diagnostic logs kept private.

The preserved **2026-09-29 historical Windows run** used OpenJDK 21.0.1, Maven 3.8.5, and PostgreSQL 17.11:

| Check | Recorded result |
| --- | --- |
| Java `verify` | 39 tests passed: 9 unit + 30 integration; no failures, errors, or skips |
| Two independent JVM HTTP demo | 83 checked HTTP observations; 50/50 post-acknowledgment concurrent retrievals returned 403 without bodies |
| Deterministic concurrency and process termination | 7 experiments passed, including actual database lock blocking and warmed caches |
| Python script checks | 16 passed; 1 POSIX permissions check explicitly skipped on Windows |

These are reproducible local observations, not a production throughput claim. Read the [validation record](docs/validation.md) for current and historical scope. The [saved HTML report](docs/reports/report.html) belongs to the 2026-09-29 JVM run and is not evidence for the current operating stack.

## Guarantee and Limits

**New retrievals initiated after successful revocation acknowledgment are rejected.** A request that overlaps revocation and acquires the shared lock first may complete; its response may arrive after the acknowledgment. Expiry is judged using database time sampled after lock acquisition, rather than response arrival time.

Data already delivered to a client or model cannot be recalled. Dependencies are registered by trusted producers; arbitrary text cannot prove its own complete provenance. Upstream changes become covered when their snapshots commit here. `fresh_until` limits the accepted age of known state; it is not real-time connector synchronization. Logical deletion blocks subsequent access but does not certify erasure from database history, cache, or backups.

The first version uses one primary database, tenant-level locks, and bounded per-item metadata queries. It makes no high-throughput promise. Identity token mapping is for the local demo; production identity providers, source connectors, and retention policies need separate integration.

Two API instances share one PostgreSQL. HAProxy, the database, and the Docker host remain single points of failure; local failover observations do not establish production high availability or off-host disaster recovery.

## Documentation

- [API and error semantics](docs/api.md)
- [Architecture and concurrency boundary](docs/architecture.md)
- [Problem rationale and primary sources](docs/rationale.md)
- [Validation and reproduction](docs/validation.md)
- [Local startup, maintenance, monitoring, and deployment runbook (中文)](docs/operations/runbook.md)
- [Database backup and safe recovery (中文)](docs/operations/recovery.md)
- [Sanitized current runtime evidence (中文)](docs/operations/runtime-evidence.md)
- [Database network disconnect postmortem (中文)](docs/operations/postmortem-db-disconnect.md)
- [Capacity, comparison, and fault validation method (中文)](docs/operations/capacity-method.md)
- [Engineering discussion guide (中文)](docs/interview.md)
- [Historical two-instance report (2026-09-29)](docs/reports/report.html)

Java 21 · Spring Boot 4.1.1 · Spring Security/JDBC · PostgreSQL 17 · Flyway · Caffeine · JUnit 5 · Testcontainers
