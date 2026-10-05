package io.contextfence.recovery;

import io.contextfence.api.Contracts.SourceVersion;
import io.contextfence.context.*;
import io.contextfence.identity.Caller;
import io.contextfence.persistence.Database;
import java.sql.*;
import java.time.Instant;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.datasource.AbstractDataSource;
import static org.assertj.core.api.Assertions.*;

class RetiredContextPolicyTest {
    @Test void retiredContextWinsOverExpiryAndDoesNotReadAnyRestoredSourceOrBody() {
        var db = new Database(new AbstractDataSource() {
            @Override public Connection getConnection() throws SQLException { throw new AssertionError("retired context queried the database"); }
            @Override public Connection getConnection(String user, String password) throws SQLException { return getConnection(); }
        });
        var item = new ContextRepository.Item(UUID.randomUUID(), "DERIVED", Instant.EPOCH, 2, "hash", 0,
                List.of(new SourceVersion("policy", 1, 1)), true);
        var caller = new Caller("acme", "alice", Set.of(Caller.Role.READER));
        var validation = new ContextPolicy(db).validate(caller, List.of(item), Instant.now());
        assertThat(validation.failure().code()).isEqualTo("CONTEXT_RETIRED");
        assertThat(validation.failure().status()).isEqualTo(410);
        assertThat(validation.reasons().getFirst().code()).isEqualTo("CONTEXT_RETIRED");
        assertThat(validation.sources()).containsExactly(new SourceVersion("policy", 1, 1));
    }
}
