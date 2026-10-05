package io.contextfence.recovery;

import io.contextfence.api.HealthController;
import java.nio.file.*;
import java.sql.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.jdbc.datasource.AbstractDataSource;
import static org.assertj.core.api.Assertions.*;

class HealthReadinessTest {
    @TempDir Path directory;
    @Test void closedGateMakesTheReadinessGaugeFalseWithoutContactingDatabase() {
        var dataSource = new AbstractDataSource() {
            @Override public Connection getConnection() { throw new AssertionError("closed readiness contacted database"); }
            @Override public Connection getConnection(String user, String password) { return getConnection(); }
        };
        var health = new HealthController(dataSource, new RecoveryGate(directory.resolve("missing").toString()));
        assertThat(health.isReady()).isFalse();
        assertThat(health.ready().getStatusCode().value()).isEqualTo(503);
        assertThat(health.ready().getBody()).isEqualTo("{\"code\":\"NOT_READY\"}");
    }
}
