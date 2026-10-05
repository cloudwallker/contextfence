package io.contextfence.recovery;
import java.io.PrintStream;
import java.util.Map;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import io.contextfence.common.Json;
import com.zaxxer.hikari.*;
import org.flywaydb.core.Flyway;

/** Offline maintenance commands: fixed-code output and no HTTP/Spring initialization. */
public final class RecoveryMain {
    public static int run(String[] args, Map<String,String> environment, PrintStream output) {
        boolean migration = args.length == 1 && "--migrate-only".equals(args[0]);
        boolean reconcile = args.length == 2 && "--reconcile-source-ledger".equals(args[0]);
        if (!migration && !reconcile) { write(output, "RECOVERY_USAGE"); return 2; }
        try {
            SourceLedger ledger = null;
            if (reconcile) {
                String file = environment.get("CONTEXTFENCE_GATE_FILE");
                if (file == null || file.isBlank()) throw new IllegalArgumentException("RECOVERY_GATE_REQUIRED");
                if (!new RecoveryGate(file).isClosed()) throw new IllegalArgumentException("RECOVERY_GATE_MUST_BE_CLOSED");
                ledger = readLedger(Path.of(args[1]));
            }
            String url = required(environment, "CONTEXTFENCE_DATABASE_URL");
            String user = required(environment, "CONTEXTFENCE_DATABASE_USER");
            String password = required(environment, "CONTEXTFENCE_DATABASE_PASSWORD");
            if (!url.startsWith("jdbc:postgresql:")) throw new IllegalArgumentException("INVALID_RECOVERY_CONFIGURATION");
            HikariConfig config = new HikariConfig();
            config.setJdbcUrl(url); config.setUsername(user); config.setPassword(password);
            // PostgreSQL Flyway keeps its metadata connection while borrowing the migration connection.
            config.setMaximumPoolSize(2); config.setMinimumIdle(0); config.setConnectionTimeout(3000);
            config.setPoolName("contextfence-maintenance");
            try (HikariDataSource pool = new HikariDataSource(config)) {
                Flyway flyway = Flyway.configure().dataSource(pool).cleanDisabled(true).ignoreMigrationPatterns(new String[0]).load();
                for (var applied : flyway.info().applied()) {
                    if (applied.getVersion() != null && !java.util.Set.of("1", "2").contains(applied.getVersion().toString()))
                        throw new IllegalArgumentException("RECOVERY_SCHEMA_NOT_READY");
                }
                if (migration) {
                    flyway.migrate();
                    write(output, "MIGRATION_COMPLETE");
                } else {
                    if (flyway.info().current() == null || flyway.info().pending().length != 0 || !flyway.validateWithResult().validationSuccessful)
                        throw new IllegalArgumentException("RECOVERY_SCHEMA_NOT_READY");
                    var report = new RecoveryReconciler(pool).reconcile(ledger);
                    output.println(Json.write(Map.of("code", "RECOVERY_COMPLETE", "report", report)));
                }
            }
            return 0;
        } catch (IllegalArgumentException invalid) {
            // Only these explicit, internal codes are allowed; JDBC/paths never enter output.
            String code = invalid.getMessage();
            if (!java.util.Set.of("RECOVERY_GATE_REQUIRED", "RECOVERY_GATE_MUST_BE_CLOSED", "INVALID_SOURCE_LEDGER",
                    "INVALID_RECOVERY_CONFIGURATION", "RECOVERY_SCHEMA_NOT_READY", "INCOMPLETE_SOURCE_LEDGER",
                    "RECOVERY_EPOCH_EXHAUSTED", "SOURCE_LEDGER_CONFLICT").contains(code == null ? "" : code)) code = "RECOVERY_FAILED";
            write(output, code); return 1;
        } catch (Exception unavailable) { write(output, "RECOVERY_FAILED"); return 1; }
    }

    private static SourceLedger readLedger(Path path) {
        try (var stream = Files.newInputStream(path)) {
            byte[] bytes = stream.readNBytes(16 * 1024 * 1024 + 1);
            if (bytes.length > 16 * 1024 * 1024) throw new IllegalArgumentException();
            return SourceLedger.fromJson(StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString());
        } catch (Exception invalid) { throw new IllegalArgumentException("INVALID_SOURCE_LEDGER"); }
    }

    private static String required(Map<String,String> environment, String key) {
        String value = environment.get(key);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("INVALID_RECOVERY_CONFIGURATION");
        return value;
    }
    private static void write(PrintStream output, String code) { output.println(Json.write(Map.of("code", code))); }

    public static void main(String[] args) {
        // Spring logging configuration is not active for offline commands. Disable library logs
        // so driver/Flyway errors cannot print SQL parameters, connection strings or credentials.
        var logger = org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        if (logger instanceof ch.qos.logback.classic.Logger root) root.setLevel(ch.qos.logback.classic.Level.OFF);
        System.exit(run(args, System.getenv(), System.out));
    }
}
