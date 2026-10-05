package io.contextfence.recovery;

import com.zaxxer.hikari.*;
import io.contextfence.api.HealthController;
import io.contextfence.common.Json;
import io.contextfence.persistence.Database;
import io.contextfence.support.TestDatabase;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.TimeUnit;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import static org.assertj.core.api.Assertions.*;

/** Real PostgreSQL role boundary plus the packaged, HTTP-free maintenance entry point. */
class RecoveryRuntimeIT {
    Database administration;
    String schema, role;
    HikariDataSource runtime;

    @BeforeEach void setup() {
        var test = TestDatabase.shared();
        administration = new Database(test.dataSource);
        String suffix = UUID.randomUUID().toString().replace("-", "");
        schema = "runtime_recovery_" + suffix; role = "cf_runtime_" + suffix;
        administration.jdbc().execute("create schema " + schema);
    }
    @AfterEach void cleanup() {
        if (runtime != null) runtime.close();
        if (administration == null || schema == null) return;
        administration.jdbc().execute("drop schema " + schema + " cascade");
        if (role != null && administration.jdbc().queryForObject("select count(*) from pg_roles where rolname=?", Integer.class, role) > 0)
            administration.jdbc().execute("drop role " + role);
    }

    @Test void packagedMigrationCommandRunsWithoutSpringIdentityOrHttpServer() throws Exception {
        var test = TestDatabase.shared();
        Path buildDirectory = Path.of(RecoveryMain.class.getProtectionDomain().getCodeSource().getLocation().toURI()).getParent();
        Path jar = buildDirectory.resolve("context-fence.jar");
        var command = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-jar", jar.toString(), "--migrate-only");
        command.redirectErrorStream(true);
        var env = command.environment();
        env.put("CONTEXTFENCE_DATABASE_URL", scopedUrl(test.url)); env.put("CONTEXTFENCE_DATABASE_USER", test.user);
        env.put("CONTEXTFENCE_DATABASE_PASSWORD", test.password); env.remove("CONTEXTFENCE_IDENTITIES_FILE");
        Process process = command.start();
        boolean finished = process.waitFor(30, TimeUnit.SECONDS);
        if (!finished) process.destroyForcibly();
        assertThat(finished).isTrue();
        String output = new String(process.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8).strip();
        Map<String,String> outcome = Json.read(output, new com.fasterxml.jackson.core.type.TypeReference<Map<String,String>>() {});
        assertThat(process.exitValue()).withFailMessage("Offline migration returned fixed code %s", outcome.get("code")).isZero();
        assertThat(Json.read(output, new com.fasterxml.jackson.core.type.TypeReference<Map<String,String>>() {}))
                .containsEntry("code", "MIGRATION_COMPLETE");
        assertThat(output).doesNotContain(test.password, test.url, "Spring", "Tomcat");
        assertThat(administration.jdbc().queryForObject("select count(*) from " + schema + ".flyway_schema_history where success and version='2'", Integer.class)).isEqualTo(1);
    }

    @Test void readinessRemainsAvailableToAnApplicationRoleWithNoDdlPrivileges() {
        var test = TestDatabase.shared();
        Flyway.configure().dataSource(scopedUrl(test.url), test.user, test.password).defaultSchema(schema).cleanDisabled(true).load().migrate();
        administration.jdbc().execute("create role " + role + " nologin");
        administration.jdbc().execute("grant usage on schema " + schema + " to " + role);
        administration.jdbc().execute("grant select,insert,update,delete on all tables in schema " + schema + " to " + role);
        var config = new HikariConfig();
        config.setJdbcUrl(scopedUrl(test.url)); config.setUsername(test.user); config.setPassword(test.password);
        config.setConnectionInitSql("set role " + role); config.setMaximumPoolSize(2);
        runtime = new HikariDataSource(config);
        var limited = new Database(runtime);
        assertThat(limited.jdbc().queryForObject("select current_user", String.class)).isEqualTo(role);
        assertThatThrownBy(() -> limited.jdbc().execute("create table forbidden_ddl(id integer)")).isInstanceOf(org.springframework.dao.DataAccessException.class);
        var health = new HealthController(runtime, new RecoveryGate((String) null));
        assertThat(health.isReady()).isTrue();
        assertThat(health.ready().getStatusCode().value()).isEqualTo(200);
    }

    @Test void unknownFutureSchemaCannotPassRuntimeReadiness() {
        migrateAndInsertUnknownVersion();
        var test=TestDatabase.shared();
        var config=new HikariConfig(); config.setJdbcUrl(scopedUrl(test.url)); config.setUsername(test.user); config.setPassword(test.password);
        runtime=new HikariDataSource(config);
        assertThat(new HealthController(runtime,new RecoveryGate((String)null)).isReady()).isFalse();
    }

