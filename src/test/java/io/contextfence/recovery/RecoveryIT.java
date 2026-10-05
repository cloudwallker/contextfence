package io.contextfence.recovery;

import com.zaxxer.hikari.*;
import io.contextfence.api.Contracts.*;
import io.contextfence.admission.AdmissionService;
import io.contextfence.audit.ReceiptService;
import io.contextfence.common.Json;
import io.contextfence.common.Problem;
import io.contextfence.context.*;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import io.contextfence.sources.SourceService;
import io.contextfence.support.TestDatabase;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import java.time.Instant;
import java.util.*;
import static org.assertj.core.api.Assertions.*;

/** Isolated schema on real PostgreSQL: never rewrites other integration fixtures. */
class RecoveryIT {
    HikariDataSource pool;
    Database db, administration;
    String schema;
    Caller writer = new Caller("acme", "writer", Set.of(Role.SOURCE_WRITER));
    Caller alice = new Caller("acme", "alice", Set.of(Role.READER, Role.PRODUCER));
    ContextService contexts;
    AdmissionService admission;
    SourceService sources;
    BodyCache cache;
    Instant fresh;
    SourceEvent first;
    ContextHandle root, child;

    @BeforeEach void setup() {
        var test = TestDatabase.shared();
        administration = new Database(test.dataSource);
        schema = "recovery_" + UUID.randomUUID().toString().replace("-", "");
        administration.jdbc().execute("create schema " + schema);
        var config = new HikariConfig();
        config.setJdbcUrl(test.url); config.setUsername(test.user); config.setPassword(test.password);
        config.setSchema(schema); config.setMaximumPoolSize(3);
        pool = new HikariDataSource(config);
        Flyway.configure().dataSource(pool).defaultSchema(schema).cleanDisabled(true).load().migrate();
        db = new Database(pool); sources = new SourceService(db); contexts = new ContextService(db);
        cache = new BodyCache(); admission = new AdmissionService(db, cache);
        fresh = db.now().plusSeconds(180);
        first = new SourceEvent("policy", 1L, "SYNTHETIC-OLD-BODY", List.of("alice", "bob"), "ACTIVE", fresh);
        sources.apply(writer, first);
        root = contexts.createSource(alice, new SourceContextRequest("policy", 300));
        child = contexts.createDerived(alice, new DerivedContextRequest("SYNTHETIC-OLD-SUMMARY", List.of(root.id()), 300));
        admission.assemble(alice, new AssembleRequest(List.of(root.id(), child.id())));
        admission.assemble(alice, new AssembleRequest(List.of(root.id(), child.id())));
        assertThat(cache.stats().hitCount()).isPositive();
    }

    @AfterEach void cleanup() {
        if (pool != null) pool.close();
        if (schema != null) administration.jdbc().execute("drop schema " + schema + " cascade");
    }

    SourceLedger ledger(SourceEvent... events) {
        List<Map<String,Object>> rows = new ArrayList<>();
        for (var event : events) rows.add(Map.of("tenant", "acme", "source_id", event.sourceId(), "sequence", event.sequence(),
                "content", event.content(), "readers", event.readers(), "state", event.state(), "fresh_until", event.freshUntil().toString()));
        return SourceLedger.fromJson(SourceLedgerTest.document(Json.write(rows)));
    }

