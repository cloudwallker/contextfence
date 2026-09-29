package io.contextfence;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.context.ContextService;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import io.contextfence.sources.SourceService;
import io.contextfence.support.TestDatabase;
import io.contextfence.support.WorkerMain;
import io.contextfence.support.WorkerMain.Command;
import io.contextfence.support.WorkerMain.Reply;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.sql.Connection;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.BooleanSupplier;
import org.junit.jupiter.api.*;
import static org.assertj.core.api.Assertions.*;

/** Real PostgreSQL + separate JVMs. Only test wiring can suspend Connection.commit(). */
@Timeout(40)
class ConcurrencyIT {
    private static final String BODY = "SYNTHETIC-CONCURRENCY-CANARY";
    private static final List<Map<String, Object>> observations = new CopyOnWriteArrayList<>();
    private final List<Worker> workers = new ArrayList<>();
    Database db;
    Caller writer, alice;
    UUID context;
    SourceEvent revocation;

    @BeforeEach void fixture() {
        db = new Database(TestDatabase.shared().dataSource);
        String tenant = "race_" + UUID.randomUUID();
        writer = new Caller(tenant, "writer", Set.of(Role.SOURCE_WRITER));
        alice = new Caller(tenant, "alice", Set.of(Role.READER));
        Instant fresh = db.now().plusSeconds(180);
        new SourceService(db).apply(writer, new SourceEvent("quote", 1L, BODY, List.of("alice"), "ACTIVE", fresh));
        context = new ContextService(db).createSource(alice, new SourceContextRequest("quote", 300)).id();
        revocation = new SourceEvent("quote", 2L, BODY, List.of(), "ACTIVE", fresh);
    }

    @AfterEach void cleanup() { workers.forEach(Worker::close); }

    @Test void twoJvmWarmCachesDenyAllFiftyConcurrentReadsAfterRevocationAcknowledgement() throws Exception {
        Worker a = worker(), b = worker();
        long hitsA = warm(a), hitsB = warm(b);
        Reply revoke = b.call(command("write", null, revocation, null, 0));
        assertThat(revoke.status()).isEqualTo(200);
        Instant acknowledgement = Instant.now();
        String ra = a.send(command("many", context, null, null, 25));
        String rb = b.send(command("many", context, null, null, 25));
        a.barrier(ra, "READY_BATCH"); b.barrier(rb, "READY_BATCH");
        a.release(ra); b.release(rb);
        Reply first = a.result(ra), second = b.result(rb);
        assertThat(first.allowed() + second.allowed()).isZero();
        assertThat(first.denied() + second.denied()).isEqualTo(50);
        assertThat(first.bodyItems() + second.bodyItems()).isZero();
        assertThat(first.cacheHits()).isEqualTo(hitsA);
        assertThat(second.cacheHits()).isEqualTo(hitsB);
        observations.add(Map.of("case", "two_jvm_post_acknowledgement", "jvms", 2,
                "samples", 50, "denied", 50, "returned_body_items", 0,
                "warm_cache_hits_a", hitsA, "warm_cache_hits_b", hitsB,
                "acknowledgement_observed_at", acknowledgement.toString()));
    }

    @Test void readFirstCanCommitBeforeBlockedRevocationAndLaterReadsDeny() throws Exception {
        Worker a = worker(), b = worker();
        warm(a);
        String read = a.send(command("read", context, null, "BEFORE_COMMIT", 0));
        a.barrier(read, "BEFORE_COMMIT");
        String write = b.send(command("write", null, revocation, null, 0));
        awaitBlocked(b, a);
        a.release(read);
        assertThat(a.result(read).status()).isEqualTo(200);
        assertThat(b.result(write).status()).isEqualTo(200);
        Reply after = a.call(command("read", context, null, null, 0));
        assertThat(after.status()).isEqualTo(403);
        assertThat(after.bodyItems()).isZero();
        observations.add(Map.of("case", "read_first", "lock_wait_observed", true,
                "overlapping_read_status", 200, "post_acknowledgement_status", 403));
    }

    @Test void writeFirstMakesBlockedReadSeeCommittedRevocation() throws Exception {
        Worker a = worker(), b = worker();
        warm(a);
        String write = b.send(command("write", null, revocation, "BEFORE_COMMIT", 0));
        b.barrier(write, "BEFORE_COMMIT");
        String read = a.send(command("read", context, null, null, 0));
        awaitBlocked(a, b);
        b.release(write);
        assertThat(b.result(write).status()).isEqualTo(200);
        Reply result = a.result(read);
        assertThat(result.status()).isEqualTo(403);
        assertThat(result.bodyItems()).isZero();
        observations.add(Map.of("case", "write_first", "lock_wait_observed", true,
                "blocked_read_status", 403, "returned_body_items", 0));
    }

    @Test void contextExpiringDuringGuardWaitIsCheckedAfterTheWait() throws Exception {
        expiryDuringWait(false);
    }