    @Test void unknownFutureSchemaCannotPassOfflineMigration() throws Exception {
        migrateAndInsertUnknownVersion();
        var result=offline("--migrate-only");
        assertThat(result.exit()).isNotZero();
        assertThat(result.code()).isEqualTo("RECOVERY_SCHEMA_NOT_READY");
    }

    @Test void packagedMigrationUpgradesTheOriginalV1SchemaBeforeRuntimeStarts() throws Exception {
        var test=TestDatabase.shared();
        Flyway.configure().dataSource(scopedUrl(test.url),test.user,test.password).defaultSchema(schema).target("1").load().migrate();
        var result=offline("--migrate-only");
        assertThat(result.exit()).withFailMessage("V1 upgrade returned fixed code %s",result.code()).isZero();
        assertThat(result.code()).isEqualTo("MIGRATION_COMPLETE");
        assertThat(administration.jdbc().queryForObject("select count(*) from "+schema+".flyway_schema_history where success and version='2'",Integer.class)).isEqualTo(1);
    }

    @Test void packagedReconciliationUsesTheClosedGateAndIndependentLedgerWithoutHttp() throws Exception {
        var test=TestDatabase.shared();
        Flyway.configure().dataSource(scopedUrl(test.url),test.user,test.password).defaultSchema(schema).load().migrate();
        Path gate=Files.createTempFile("closed-gate-",".json"); Path ledger=Files.createTempFile("independent-ledger-",".json");
        try {
            Files.writeString(gate,"{\"format_version\":1,\"state\":\"CLOSED\"}");
            String rows=Json.write(List.of(Map.of("tenant","acme","source_id","policy","sequence",1,"content","SYNTHETIC",
                    "readers",List.of("alice"),"state","ACTIVE","fresh_until",java.time.Instant.now().plusSeconds(60).toString())));
            Files.writeString(ledger,SourceLedgerTest.document(rows));
            var result=offline(Map.of("CONTEXTFENCE_GATE_FILE",gate.toString()),"--reconcile-source-ledger",ledger.toString());
            assertThat(result.exit()).withFailMessage("Reconciliation returned fixed code %s",result.code()).isZero();
            assertThat(result.code()).isEqualTo("RECOVERY_COMPLETE");
            assertThat(administration.jdbc().queryForObject("select auth_epoch from "+schema+".source_state where tenant='acme' and source_id='policy'",Long.class)).isEqualTo(2);
            assertThat(Files.readString(gate)).isEqualTo("{\"format_version\":1,\"state\":\"CLOSED\"}");
        } finally { Files.deleteIfExists(gate); Files.deleteIfExists(ledger); }
    }

    private void migrateAndInsertUnknownVersion() {
        var test=TestDatabase.shared();
        Flyway.configure().dataSource(scopedUrl(test.url),test.user,test.password).defaultSchema(schema).load().migrate();
        administration.jdbc().execute("insert into "+schema+".flyway_schema_history(installed_rank,version,description,type,script,checksum,installed_by,execution_time,success) values(3,'999','unknown future','SQL','V999__unknown.sql',123,current_user,0,true)");
    }

    private record OfflineResult(int exit,String code) {}
    private OfflineResult offline(String... arguments) throws Exception {
        return offline(Map.of(),arguments);
    }
    private OfflineResult offline(Map<String,String> additionalEnvironment,String... arguments) throws Exception {
        var test=TestDatabase.shared();
        Path build=Path.of(RecoveryMain.class.getProtectionDomain().getCodeSource().getLocation().toURI()).getParent();
        var args=new ArrayList<String>(); args.add(Path.of(System.getProperty("java.home"),"bin","java").toString());
        args.add("-jar"); args.add(build.resolve("context-fence.jar").toString()); args.addAll(List.of(arguments));
        var command=new ProcessBuilder(args); command.redirectErrorStream(true);
        command.environment().put("CONTEXTFENCE_DATABASE_URL",scopedUrl(test.url)); command.environment().put("CONTEXTFENCE_DATABASE_USER",test.user);
        command.environment().put("CONTEXTFENCE_DATABASE_PASSWORD",test.password); command.environment().remove("CONTEXTFENCE_IDENTITIES_FILE");
        command.environment().putAll(additionalEnvironment);
        var process=command.start(); boolean finished=process.waitFor(30,TimeUnit.SECONDS);
        if (!finished) process.destroyForcibly(); assertThat(finished).isTrue();
        String output=new String(process.getInputStream().readAllBytes(),java.nio.charset.StandardCharsets.UTF_8).strip();
        Map<String,Object> status=Json.read(output,new com.fasterxml.jackson.core.type.TypeReference<Map<String,Object>>(){});
        assertThat(output).doesNotContain(test.password,test.url,"Spring","Tomcat");
        return new OfflineResult(process.exitValue(),String.valueOf(status.get("code")));
    }

    private String scopedUrl(String url) { return url + (url.contains("?") ? "&" : "?") + "currentSchema=" + schema; }
}
