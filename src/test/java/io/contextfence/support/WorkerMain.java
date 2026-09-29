package io.contextfence.support;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import io.contextfence.admission.AdmissionService;
import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.context.BodyCache;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import io.contextfence.sources.SourceService;
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import javax.sql.DataSource;
import org.springframework.jdbc.datasource.AbstractDataSource;

/** Child-JVM test driver. Never packaged into the production application. */
public final class WorkerMain {
    public static final String PREFIX = "CONTEXTFENCE_TEST_REPLY ";
    public record Command(String id, String op, String tenant, UUID contextId, SourceEvent event,
                          String phase, int count, String target) {}
    public record Reply(String id, String stage, int status, String code, int allowed, int denied,
                        int bodyItems, long cacheHits, Instant checkedAt) {}

    private static final Map<String, CountDownLatch> barriers = new ConcurrentHashMap<>();
    private static final ThreadLocal<Command> active = new ThreadLocal<>();
    private final BodyCache cache = new BodyCache();
    private final Database database;
    private final AdmissionService admission;
    private final SourceService sources;

    private WorkerMain(DataSource dataSource) {
        database = new Database(new CommitBarrierDataSource(dataSource));
        admission = new AdmissionService(database, cache);
        sources = new SourceService(database);
    }

    public static void main(String[] args) throws Exception {
        HikariConfig config = new HikariConfig();
        config.setJdbcUrl(System.getenv("TEST_DATABASE_URL"));
        config.setUsername(System.getenv("TEST_DATABASE_USER"));
        config.setPassword(System.getenv("TEST_DATABASE_PASSWORD"));
        config.setMaximumPoolSize(25);
        config.setMinimumIdle(1);
        config.setConnectionTimeout(3000);
        config.addDataSourceProperty("ApplicationName", args[0]);
        try (HikariDataSource pool = new HikariDataSource(config);
             var executor = Executors.newVirtualThreadPerTaskExecutor();
             var input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            WorkerMain worker = new WorkerMain(pool);
            emit(new Reply("READY", "READY", 200, "READY", 0, 0, 0, 0, null));
            String line;
            while ((line = input.readLine()) != null) {
                Command command = Json.read(line, Command.class);
                if (command.op().equals("release")) {
                    CountDownLatch barrier = barriers.get(command.target());
                    if (barrier == null) throw new IllegalStateException("Unknown test barrier");
                    barrier.countDown();
                    emit(new Reply(command.id(), "RESULT", 200, "RELEASED", 0, 0, 0, 0, null));
                } else {
                    barriers.put(command.id(), new CountDownLatch(1));
                    executor.submit(() -> worker.execute(command));
                }
            }
        }
    }

    private void execute(Command command) {
        active.set(command);
        try {
            Caller reader = new Caller(command.tenant(), "alice", Set.of(Role.READER));
            switch (command.op()) {
                case "read" -> {
                    AdmissionDecision result = admission.assemble(reader, new AssembleRequest(List.of(command.contextId())));
                    emit(reply(command.id(), result));
                }
                case "write" -> {
                    sources.apply(new Caller(command.tenant(), "writer", Set.of(Role.SOURCE_WRITER)), command.event());
                    emit(new Reply(command.id(), "RESULT", 200, "APPLIED", 0, 0, 0, cache.stats().hitCount(), null));
                }
                case "many" -> concurrentReads(command, reader);
                default -> throw new IllegalArgumentException("Unknown test operation");
            }
        } catch (Exception error) {
            // Exception messages may contain SQL parameters. Return only its class, never a stack trace.
            emit(new Reply(command.id(), "ERROR", 500, error.getClass().getSimpleName(), 0, 0, 0, cache.stats().hitCount(), null));
        } finally {
            active.remove();
            barriers.remove(command.id());
        }
    }

    private void concurrentReads(Command command, Caller caller) throws Exception {
        CountDownLatch ready = new CountDownLatch(command.count());
        CountDownLatch start = new CountDownLatch(1);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            List<Future<AdmissionDecision>> tasks = new ArrayList<>();
            for (int i = 0; i < command.count(); i++) tasks.add(executor.submit(() -> {
                ready.countDown();
                if (!start.await(6, TimeUnit.SECONDS)) throw new TimeoutException("Batch start timed out");
                return admission.assemble(caller, new AssembleRequest(List.of(command.contextId())));
            }));
            if (!ready.await(3, TimeUnit.SECONDS)) throw new TimeoutException("Batch readiness timed out");
            try {
                barrier(command, "READY_BATCH");
            } finally {
                start.countDown();
            }
            int allowed = 0, denied = 0, bodyItems = 0;
            for (var task : tasks) {
                AdmissionDecision result = task.get(12, TimeUnit.SECONDS);
                if (result.status() == 200) allowed++;
                else if (result.status() == 403 && result.code().equals("SOURCE_ACCESS_DENIED")) denied++;
                else throw new IllegalStateException("Unexpected admission outcome");
                bodyItems += result.items().size();
            }
            emit(new Reply(command.id(), "RESULT", 200, "BATCH_COMPLETE", allowed, denied,
                    bodyItems, cache.stats().hitCount(), null));
        }
    }

    private Reply reply(String id, AdmissionDecision result) {
        return new Reply(id, "RESULT", result.status(), result.code(), result.status() == 200 ? 1 : 0,
                result.status() == 200 ? 0 : 1, result.items().size(), cache.stats().hitCount(), result.receipt().checkedAt());
    }

    private static void barrier(Command command, String stage) throws InterruptedException, SQLException {
        emit(new Reply(command.id(), stage, 0, "BARRIER", 0, 0, 0, 0, null));
        if (!barriers.get(command.id()).await(6, TimeUnit.SECONDS)) throw new SQLException("Test barrier timed out");
    }

    private static synchronized void emit(Reply reply) {
        System.out.println(PREFIX + Json.write(reply));
        System.out.flush();
    }

    /** A Connection proxy makes the transaction's actual commit the test boundary. */
    private static final class CommitBarrierDataSource extends AbstractDataSource {
        private final DataSource delegate;
        private CommitBarrierDataSource(DataSource delegate) { this.delegate = delegate; }
        @Override public Connection getConnection() throws SQLException { return wrap(delegate.getConnection()); }
        @Override public Connection getConnection(String user, String password) throws SQLException {
            return wrap(delegate.getConnection(user, password));
        }
        private Connection wrap(Connection connection) {
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[]{Connection.class}, (proxy, method, args) -> {
                Command command = active.get();
                boolean commit = method.getName().equals("commit") && command != null;
                if (commit && "BEFORE_COMMIT".equals(command.phase())) barrier(command, "BEFORE_COMMIT");
                Object result;
                try { result = method.invoke(connection, args); }
                catch (InvocationTargetException exception) { throw exception.getCause(); }
                if (commit && "AFTER_COMMIT".equals(command.phase())) barrier(command, "AFTER_COMMIT");
                return result;
            });
        }
    }
}
