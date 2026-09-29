package io.contextfence;

import io.contextfence.api.Contracts.SourceEvent;
import io.contextfence.common.Problem;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import io.contextfence.sources.SourceService;
import io.contextfence.support.TestDatabase;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.*;

class SourceIT {
    Database db;
    SourceService service;
    Caller writer;
    Instant deadline;

    @BeforeEach void setup() {
        db = new Database(TestDatabase.shared().dataSource);
        service = new SourceService(db);
        writer = new Caller("t_" + UUID.randomUUID(), "writer", Set.of(Role.SOURCE_WRITER));
        deadline = db.now().plusSeconds(180);
    }

    SourceEvent event(long sequence, String content, List<String> readers) {
        return new SourceEvent("quote", sequence, content, readers, "ACTIVE", deadline);
    }

    @Test void changesContentAndAuthorizationIndependentlyAndDoesNotResurrectOldEpoch() {
        var first = service.apply(writer, event(1, "报价v1", List.of("alice")));
        assertThat(first.contentVersion()).isEqualTo(1);
        assertThat(first.authEpoch()).isEqualTo(1);
        var content = service.apply(writer, event(2, "报价v2", List.of("alice")));
        assertThat(content.contentVersion()).isEqualTo(2);
        assertThat(content.authEpoch()).isEqualTo(1);
        var revoke = service.apply(writer, event(3, "报价v2", List.of()));
        assertThat(revoke.authEpoch()).isEqualTo(2);
        var regrant = service.apply(writer, event(4, "报价v2", List.of("alice")));
        assertThat(regrant.authEpoch()).isEqualTo(3);
    }

    @Test void duplicateReaderOrderIsCanonicalAndConflictingPayloadIsRejected() {
        var original = service.apply(writer, event(1, "body", List.of("bob", "alice")));
        var duplicate = service.apply(writer, event(1, "body", List.of("alice", "bob")));
        assertThat(duplicate).isEqualTo(original);
        assertThatThrownBy(() -> service.apply(writer, event(1, "changed", List.of("alice", "bob"))))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("EVENT_CONFLICT"));
    }

    @Test void staleEventsCannotRestorePermissionOrExtendFreshness() {
        service.apply(writer, event(3, "current", List.of()));
        var stale = service.apply(writer, event(2, "old", List.of("alice")));
        assertThat(stale.outcome()).isEqualTo("IGNORED_STALE");
        assertThat(db.jdbc().queryForObject("select readers::text from source_state where tenant=?", String.class, writer.tenant()))
                .isEqualTo("[]");
        assertThat(service.apply(writer, event(2, "old", List.of("alice")))).isEqualTo(stale);
        assertThat(db.jdbc().queryForObject("select last_sequence from source_state where tenant=?", Long.class, writer.tenant())).isEqualTo(3);
    }

    @Test void identicalReplayAfterDeadlineIsStillAReplayAndNeverRenewsIt() {
        var input = event(1, "body", List.of("alice"));
        var original = service.apply(writer, input);
        db.jdbc().update("update source_state set fresh_until=clock_timestamp()-interval '1 second' where tenant=?", writer.tenant());
        assertThat(service.apply(writer, input)).isEqualTo(original);
        assertThat(db.jdbc().queryForObject("select fresh_until < clock_timestamp() from source_state where tenant=?", Boolean.class, writer.tenant())).isTrue();
    }

    @Test void renewalKeepsVersionsButDeletionIsTerminal() {
        service.apply(writer, event(1, "body", List.of("alice")));
        deadline = deadline.plusSeconds(10);
        var renew = service.apply(writer, event(2, "body", List.of("alice")));
        assertThat(renew.contentVersion()).isEqualTo(1);
        assertThat(renew.authEpoch()).isEqualTo(1);
        var deletion = new SourceEvent("quote", 3L, "", List.of(), "DELETED", deadline);
        service.apply(writer, deletion);
        assertThatThrownBy(() -> service.apply(writer, event(4, "restored", List.of("alice"))))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("SOURCE_DELETED"));
    }

    @Test void roleAndTenantAreEnforcedIndependentlyOfSourceId() {
        service.apply(writer, event(1, "tenant one", List.of("alice")));
        Caller other = new Caller("t_" + UUID.randomUUID(), "writer", Set.of(Role.SOURCE_WRITER));
        service.apply(other, event(1, "tenant two", List.of("alice")));
        assertThat(db.jdbc().queryForObject("select content from source_state where tenant=? and source_id='quote'", String.class, other.tenant())).isEqualTo("tenant two");
        assertThatThrownBy(() -> service.apply(new Caller(writer.tenant(), "alice", Set.of(Role.READER)), event(2, "bad", List.of())))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.status()).isEqualTo(403));
    }

    @Test void rejectsInvalidLimitsAndDuplicateReadersWithoutWriting() {
        assertThatThrownBy(() -> service.apply(writer, event(0, "body", List.of("alice")))).isInstanceOf(Problem.class);
        assertThatThrownBy(() -> service.apply(writer, event(1, "中".repeat(21846), List.of("alice")))).isInstanceOf(Problem.class);
        assertThatThrownBy(() -> service.apply(writer, event(1, "body", List.of("alice", "alice")))).isInstanceOf(Problem.class);
        assertThatThrownBy(() -> service.apply(writer, new SourceEvent("quote", 1L, "body", List.of("alice"), "ACTIVE", db.now().plusSeconds(360))))
                .isInstanceOf(Problem.class);
        assertThat(db.jdbc().queryForObject("select count(*) from source_state where tenant=?", Integer.class, writer.tenant())).isZero();
    }
}
