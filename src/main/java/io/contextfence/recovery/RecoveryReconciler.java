package io.contextfence.recovery;

import io.contextfence.common.Json;
import io.contextfence.common.Values;
import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.util.*;
import javax.sql.DataSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/** Offline reconciliation of an isolated restored database; never opens the external gate. */
public final class RecoveryReconciler {
    public record SourceRecovery(String tenant, String sourceId, long sequence, long contentVersion,
                                 long rebuiltEpoch, long previousEpoch, long maximumContextEpoch, long restoredEpoch) {}
    public record Report(int retiredContexts, List<SourceRecovery> sources) {}
    private record EventKey(SourceLedger.Key source, long sequence) {}
    private final JdbcTemplate jdbc;
    private final TransactionTemplate transaction;

    public RecoveryReconciler(DataSource dataSource) {
        jdbc = new JdbcTemplate(dataSource);
        transaction = new TransactionTemplate(new DataSourceTransactionManager(dataSource));
        transaction.setTimeout(120);
    }

    public Report reconcile(SourceLedger ledger) {
        if (ledger == null) throw new IllegalArgumentException("INVALID_SOURCE_LEDGER");
        return transaction.execute(status -> {
            jdbc.execute("set local lock_timeout = '4s'");
            jdbc.execute("set local statement_timeout = '110s'");
            // The orchestrator stops both APIs first. These locks also refuse concurrent DML
            // if a caller accidentally leaves a process running against the isolated database.
            jdbc.execute("lock table tenant_guard,source_state,source_events,context_items,context_sources,admission_receipts in access exclusive mode");
            Map<SourceLedger.Key, RecoveryPlan.Existing> existing = new HashMap<>();
            jdbc.query("""
                    select s.tenant,s.source_id,s.last_sequence,s.auth_epoch,
                           coalesce(max(cs.auth_epoch),0) as maximum_context_epoch
                    from source_state s left join context_sources cs
                      on cs.tenant=s.tenant and cs.source_id=s.source_id
                    group by s.tenant,s.source_id,s.last_sequence,s.auth_epoch
                    """, rs -> {
                var key = new SourceLedger.Key(rs.getString("tenant"), rs.getString("source_id"));
                existing.put(key, new RecoveryPlan.Existing(rs.getLong("last_sequence"), rs.getLong("auth_epoch"), rs.getLong("maximum_context_epoch")));
            });
            var plan = RecoveryPlan.create(ledger, existing);
            Map<EventKey,SourceLedger.History> history = new HashMap<>();
            ledger.histories().forEach(h -> history.put(new EventKey(h.key(), h.event().sequence()), h));
            jdbc.query("select tenant,source_id,sequence,payload_hash from source_events", rs -> {
                var key = new EventKey(new SourceLedger.Key(rs.getString("tenant"), rs.getString("source_id")), rs.getLong("sequence"));
                var authoritative = history.get(key);
                if (authoritative == null) throw new IllegalArgumentException("INCOMPLETE_SOURCE_LEDGER");
                if (!authoritative.fingerprint().equals(rs.getString("payload_hash"))) throw new IllegalArgumentException("SOURCE_LEDGER_CONFLICT");
            });
            var now = jdbc.queryForObject("select clock_timestamp()", OffsetDateTime.class).toInstant();
            for (var source : plan) if (source.projection().event().freshUntil().isAfter(now.plusSeconds(300)))
                throw new IllegalArgumentException("INVALID_SOURCE_LEDGER");

            int retired = jdbc.update("""
                    update context_items set retired_at=coalesce(retired_at,clock_timestamp()),
                      retired_reason='DATABASE_RESTORE',content=''
                    """);
            List<SourceRecovery> report = new ArrayList<>();
            for (var source : plan) {
                var projection = source.projection();
                var key = projection.key();
                var event = projection.event();
                jdbc.update("insert into tenant_guard(tenant) values(?) on conflict do nothing", key.tenant());
                jdbc.update("""
                        insert into source_state(tenant,source_id,last_sequence,content_version,auth_epoch,state,content,content_hash,readers,fresh_until)
                        values(?,?,?,?,?,?,?,?,?::jsonb,?)
                        on conflict(tenant,source_id) do update set last_sequence=excluded.last_sequence,
                          content_version=excluded.content_version,auth_epoch=excluded.auth_epoch,state=excluded.state,
                          content=excluded.content,content_hash=excluded.content_hash,readers=excluded.readers,fresh_until=excluded.fresh_until
                        """, key.tenant(), key.sourceId(), projection.sequence(), projection.version(), source.restoredEpoch(),
                        event.state(), event.content(), Values.hash(event.content()), Json.write(event.readers()), Timestamp.from(event.freshUntil()));
                var previous = existing.get(key);
                report.add(new SourceRecovery(key.tenant(), key.sourceId(), projection.sequence(), projection.version(), source.rebuiltEpoch(),
                        previous == null ? 0 : previous.currentEpoch(), previous == null ? 0 : previous.maximumContextEpoch(), source.restoredEpoch()));
            }
            // Preserve every already-committed historical replay result and applied_at exactly.
            // Missing upstream events get their original history epoch, never the recovery epoch.
            jdbc.batchUpdate("""
                    insert into source_events(tenant,source_id,sequence,payload_hash,result) values(?,?,?,?,?::jsonb)
                    on conflict(tenant,source_id,sequence) do nothing
                    """, ledger.histories(), 500, (statement, entry) -> {
                statement.setString(1, entry.key().tenant()); statement.setString(2, entry.key().sourceId());
                statement.setLong(3, entry.event().sequence()); statement.setString(4, entry.fingerprint());
                statement.setString(5, Json.write(entry.result()));
            });
            return new Report(retired, List.copyOf(report));
        });
    }
}
