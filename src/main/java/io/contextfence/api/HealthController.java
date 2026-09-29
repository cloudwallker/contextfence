package io.contextfence.api;

import org.flywaydb.core.Flyway;
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

    public HealthController(DataSource source, Flyway flyway) { jdbc = new JdbcTemplate(source); this.flyway = flyway; }

    @GetMapping("/health/live")
    public ResponseEntity<String> live() { return ContextController.json(200, Map.of("status", "UP")); }

    @GetMapping("/health/ready")
    public ResponseEntity<String> ready() {
        try {
            if (!Integer.valueOf(1).equals(jdbc.queryForObject("select 1", Integer.class))
                    || flyway.info().current() == null || flyway.info().pending().length != 0
                    || !flyway.validateWithResult().validationSuccessful) {
                return ContextController.json(503, Map.of("code", "NOT_READY"));
            }
            return ContextController.json(200, Map.of("status", "UP"));
        } catch (Exception unavailable) {
            return ContextController.json(503, Map.of("code", "NOT_READY"));
        }
    }
}
