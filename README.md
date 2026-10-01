# ContextFence

### A Java backend for context validity before agent use

**Keep managed context and its registered derivatives tied to current permissions, source versions, and expiry. Recheck every retrieval, even when another service instance already holds the body in cache.**

English | [中文](README_ZH.md)

[Quick Start](#quick-start) · [Features](#features) · [Validation](#validation) · [Documentation](#documentation)

![ContextFence checks current source state under a shared database lock before retrieving immutable cached bodies; source updates use an exclusive lock.](docs/images/contextfence-overview.svg)

*Architecture diagram: source updates and context retrieval share a PostgreSQL lock boundary. This is a flow illustration, not a runtime screenshot.*

![contextfence](docs/images/cartoon-infographic.png)

Alice reads a pricing policy and a trusted producer registers a summary derived from it. Instance A caches that summary. An administrator revokes Alice through instance B. Once the revocation is acknowledged, a new retrieval through either instance rejects the summary. Regranting access does not revive old context: its authorization epoch is stale.

## Features

- **Validity on every retrieval:** current source ACL, content version, authorization epoch, deletion state, and expiry are checked against the primary database.
- **Registered dependency tracking:** derived items inherit their parents' leaf sources and earliest expiry. A source change invalidates old registered descendants at retrieval time.
- **Multi-instance consistency:** shared read locks and exclusive source-update locks define the revocation boundary; body caches never store permission decisions.
- **Recoverable source events:** complete snapshots use monotonic sequence numbers and normalized hashes; replay recovers the original result, while conflicting duplicates return 409.
- **All-or-nothing results:** a mixed valid/invalid batch returns no bodies. Decision receipts contain metadata and reasons, without source bodies or credentials.

The project includes HTTP APIs, synthetic fixtures, two-instance scripts, and failure experiments. It does not require a model API key. There is no model invocation, chat UI, agent loop, or vector search.

## Quick Start

Run from the repository root. The container workflow needs Docker Engine/Desktop, the Docker Compose plugin, and Python 3. The first build needs network access.

```powershell
# Windows PowerShell
.\scripts\start.ps1
python scripts/demo.py
python scripts/report.py
# Open artifacts/local/report.html
.\scripts\stop.ps1
```

```bash
# macOS / Linux
bash scripts/start.sh
python3 scripts/demo.py
python3 scripts/report.py
# Open artifacts/local/report.html
bash scripts/stop.sh
```

Startup creates random local credentials in `.env` and `.local/identities.json`; repeated startup preserves them and the database. Default endpoints bind to loopback: API A at `58091`, API B at `58092`, PostgreSQL at `55448`. Change the ports in `.env` if needed. Stop scripts retain the database volume.

The demo creates new synthetic resources per run, warms both instances' caches, revokes access, then checks 50 concurrent retrievals initiated after acknowledgment. It also checks reauthorization, source updates, deletion, expiry, tenant isolation, and mixed batches. The generated report contains decision metadata, not protected bodies.

**Container validation is limited:** the recorded environment returned HTTP 500 from the Docker Engine API. Compose configuration was checked, but image build, container startup, and the Testcontainers startup branch were not exercised. The recorded backend run used two independent JVMs and real PostgreSQL 17.11. See [validation details](docs/validation.md).

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

The recorded Windows run used OpenJDK 21.0.1, Maven 3.8.5, and PostgreSQL 17.11:

| Check | Recorded result |
| --- | --- |
| Java `verify` | 39 tests passed: 9 unit + 30 integration; no failures, errors, or skips |
| Two independent JVM HTTP demo | 83 checked HTTP observations; 50/50 post-acknowledgment concurrent retrievals returned 403 without bodies |
| Deterministic concurrency and process termination | 7 experiments passed, including actual database lock blocking and warmed caches |
| Python script checks | 16 passed; 1 POSIX permissions check explicitly skipped on Windows |

These are reproducible local observations, not a production throughput claim. Read the [validation record](docs/validation.md) and [saved HTML report](docs/reports/report.html) for scope and evidence.

## Guarantee and Limits

**New retrievals initiated after successful revocation acknowledgment are rejected.** A request that overlaps revocation and acquires the shared lock first may complete; its response may arrive after the acknowledgment. Expiry is judged using database time sampled after lock acquisition, rather than response arrival time.

Data already delivered to a client or model cannot be recalled. Dependencies are registered by trusted producers; arbitrary text cannot prove its own complete provenance. Upstream changes become covered when their snapshots commit here. `fresh_until` limits the accepted age of known state; it is not real-time connector synchronization. Logical deletion blocks subsequent access but does not certify erasure from database history, cache, or backups.

The first version uses one primary database, tenant-level locks, and bounded per-item metadata queries. It makes no high-throughput promise. Identity token mapping is for the local demo; production identity providers, source connectors, and retention policies need separate integration.

## Documentation

- [API and error semantics](docs/api.md)
- [Architecture and concurrency boundary](docs/architecture.md)
- [Problem rationale and primary sources](docs/rationale.md)
- [Validation and reproduction](docs/validation.md)
- [Engineering discussion guide (中文)](docs/interview.md)
- [Saved two-instance report](docs/reports/report.html)

Java 21 · Spring Boot 4.1.1 · Spring Security/JDBC · PostgreSQL 17 · Flyway · Caffeine · JUnit 5 · Testcontainers
