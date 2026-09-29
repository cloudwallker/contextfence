package io.contextfence;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.context.BodyCache;
import io.contextfence.support.TestDatabase;
import java.io.IOException;
import java.net.*;
import java.net.http.*;
import java.nio.file.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.SpringApplication;
import static org.assertj.core.api.Assertions.*;

class DatabaseOutageIT {
    @TempDir Path temp;
    @Test void disconnectedDatabaseReturns503EvenWhenBodyCacheIsHot() throws Exception {
        var database = TestDatabase.shared();
        URI destination = URI.create(database.url.substring("jdbc:".length()));
        String token = UUID.randomUUID().toString() + UUID.randomUUID();
        String tenant = "outage." + UUID.randomUUID();
        Path identities = temp.resolve("identities.json");
        Files.writeString(identities, Json.write(Map.of("principals", List.of(Map.of("token", token,
                "tenant", tenant, "subject", "alice", "roles", List.of("SOURCE_WRITER", "READER"))))));
        try (var proxy = new DatabaseWire(destination.getHost(), destination.getPort())) {
            String url = "jdbc:postgresql://127.0.0.1:" + proxy.port() + destination.getRawPath();
            try (var app = SpringApplication.run(ContextFenceApplication.class,
                    "--server.port=0", "--spring.main.banner-mode=off", "--logging.level.root=ERROR",
                    "--spring.datasource.url=" + url, "--spring.datasource.username=" + database.user,
                    "--spring.datasource.password=" + database.password,
                    "--spring.datasource.hikari.connection-timeout=1000",
                    "--CONTEXTFENCE_IDENTITIES_FILE=" + identities.toAbsolutePath())) {
                String base = "http://127.0.0.1:" + app.getEnvironment().getProperty("local.server.port");
                try (var http = HttpClient.newHttpClient()) {
                    String body = "OUTAGE-ONLY-SYNTHETIC-" + UUID.randomUUID();
                    assertThat(post(http, base, token, "/v1/source-events", new SourceEvent("source", 1L,
                            body, List.of("alice"), "ACTIVE", Instant.now().plusSeconds(180))).statusCode()).isEqualTo(200);
                    var created = post(http, base, token, "/v1/contexts/source", new SourceContextRequest("source", 180));
                    assertThat(created.statusCode()).isEqualTo(201);
                    var item = Json.read(created.body(), ContextHandle.class);
                    var request = new AssembleRequest(List.of(item.id()));
                    for (int i = 0; i < 2; i++) assertThat(post(http, base, token, "/v1/contexts/assemble", request).statusCode()).isEqualTo(200);
                    long hits = app.getBean(BodyCache.class).stats().hitCount();
                    assertThat(hits).isPositive();
                    // Close established PostgreSQL sockets AND refuse future connections. The real database stays untouched.
                    proxy.disconnect();
                    var denied = post(http, base, token, "/v1/contexts/assemble", request);
                    assertThat(denied.statusCode()).isEqualTo(503);
                    assertThat(denied.body().contains(body)).isFalse();
                    assertThat(denied.body().contains(token)).isFalse();
                    assertThat(denied.body()).contains("DATABASE_UNAVAILABLE");
                    assertThat(denied.headers().firstValue("Cache-Control")).contains("no-store");
                    assertThat(app.getBean(BodyCache.class).stats().hitCount()).isEqualTo(hits);
                    var ready = http.send(HttpRequest.newBuilder(URI.create(base + "/health/ready"))
                            .timeout(Duration.ofSeconds(10)).GET().build(), HttpResponse.BodyHandlers.ofString());
                    assertThat(ready.statusCode()).isEqualTo(503);
                }
            }
        }
    }
    static HttpResponse<String> post(HttpClient http, String base, String token, String path, Object payload) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(15))
                .header("Authorization", "Bearer " + token).header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(payload))).build(), HttpResponse.BodyHandlers.ofString());
    }
    /** Test-only loopback TCP transport. No production failpoint and no shutdown of the shared test database. */
    static final class DatabaseWire implements AutoCloseable {
        private final ServerSocket listener;
        private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
        private final Thread acceptor;
        private volatile boolean disconnected;
        DatabaseWire(String host, int port) throws IOException {
            listener = new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
            acceptor = Thread.ofVirtual().start(() -> {
                while (!listener.isClosed()) {
                    try {
                        Socket incoming = listener.accept();
                        synchronized (sockets) {
                            if (disconnected) { incoming.close(); continue; }
                            Socket upstream = new Socket();
                            upstream.connect(new InetSocketAddress(host, port), 2000);
                            sockets.add(incoming); sockets.add(upstream);
                            Thread.ofVirtual().start(() -> pipe(incoming, upstream));
                            Thread.ofVirtual().start(() -> pipe(upstream, incoming));
                        }
                    } catch (IOException closed) { if (listener.isClosed()) return; }
                }
            });
        }
        int port() { return listener.getLocalPort(); }
        private void pipe(Socket from, Socket to) {
            try { from.getInputStream().transferTo(to.getOutputStream()); }
            catch (IOException expectedDisconnect) { /* Socket closure is the injected failure. */ }
            finally { closeSocket(from); closeSocket(to); sockets.remove(from); sockets.remove(to); }
        }
        void disconnect() {
            synchronized (sockets) { disconnected = true; sockets.forEach(DatabaseWire::closeSocket); }
        }
        private static void closeSocket(Socket socket) { try { socket.close(); } catch (IOException ignored) {} }
        @Override public void close() throws Exception { disconnect(); listener.close(); acceptor.join(3000); }
    }
}
