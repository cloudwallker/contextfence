package io.contextfence.support;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import java.net.URI;
import org.flywaydb.core.Flyway;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** Uses a real PostgreSQL database; an unavailable database is a failure, never a skip. */
public final class TestDatabase {
    private static TestDatabase instance;
    public final HikariDataSource dataSource;
    public final String url;
    public final String user;
    public final String password;
    private PostgreSQLContainer container;

    private TestDatabase() {
        String configured = System.getenv("TEST_DATABASE_URL");
        if (configured == null || configured.isBlank()) {
            container = new PostgreSQLContainer("postgres:17.11-alpine").withDatabaseName("contextfence_test");
            container.start();
            url = container.getJdbcUrl();
            user = container.getUsername();
            password = container.getPassword();
        } else {
            URI parsed = URI.create(configured.substring("jdbc:".length()));
            if (!parsed.getPath().endsWith("_test")) {
                throw new IllegalArgumentException("External integration database must end in _test");
            }
            url = configured;
            user = required("TEST_DATABASE_USER");
            password = required("TEST_DATABASE_PASSWORD");
        }
        HikariConfig config = new HikariConfig();
        config.setJdbcUrl(url);
        config.setUsername(user);
        config.setPassword(password);
        config.setMaximumPoolSize(12);
        config.setConnectionTimeout(3000);
        config.addDataSourceProperty("ApplicationName", "contextfence-tests");
        dataSource = new HikariDataSource(config);
        Flyway.configure().dataSource(dataSource).cleanDisabled(true).load().migrate();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            dataSource.close();
            if (container != null) container.stop();
        }));
    }

    public static synchronized TestDatabase shared() {
        if (instance == null) instance = new TestDatabase();
        return instance;
    }

    private static String required(String key) {
        String value = System.getenv(key);
        if (value == null || value.isBlank()) throw new IllegalStateException("Missing " + key);
        return value;
    }
}
