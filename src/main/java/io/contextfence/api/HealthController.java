package io.contextfence.api;

import io.contextfence.recovery.RecoveryGate;

import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import javax.sql.DataSource;
import java.util.Map;

@RestController
public class HealthController {
    private final JdbcTemplate jdbc;
    private final Flyway flyway;
    private final RecoveryGate gate;

    @Autowired
    public HealthController(DataSource source, RecoveryGate gate) {
        jdbc = new JdbcTemplate(source);
        this.flyway = Flyway.configure().dataSource(source).cleanDisabled(true).ignoreMigrationPatterns(new String[0]).load();
        this.gate = gate;
    }

    @GetMapping("/health/live")
    public ResponseEntity<String> live() { return ContextController.json(200, Map.of("status", "UP")); }

    @GetMapping("/health/ready")
    public ResponseEntity<String> ready() {
        return isReady() ? ContextController.json(200, Map.of("status", "UP"))
                : ContextController.json(503, Map.of("code", "NOT_READY"));
    }

    public boolean isReady() {
        if (!gate.isOpen()) return false;
        try {
            if (!Integer.valueOf(1).equals(jdbc.queryForObject("select 1", Integer.class))
                    || flyway.info().current() == null || flyway.info().pending().length != 0
                    || !flyway.validateWithResult().validationSuccessful) {
                return false;
            }
            return true;
        } catch (Exception unavailable) {
            return false;
        }
    }
}