    @Test void sourceFreshnessExpiringDuringGuardWaitIsCheckedAfterTheWait() throws Exception {
        expiryDuringWait(true);
    }

    private void expiryDuringWait(boolean source) throws Exception {
        Worker a = worker(); warm(a);
        String update = source
                ? "update source_state set fresh_until=clock_timestamp()+interval '1 second' where tenant=? and source_id='quote' returning fresh_until"
                : "update context_items set expires_at=clock_timestamp()+interval '1 second' where tenant=? and id=? returning expires_at";
        Instant deadline = source
                ? db.jdbc().queryForObject(update, OffsetDateTime.class, alice.tenant()).toInstant()
                : db.jdbc().queryForObject(update, OffsetDateTime.class, alice.tenant(), context).toInstant();
        try (Connection lock = TestDatabase.shared().dataSource.getConnection()) {
            lock.setAutoCommit(false);
            try (var statement = lock.prepareStatement("select tenant from tenant_guard where tenant=? for update")) {
                statement.setString(1, alice.tenant()); statement.executeQuery().close();
            }
            String read = a.send(command("read", context, null, null, 0));
            await(() -> blocked(a.name), Duration.ofSeconds(3), "Reader did not wait for the held guard");
            Instant began = db.jdbc().queryForObject("select min(xact_start) from pg_stat_activity where application_name=? and wait_event_type='Lock'", OffsetDateTime.class, a.name).toInstant();
            assertThat(began).isBefore(deadline);
            await(() -> !db.now().isBefore(deadline), Duration.ofSeconds(2), "Database clock did not reach expiry");
            lock.commit();
            Reply result = a.result(read);
            assertThat(result.status()).isEqualTo(source ? 503 : 410);
            assertThat(result.code()).isEqualTo(source ? "SOURCE_UNVERIFIED" : "CONTEXT_EXPIRED");
            assertThat(result.checkedAt()).isAfterOrEqualTo(deadline);
            assertThat(result.bodyItems()).isZero();
            observations.add(Map.of("case", source ? "source_expires_while_waiting" : "context_expires_while_waiting",
                    "transaction_began_at", began.toString(), "deadline", deadline.toString(),
                    "checked_at", result.checkedAt().toString(), "status", result.status()));
        }
    }

    @Test void processKilledBeforeCommitLeavesNeitherNewStateNorEventAndReplaySucceeds() throws Exception {
        Worker b = worker();
        String write = b.send(command("write", null, revocation, "BEFORE_COMMIT", 0));
        b.barrier(write, "BEFORE_COMMIT");
        assertState(1, 0);
        b.close();
        assertState(1, 0);
        SourceResult applied = new SourceService(db).apply(writer, revocation);
        assertState(2, 1);
        assertThat(new SourceService(db).apply(writer, revocation)).isEqualTo(applied);
        observations.add(Map.of("case", "kill_before_commit", "uncommitted_state_visible", false,
                "replay_outcome", applied.outcome(), "event_rows_after_replay", 1));
    }

    @Test void processKilledAfterCommitBeforeReplyReplaysThePersistedOriginalResult() throws Exception {
        Worker b = worker();
        String write = b.send(command("write", null, revocation, "AFTER_COMMIT", 0));
        b.barrier(write, "AFTER_COMMIT");
        assertState(2, 1);
        String stored = db.jdbc().queryForObject("select result::text from source_events where tenant=? and source_id='quote' and sequence=2", String.class, writer.tenant());
        b.close();
        assertState(2, 1);
        SourceResult replay = new SourceService(db).apply(writer, revocation);
        assertThat(replay).isEqualTo(Json.read(stored, SourceResult.class));
        assertState(2, 1);
        observations.add(Map.of("case", "kill_after_commit_before_reply", "commit_observed", true,
                "replayed_original_result", true, "event_rows_after_replay", 1));
    }

    private void assertState(long sequence, int events) {
        assertThat(db.jdbc().queryForObject("select last_sequence from source_state where tenant=? and source_id='quote'", Long.class, writer.tenant())).isEqualTo(sequence);
        assertThat(db.jdbc().queryForObject("select count(*) from source_events where tenant=? and source_id='quote' and sequence=2", Integer.class, writer.tenant())).isEqualTo(events);
    }

    private long warm(Worker worker) throws Exception {
        assertThat(worker.call(command("read", context, null, null, 0)).status()).isEqualTo(200);
        Reply second = worker.call(command("read", context, null, null, 0));
        assertThat(second.status()).isEqualTo(200);
        assertThat(second.cacheHits()).isPositive();
        return second.cacheHits();
    }

    private Worker worker() throws Exception {
        Worker worker = new Worker(); workers.add(worker); return worker;
    }

    private Command command(String op, UUID id, SourceEvent event, String phase, int count) {
        return new Command(UUID.randomUUID().toString(), op, alice.tenant(), id, event, phase, count, null);
    }

