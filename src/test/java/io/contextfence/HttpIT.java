package io.contextfence;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.support.TestDatabase;
import org.junit.jupiter.api.*;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.servlet.mvc.method.RequestMappingInfo;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import static org.assertj.core.api.Assertions.*;

/** Real embedded HTTP server and PostgreSQL; no mock controller or skipped database. */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class HttpIT {
    static final String ALICE = token(), WRITER = token(), BOB = token(), EVE = token();
    static final String TENANT = "http." + UUID.randomUUID();
    static final String SECRET_BODY = "synthetic-http-protected-marker-" + UUID.randomUUID();
    static final String DERIVED_BODY = "synthetic-http-derived-marker-" + UUID.randomUUID();
    static ConfigurableApplicationContext app;
    static HttpClient client;
    static String base;
    static PrintStream previousOut, previousErr;
    static ByteArrayOutputStream logs;

    @BeforeAll static void start() throws Exception {
        TestDatabase db = TestDatabase.shared();
        Path directory = Path.of(System.getProperty("project.build.directory", "target-http"), "http-private");
        Files.createDirectories(directory);
        Path identities = directory.resolve("identities.json").toAbsolutePath();
        Files.writeString(identities, Json.write(Map.of("principals", List.of(
                principal(ALICE, TENANT, "alice", List.of("READER", "PRODUCER")),
                principal(WRITER, TENANT, "writer", List.of("SOURCE_WRITER")),
                principal(BOB, TENANT, "bob", List.of("READER")),
                principal(EVE, "other." + UUID.randomUUID(), "alice", List.of("READER"))))));
        logs = new ByteArrayOutputStream();
        previousOut = System.out; previousErr = System.err;
        System.setOut(new PrintStream(logs, true, StandardCharsets.UTF_8));
        System.setErr(new PrintStream(logs, true, StandardCharsets.UTF_8));
        try {
            app = SpringApplication.run(ContextFenceApplication.class,
                    "--server.port=0", "--spring.main.banner-mode=off", "--logging.level.root=WARN",
                    "--logging.level.io.contextfence.observability=INFO", "--contextfence.observability.instance=http-it",
                    "--spring.datasource.url=" + db.url, "--spring.datasource.username=" + db.user,
                    "--spring.datasource.password=" + db.password,
                    "--CONTEXTFENCE_IDENTITIES_FILE=" + identities);
            base = "http://127.0.0.1:" + app.getEnvironment().getProperty("local.server.port");
            client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();
            var mapping = app.getBean("requestMappingHandlerMapping", RequestMappingHandlerMapping.class);
            var options = new RequestMappingInfo.BuilderConfiguration();
            options.setPatternParser(mapping.getPatternParser());
            var failures = new FailureEndpoints();
            for (String action : List.of("database", "unexpected")) {
                mapping.registerMapping(RequestMappingInfo.paths("/test-failures/" + action).methods(RequestMethod.GET)
                        .options(options).build(), failures, FailureEndpoints.class.getMethod(action));
            }
        } catch (Throwable failure) {
            System.setOut(previousOut); System.setErr(previousErr);
            throw failure;
        }
    }

    @AfterAll static void stop() {
        try { if (app != null) app.close(); }
        finally {
            if (previousOut != null) System.setOut(previousOut);
            if (previousErr != null) System.setErr(previousErr);
        }
        if (logs != null) {
            String output = logs.toString(StandardCharsets.UTF_8);
            assertThat(output.contains(SECRET_BODY)).isFalse();
            assertThat(output.contains(DERIVED_BODY)).isFalse();
            for (String token : List.of(ALICE, WRITER, BOB, EVE)) assertThat(output.contains(token)).isFalse();
            assertThat(output.contains("Using generated security password")).isFalse();
            var requestLogs = output.lines().filter(line -> line.contains("request.completed")).toList();
            assertThat(requestLogs).isNotEmpty();
            for (String line : requestLogs) {
                Map<String, Object> entry = Json.read(line, new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() {});
                assertThat(entry).containsEntry("instance", "http-it").containsEntry("message", "request.completed")
                        .containsKeys("requestId", "version", "route", "method", "status", "reason", "durationMs", "actor");
                assertThatCode(() -> UUID.fromString((String) entry.get("requestId"))).doesNotThrowAnyException();
                assertThat(entry.get("actor").toString()).matches("ANONYMOUS|[0-9a-f]{64}");
                assertThat(line).doesNotContain(TENANT, "Authorization", "Bearer ", "?token=");
            }
        }
    }

    @Test @Order(1) void authenticationStrictJsonAndEveryErrorRemainSafe() throws Exception {
        assertSafe(send("GET", "/health/live", null, null), 200);
        assertSafe(send("GET", "/health/ready", null, null), 200);
        assertCode(send("POST", "/v1/contexts/assemble", null, "{}"), 401, "UNAUTHENTICATED");
        assertCode(send("POST", "/v1/source-events", ALICE, "{}"), 403, "FORBIDDEN");
        for (String body : List.of(
                "{\"source_id\":\"a\",\"tenant\":\"forged\",\"subject\":\"bob\",\"content\":\"" + SECRET_BODY + "\"}",
                "{\"source_id\":\"a\",\"source_id\":\"b\"}",
                "{\"source_id\":123}", "{\"ttl_seconds\":\"30\"}", "{} {}")) {
            assertCode(send("POST", "/v1/contexts/source", ALICE, body), 400, "INVALID_JSON");
        }
        assertCode(send("GET", "/not-a-route", ALICE, null), 404, "NOT_FOUND");
        assertCode(send("GET", "/v1/source-events", ALICE, null), 405, "METHOD_NOT_ALLOWED");
        assertCode(send("GET", "/v1/receipts/not-a-uuid", ALICE, null), 400, "INVALID_REQUEST");
        var unsupported = client.send(HttpRequest.newBuilder(URI.create(base + "/v1/contexts/source"))
                .header("Authorization", "Bearer " + ALICE).header("Content-Type", "text/plain")
                .POST(HttpRequest.BodyPublishers.ofString(SECRET_BODY)).build(), HttpResponse.BodyHandlers.ofString());
        assertCode(unsupported, 415, "UNSUPPORTED_MEDIA_TYPE");
        assertCode(send("POST", "/v1/contexts/derived", ALICE, "x".repeat(300 * 1024 + 1)), 413, "REQUEST_TOO_LARGE");
        assertCode(send("GET", "/test-failures/database", ALICE, null), 503, "DATABASE_UNAVAILABLE");
        assertCode(send("GET", "/test-failures/unexpected", ALICE, null), 500, "INTERNAL_ERROR");
    }

    @Test @Order(2) void trustedIdentityCreationRevocationAndReceiptsWorkEndToEnd() throws Exception {
        String source = "policy." + UUID.randomUUID();
        SourceEvent event = new SourceEvent(source, 1L, SECRET_BODY, List.of("alice", "bob"), "ACTIVE", Instant.now().plusSeconds(120));
        assertSafe(send("POST", "/v1/source-events", WRITER, Json.write(event)), 200);
        var sourceResponse = send("POST", "/v1/contexts/source", ALICE, Json.write(new SourceContextRequest(source, 120)));
        assertSafe(sourceResponse, 201);
        ContextHandle first = Json.read(sourceResponse.body(), ContextHandle.class);
        var derivedResponse = send("POST", "/v1/contexts/derived", ALICE,
                Json.write(new DerivedContextRequest(DERIVED_BODY, List.of(first.id()), 120)));
        assertSafe(derivedResponse, 201);
        ContextHandle derived = Json.read(derivedResponse.body(), ContextHandle.class);
        var assembled = send("POST", "/v1/contexts/assemble", ALICE, Json.write(new AssembleRequest(List.of(first.id(), derived.id()))));
        assertSafe(assembled, 200);
        AdmissionDecision allowed = Json.read(assembled.body(), AdmissionDecision.class);
        List<String> admitted = allowed.items().stream().map(ContextBody::content).toList();
        assertThat(admitted.contains(SECRET_BODY)).isTrue();
        assertThat(admitted.contains(DERIVED_BODY)).isTrue();
        var repeated = send("POST", "/v1/contexts/assemble", ALICE, Json.write(new AssembleRequest(List.of(first.id(), derived.id()))));
        assertSafe(repeated, 200);
        assertThat(Json.read(repeated.body(), AdmissionDecision.class).items()).isEqualTo(allowed.items());
        var receipt = send("GET", "/v1/receipts/" + allowed.receipt().id(), ALICE, null);
        assertSafe(receipt, 200);
        assertThat(receipt.body().contains(SECRET_BODY)).isFalse();
        assertCode(send("GET", "/v1/receipts/" + allowed.receipt().id(), BOB, null), 404, "NOT_FOUND");
        assertCode(send("POST", "/v1/contexts/assemble", EVE, Json.write(new AssembleRequest(List.of(first.id())))), 404, "NOT_FOUND");
        var spoofed = client.send(HttpRequest.newBuilder(URI.create(base + "/v1/contexts/assemble"))
                .header("Authorization", "Bearer " + BOB).header("X-Tenant", TENANT).header("X-Subject", "alice")
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(new AssembleRequest(List.of(first.id()))))).build(), HttpResponse.BodyHandlers.ofString());
        assertCode(spoofed, 404, "NOT_FOUND");
        assertSafe(send("POST", "/v1/source-events", WRITER,
                Json.write(new SourceEvent(source, 2L, SECRET_BODY, List.of("bob"), "ACTIVE", Instant.now().plusSeconds(120)))), 200);
        var denied = send("POST", "/v1/contexts/assemble", ALICE, Json.write(new AssembleRequest(List.of(first.id(), derived.id()))));
        assertSafe(denied, 403);
        AdmissionDecision decision = Json.read(denied.body(), AdmissionDecision.class);
        assertThat(decision.code()).isEqualTo("SOURCE_ACCESS_DENIED");
        assertThat(decision.items()).isEmpty();
        assertThat(denied.body().contains(SECRET_BODY)).isFalse();
        assertThat(denied.body().contains(DERIVED_BODY)).isFalse();
        assertSafe(send("GET", "/v1/receipts/" + decision.receipt().id(), ALICE, null), 200);
    }

    @Test @Order(3) void exportsAuthenticatedBoundedHttpCachePoolAndJvmMetrics() throws Exception {
        assertCode(send("GET", "/actuator/prometheus", null, null), 401, "UNAUTHENTICATED");
        assertCode(send("GET", "/actuator/env", ALICE, null), 403, "FORBIDDEN");
        assertCode(send("POST", "/actuator/prometheus", ALICE, "{}"), 403, "FORBIDDEN");
        assertCode(send("GET", "/unknown/" + SECRET_BODY + "?token=" + ALICE, ALICE, null), 404, "NOT_FOUND");
        for (int i = 0; i < 4; i++)
            assertCode(send("GET", "/v1/receipts/" + UUID.randomUUID(), ALICE, null), 404, "NOT_FOUND");
        var metrics = send("GET", "/actuator/prometheus", ALICE, null);
        assertThat(metrics.statusCode()).isEqualTo(200);
        assertThat(metrics.headers().firstValue("Content-Type").orElse("")).startsWith("text/plain");
        String scrape = metrics.body();
        assertThat(scrape).contains("http_server_requests_seconds_bucket", "cache_gets_total", "cache_evictions_total",
                "hikaricp_connections_pending", "hikaricp_connections_acquire_seconds", "jvm_memory_used_bytes", "contextfence_ready");
        assertThat(scrape).doesNotContain(SECRET_BODY, DERIVED_BODY, TENANT, ALICE, WRITER, BOB, EVE,
                "requestId=", "user=", "subject=", "tenant=", "sourceId=", "?token=");
        for (String expected : List.of("status=\"401\"", "status=\"403\"", "status=\"413\"", "status=\"503\"")) {
            assertThat(scrape.lines().anyMatch(line -> line.startsWith("http_server_requests_seconds_count{") && line.contains(expected))).isTrue();
        }
        assertThat(scrape.lines().anyMatch(line -> line.startsWith("http_server_requests_seconds_count{")
                && line.contains("uri=\"/v1/receipts/{id}\"") && line.contains("reason=\"NOT_FOUND\""))).isTrue();
        assertThat(scrape.lines().anyMatch(line -> line.startsWith("cache_gets_total{")
                && line.contains("cache=\"context-body\"") && line.contains("result=\"miss\""))).isTrue();
        assertThat(scrape.lines().anyMatch(line -> line.startsWith("cache_gets_total{")
                && line.contains("cache=\"context-body\"") && line.contains("result=\"hit\""))).isTrue();
    }

    private static HttpResponse<String> send(String method, String path, String token, String body) throws Exception {
        var request = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(15));
        if (token != null) request.header("Authorization", "Bearer " + token);
        if (body != null) request.header("Content-Type", "application/json");
        request.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
        return client.send(request.build(), HttpResponse.BodyHandlers.ofString());
    }
    private static void assertSafe(HttpResponse<String> response, int status) {
        assertThat(response.statusCode()).isEqualTo(status);
        assertThat(response.headers().firstValue("Cache-Control")).hasValue("no-store");
        assertThat(response.headers().firstValue("Content-Type").orElse("")).startsWith("application/json");
    }
    private static void assertCode(HttpResponse<String> response, int status, String code) {
        assertSafe(response, status);
        assertThat(response.body().contains(SECRET_BODY)).isFalse();
        assertThat(response.body().contains(DERIVED_BODY)).isFalse();
        for (String token : List.of(ALICE, WRITER, BOB, EVE)) assertThat(response.body().contains(token)).isFalse();
        assertThat(response.body()).isEqualTo("{\"code\":\"" + code + "\"}");
    }
    private static Map<String, Object> principal(String token, String tenant, String subject, List<String> roles) {
        return Map.of("token", token, "tenant", tenant, "subject", subject, "roles", roles);
    }
    private static String token() { return UUID.randomUUID().toString() + UUID.randomUUID(); }

    public static class FailureEndpoints {
        public void database() { throw new DataAccessResourceFailureException(SECRET_BODY, new SQLException(SECRET_BODY)); }
        public void unexpected() { throw new IllegalStateException(SECRET_BODY); }
    }
}
