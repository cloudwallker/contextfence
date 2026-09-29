package io.contextfence.persistence;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.stereotype.Component;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

/** All source writes and content admission must use this guard protocol. */
@Component
public class Database {
    private final JdbcTemplate jdbc;
    private final TransactionTemplate transactions;

    public Database(DataSource dataSource) {
        jdbc = new JdbcTemplate(dataSource);
        transactions = new TransactionTemplate(new DataSourceTransactionManager(dataSource));
        transactions.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
        transactions.setTimeout(8);
    }

    public JdbcTemplate jdbc() { return jdbc; }

    public Instant now() {
        return jdbc.queryForObject("select clock_timestamp()", OffsetDateTime.class).toInstant();
    }

    public <T> T read(String tenant, Supplier<T> action) { return guarded(tenant, false, action); }
    public <T> T write(String tenant, Supplier<T> action) { return guarded(tenant, true, action); }

    private <T> T guarded(String tenant, boolean exclusive, Supplier<T> action) {
        return transactions.execute(status -> {
            jdbc.execute("set local lock_timeout = '4s'");
            jdbc.execute("set local statement_timeout = '6s'");
            jdbc.execute("set local idle_in_transaction_session_timeout = '8s'");
            jdbc.update("insert into tenant_guard(tenant) values (?) on conflict do nothing", tenant);
            jdbc.queryForObject("select tenant from tenant_guard where tenant=? for " + (exclusive ? "update" : "share"), String.class, tenant);
            // The next command gets a fresh READ COMMITTED snapshot after acquiring the guard.
            return action.get();
        });
    }
}