    @Test void restoresLatestAclAndRetiresEveryOldBodyEvenWhenTheCacheIsWarm() {
        var previousReceipt = admission.assemble(alice, new AssembleRequest(List.of(root.id(), child.id()))).receipt();
        var revoke = new SourceEvent("policy", 2L, first.content(), List.of("bob"), "ACTIVE", fresh);
        var report = new RecoveryReconciler(pool).reconcile(ledger(first, revoke));
        assertThat(report.retiredContexts()).isEqualTo(2);
        assertThat(db.jdbc().queryForObject("select readers::text from source_state", String.class)).isEqualTo("[\"bob\"]");
        assertThat(db.jdbc().queryForObject("select count(*) from context_items where content<>''", Integer.class)).isZero();
        var denied = admission.assemble(alice, new AssembleRequest(List.of(root.id(), child.id())));
        assertThat(denied.code()).isEqualTo("CONTEXT_RETIRED"); assertThat(denied.status()).isEqualTo(410);
        assertThat(denied.items()).isEmpty();
        assertThat(Json.write(new ReceiptService(db).get(alice, previousReceipt.id()))).doesNotContain(first.content(), "SYNTHETIC-OLD-SUMMARY");
        assertThat(sources.apply(writer, first).authEpoch()).isEqualTo(1);
        assertThat(db.jdbc().queryForObject("select auth_epoch>(select max(auth_epoch) from context_sources) from source_state", Boolean.class)).isTrue();
        assertThatThrownBy(() -> contexts.createDerived(alice, new DerivedContextRequest("replacement", List.of(child.id()), 300)))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("CONTEXT_RETIRED"));
    }

    @Test void recoveryEpochProtectsOldBinariesAndNewContextsCanBeRegisteredFromLatestAuthority() {
        // Simulate a source whose restored context binding exceeds ledger epochs.
        db.jdbc().update("update context_sources set auth_epoch=90");
        var report = new RecoveryReconciler(pool).reconcile(ledger(first));
        assertThat(report.sources()).hasSize(1);
        assertThat(report.sources().getFirst().rebuiltEpoch()).isEqualTo(1);
        assertThat(report.sources().getFirst().restoredEpoch()).isEqualTo(91);
        assertThat(db.jdbc().queryForObject("select auth_epoch from source_state", Long.class)).isEqualTo(91);
        assertThat(db.jdbc().queryForObject("select bool_and(cs.auth_epoch<>s.auth_epoch) from context_sources cs join source_state s using(tenant,source_id)", Boolean.class)).isTrue();
        var current = contexts.createSource(alice, new SourceContextRequest("policy", 300));
        var derived = contexts.createDerived(alice, new DerivedContextRequest("SYNTHETIC-NEW", List.of(current.id()), 300));
        assertThat(admission.assemble(alice, new AssembleRequest(List.of(current.id(), derived.id()))).status()).isEqualTo(200);
        var mixed = admission.assemble(alice, new AssembleRequest(List.of(current.id(), root.id())));
        assertThat(mixed.items()).isEmpty(); assertThat(mixed.code()).isEqualTo("CONTEXT_RETIRED");
        var otherTenant = new Caller("beta", "alice", Set.of(Role.READER));
        assertThatThrownBy(() -> admission.assemble(otherTenant, new AssembleRequest(List.of(current.id()))))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.status()).isEqualTo(404));
    }

    @Test void missingSourceHistoryAndDatabaseFailureCannotLeavePartlyRetiredState() {
        sources.apply(writer, new SourceEvent("missing", 1L, "synthetic", List.of("alice"), "ACTIVE", fresh));
        assertThatThrownBy(() -> new RecoveryReconciler(pool).reconcile(ledger(first))).isInstanceOf(IllegalArgumentException.class)
                .hasMessage("INCOMPLETE_SOURCE_LEDGER");
        assertThat(db.jdbc().queryForObject("select count(*) from context_items where retired_at is not null", Integer.class)).isZero();
        var missing = new SourceEvent("missing", 1L, "synthetic", List.of("alice"), "ACTIVE", fresh);
        db.jdbc().execute("create function fail_restore() returns trigger language plpgsql as $$begin raise exception 'synthetic transaction failure'; end$$");
        db.jdbc().execute("create trigger fail_restore before update on source_state for each row execute function fail_restore()");
        assertThatThrownBy(() -> new RecoveryReconciler(pool).reconcile(ledger(first, missing))).isInstanceOf(RuntimeException.class);
        assertThat(db.jdbc().queryForObject("select count(*) from context_items where retired_at is not null", Integer.class)).isZero();
        assertThat(db.jdbc().queryForObject("select content from context_items where id=?", String.class, root.id())).isEqualTo(first.content());
    }

    @Test void expiredLedgerNeverAutomaticallyRenewsSourceFreshness() {
        var expired = new SourceEvent("policy", 2L, first.content(), first.readers(), "ACTIVE", db.now().minusSeconds(1));
        new RecoveryReconciler(pool).reconcile(ledger(first, expired));
        assertThatThrownBy(() -> contexts.createSource(alice, new SourceContextRequest("policy", 300)))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("SOURCE_UNVERIFIED"));
    }

    @Test void preservesHistoricalReplayResultsWhileRebuildingTheCompleteSourceHistory() {
        var third = new SourceEvent("policy", 3L, "SYNTHETIC-LATEST", List.of("bob"), "ACTIVE", fresh);
        var second = new SourceEvent("policy", 2L, "SYNTHETIC-MIDDLE", first.readers(), "ACTIVE", fresh);
        var originallyApplied = sources.apply(writer, third);
        var originallyStale = sources.apply(writer, second);
        assertThat(originallyStale.outcome()).isEqualTo("IGNORED_STALE");
        new RecoveryReconciler(pool).reconcile(ledger(first, second, third));
        assertThat(sources.apply(writer, second)).isEqualTo(originallyStale);
        assertThat(sources.apply(writer, third)).isEqualTo(originallyApplied);
        assertThat(db.jdbc().queryForObject("select content_version from source_state", Long.class)).isEqualTo(3);
    }

    @Test void conflictingRestoredEventFingerprintFailsWithoutRetiringContexts() {
        db.jdbc().update("update source_events set payload_hash=?", "0".repeat(64));
        assertThatThrownBy(() -> new RecoveryReconciler(pool).reconcile(ledger(first)))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("SOURCE_LEDGER_CONFLICT");
        assertThat(db.jdbc().queryForObject("select count(*) from context_items where retired_at is not null", Integer.class)).isZero();
    }

    @Test void recoveryCannotExtendTheNormalFreshnessLimitThroughAFutureLedgerDeadline() {
        var future = new SourceEvent("policy", 2L, first.content(), first.readers(), "ACTIVE", db.now().plusSeconds(3600));
        assertThatThrownBy(() -> new RecoveryReconciler(pool).reconcile(ledger(first, future)))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("INVALID_SOURCE_LEDGER");
        assertThat(db.jdbc().queryForObject("select count(*) from context_items where retired_at is not null", Integer.class)).isZero();
    }

    @Test void databaseConstraintRequiresPairedRetirementMetadata() {
        assertThatThrownBy(() -> db.jdbc().update("update context_items set retired_at=clock_timestamp(),retired_reason=null,content='' where id=?", root.id()))
                .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
        assertThat(db.jdbc().queryForObject("select retired_at is null from context_items where id=?", Boolean.class, root.id())).isTrue();
    }
}