    private boolean blocked(String application) {
        return Boolean.TRUE.equals(db.jdbc().queryForObject("select exists(select 1 from pg_stat_activity where application_name=? and wait_event_type='Lock' and cardinality(pg_blocking_pids(pid))>0)", Boolean.class, application));
    }

    private void awaitBlocked(Worker blocked, Worker holder) throws Exception {
        await(() -> Boolean.TRUE.equals(db.jdbc().queryForObject("""
                select exists(select 1 from pg_stat_activity waiting join pg_stat_activity holder
                on holder.pid=any(pg_blocking_pids(waiting.pid))
                where waiting.application_name=? and holder.application_name=? and waiting.wait_event_type='Lock')
                """, Boolean.class, blocked.name, holder.name)), Duration.ofSeconds(3), "Expected database blocking relation was absent");
    }

    private static void await(BooleanSupplier condition, Duration timeout, String failure) throws Exception {
        long end = System.nanoTime() + timeout.toNanos();
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() >= end) throw new AssertionError(failure);
            Thread.sleep(10); // Poll an observed condition; never infer lock order from elapsed sleep.
        }
    }

    @AfterAll static void report() throws Exception {
        Path destination = Path.of("target-concurrency", "concurrency-results.json");
        Files.createDirectories(destination.getParent());
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("recorded_at", Instant.now());
        result.put("java", Runtime.version().feature());
        try (Connection connection = TestDatabase.shared().dataSource.getConnection()) {
            result.put("database", connection.getMetaData().getDatabaseProductVersion());
        }
        result.put("scope", "Separate JVM service instances and real PostgreSQL; HTTP transport is verified separately. Only passing cases are recorded; consult JUnit for suite success.");
        result.put("observations", observations);
        Files.writeString(destination, Json.write(result), StandardCharsets.UTF_8);
    }

    /** No database credential is put in argv or protocol output. */
    private static final class Worker implements AutoCloseable {
        final String name = "cf-worker-" + UUID.randomUUID();
        final Process process;
        final BufferedWriter input;
        final Map<String, BlockingQueue<Reply>> inbox = new ConcurrentHashMap<>();
        final List<String> captured = new CopyOnWriteArrayList<>();
        Worker() throws Exception {
            String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
            String classpath = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
            ProcessBuilder builder = new ProcessBuilder(java, "-cp", classpath, WorkerMain.class.getName(), name).redirectErrorStream(true);
            TestDatabase database = TestDatabase.shared();
            builder.environment().put("TEST_DATABASE_URL", database.url);
            builder.environment().put("TEST_DATABASE_USER", database.user);
            builder.environment().put("TEST_DATABASE_PASSWORD", database.password);
            process = builder.start();
            input = new BufferedWriter(new OutputStreamWriter(process.getOutputStream(), StandardCharsets.UTF_8));
            Thread.ofVirtual().start(() -> {
                try (var output = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = output.readLine()) != null) {
                        captured.add(line);
                        if (line.startsWith(WorkerMain.PREFIX)) {
                            Reply reply = Json.read(line.substring(WorkerMain.PREFIX.length()), Reply.class);
                            inbox.computeIfAbsent(reply.id(), ignored -> new LinkedBlockingQueue<>()).add(reply);
                        }
                    }
                } catch (IOException ignored) { /* Expected after destroying this test's process. */ }
            });
            try {
                Reply ready = receive("READY");
                assertThat(ready.stage()).isEqualTo("READY");
            } catch (Exception | AssertionError failedStart) {
                close();
                throw failedStart;
            }
        }
        synchronized String send(Command command) throws IOException {
            inbox.computeIfAbsent(command.id(), ignored -> new LinkedBlockingQueue<>());
            input.write(Json.write(command)); input.newLine(); input.flush(); return command.id();
        }
        Reply call(Command command) throws Exception { return result(send(command)); }
        Reply receive(String id) throws Exception {
            Reply reply = inbox.computeIfAbsent(id, ignored -> new LinkedBlockingQueue<>()).poll(12, TimeUnit.SECONDS);
            if (reply == null) throw new AssertionError("Worker reply timed out; alive=" + process.isAlive());
            assertThat(reply.stage()).isNotEqualTo("ERROR");
            return reply;
        }
        Reply result(String id) throws Exception {
            Reply reply = receive(id); assertThat(reply.stage()).isEqualTo("RESULT"); return reply;
        }
        void barrier(String id, String phase) throws Exception { assertThat(receive(id).stage()).isEqualTo(phase); }
        void release(String target) throws Exception {
            call(new Command(UUID.randomUUID().toString(), "release", null, null, null, null, 0, target));
        }
        @Override public void close() {
            process.destroyForcibly();
            try {
                if (!process.waitFor(5, TimeUnit.SECONDS)) throw new AssertionError("Test worker failed to terminate");
                assertThat(captured.stream().anyMatch(line -> line.contains(BODY)))
                        .as("Protected body must not appear in worker output").isFalse();
            } catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        }
    }
}
