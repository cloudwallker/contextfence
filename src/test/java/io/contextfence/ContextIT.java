package io.contextfence;

import io.contextfence.api.Contracts.*;
import io.contextfence.admission.AdmissionService;
import io.contextfence.audit.ReceiptService;
import io.contextfence.common.Json;
import io.contextfence.common.Problem;
import io.contextfence.context.BodyCache;
import io.contextfence.context.ContextService;
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

class ContextIT {
    Database db;
    SourceService sources;
    ContextService contexts;
    BodyCache cache;
    AdmissionService admission;
    ReceiptService receipts;
    Caller writer, alice, bob;
    Instant fresh;
    static final String MARKER = "合成内部报价-SYNTHETIC-CONTEXT-SECRET";

    @BeforeEach void setup() {
        db = new Database(TestDatabase.shared().dataSource);
        sources = new SourceService(db); contexts = new ContextService(db);
        cache = new BodyCache(); admission = new AdmissionService(db, cache); receipts = new ReceiptService(db);
        String tenant = "t_" + UUID.randomUUID();
        writer = new Caller(tenant, "writer", Set.of(Role.SOURCE_WRITER));
        alice = new Caller(tenant, "alice", Set.of(Role.READER, Role.PRODUCER));
        bob = new Caller(tenant, "bob", Set.of(Role.READER, Role.PRODUCER));
        fresh = db.now().plusSeconds(180);
        update("quote", 1, MARKER, List.of("alice", "bob"));
    }

    void update(String source, long sequence, String body, List<String> readers) {
        sources.apply(writer, new SourceEvent(source, sequence, body, readers, "ACTIVE", fresh));
    }
    ContextHandle source(String source) { return contexts.createSource(alice, new SourceContextRequest(source, 300)); }
    ContextHandle derived(String body, UUID... parents) {
        return contexts.createDerived(alice, new DerivedContextRequest(body, List.of(parents), 300));
    }
    AdmissionDecision read(UUID... ids) { return admission.assemble(alice, new AssembleRequest(List.of(ids))); }

    @Test void sourceAndDerivedContentAreServedWithReceiptsButNoTextInAudit() {
        var root = source("quote");
        var summary = derived("合成摘要-SUMMARY-CANARY", root.id());
        var result = read(root.id(), summary.id());
        assertThat(result.status()).isEqualTo(200);
        assertThat(result.items()).extracting(ContextBody::content).containsExactly(MARKER, "合成摘要-SUMMARY-CANARY");
        var receipt = receipts.get(alice, result.receipt().id());
        assertThat(receipt.decision()).isEqualTo("ALLOWED");
        assertThat(receipt.contextIds()).containsExactly(root.id(), summary.id());
        assertThat(Json.write(receipt)).doesNotContain(MARKER, "SUMMARY-CANARY");
        assertThat(db.jdbc().queryForObject("select row_to_json(a)::text from admission_receipts a where tenant=? and id=?", String.class, alice.tenant(), receipt.id()))
                .doesNotContain(MARKER, "SUMMARY-CANARY");
    }

    @Test void revokedSourceCannotBeServedFromWarmCacheAndDenialIsPersisted() {
        var root = source("quote");
        var summary = derived("摘要", root.id());
        read(summary.id()); read(summary.id());
        assertThat(cache.stats().hitCount()).isPositive();
        update("quote", 2, MARKER, List.of("bob"));
        var denied = read(summary.id());
        assertThat(denied.status()).isEqualTo(403);
        assertThat(denied.items()).isEmpty();
        assertThat(receipts.get(alice, denied.receipt().id()).decision()).isEqualTo("SOURCE_ACCESS_DENIED");
    }

    @Test void contentUpdateInvalidatesEveryRegisteredDescendant() {
        var root = source("quote"); var child = derived("child", root.id()); var grandchild = derived("grandchild", child.id());
        update("quote", 2, "new policy", List.of("alice", "bob"));
        for (var item : List.of(root, child, grandchild)) {
            assertThat(read(item.id()).code()).isEqualTo("CONTEXT_STALE");
        }
        assertThat(read(source("quote").id()).items().getFirst().content()).isEqualTo("new policy");
    }

    @Test void grantAfterRevokeDoesNotResurrectOldItems() {
        var original = source("quote");
        update("quote", 2, MARKER, List.of("bob"));
        update("quote", 3, MARKER, List.of("alice", "bob"));
        assertThat(read(original.id()).code()).isEqualTo("CONTEXT_STALE");
        assertThat(read(source("quote").id()).status()).isEqualTo(200);
    }

    @Test void deletionAndExpiryBlockEvenCachedBody() {
        var root = source("quote"); read(root.id());
        sources.apply(writer, new SourceEvent("quote", 2L, "", List.of(), "DELETED", fresh));
        assertThat(read(root.id()).code()).isEqualTo("SOURCE_DELETED");
        update("other", 1, "other body", List.of("alice"));
        var expiring = source("other");
        db.jdbc().update("update context_items set expires_at=clock_timestamp()-interval '1 second' where tenant=? and id=?", alice.tenant(), expiring.id());
        assertThat(read(expiring.id()).code()).isEqualTo("CONTEXT_EXPIRED");
    }

    @Test void unverifiedSourceFailsClosedButRenewalDoesNotInvalidateItsVersion() {
        var root = source("quote"); read(root.id());
        sources.apply(writer, new SourceEvent("quote", 2L, MARKER, List.of("alice", "bob"), "ACTIVE", db.now().minusSeconds(1)));
        assertThat(read(root.id()).code()).isEqualTo("SOURCE_UNVERIFIED");
        update("quote", 3, MARKER, List.of("alice", "bob"));
        assertThat(read(root.id()).status()).isEqualTo(200);
    }

    @Test void privateItemsAndReceiptsDoNotCrossSubjectOrTenant() {
        var root = source("quote"); var allowed = read(root.id());
        Caller otherTenant = new Caller("other_" + UUID.randomUUID(), "alice", Set.of(Role.READER));
        for (Caller caller : List.of(bob, otherTenant)) {
            assertThatThrownBy(() -> admission.assemble(caller, new AssembleRequest(List.of(root.id()))))
                    .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.status()).isEqualTo(404));
            assertThatThrownBy(() -> receipts.get(caller, allowed.receipt().id()))
                    .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.status()).isEqualTo(404));
        }
    }

    @Test void mixedBatchIsAllOrNothingAndCurrentPermissionWinsOverStaleness() {
        var stale = source("quote");
        update("valid", 1, "VALID-CANARY", List.of("alice")); var valid = source("valid");
        update("quote", 2, "changed", List.of("bob"));
        var result = read(valid.id(), stale.id());
        assertThat(result.code()).isEqualTo("SOURCE_ACCESS_DENIED");
        assertThat(result.items()).isEmpty();
        assertThat(Json.write(result)).doesNotContain("VALID-CANARY", MARKER);
    }

    @Test void derivesCompleteUnionAndInheritsEarliestExpiry() {
        update("second", 1, "second", List.of("alice"));
        var shortLife = contexts.createSource(alice, new SourceContextRequest("quote", 10));
        var other = source("second");
        var summary = derived("union", shortLife.id(), other.id());
        assertThat(summary.expiresAt()).isEqualTo(shortLife.expiresAt());
        assertThat(summary.sources()).extracting(SourceVersion::sourceId).containsExactly("quote", "second");
        update("second", 2, "second", List.of());
        assertThat(read(summary.id()).status()).isEqualTo(403);
    }

    @Test void rejectsMissingForeignStaleAndTooDeepParents() {
        var root = source("quote");
        assertThatThrownBy(() -> derived("empty")).isInstanceOf(Problem.class);
        assertThatThrownBy(() -> derived("missing", UUID.randomUUID())).isInstanceOf(Problem.class);
        assertThatThrownBy(() -> contexts.createDerived(bob, new DerivedContextRequest("foreign", List.of(root.id()), 300))).isInstanceOf(Problem.class);
        var current = root;
        for (int i = 0; i < 4; i++) current = derived("depth", current.id());
        UUID tooDeepParent = current.id();
        assertThatThrownBy(() -> derived("too deep", tooDeepParent)).isInstanceOf(Problem.class);
        update("quote", 2, "changed", List.of("alice", "bob"));
        assertThatThrownBy(() -> derived("stale", root.id())).isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("CONTEXT_STALE"));
    }

    @Test void enforcesUtf8IndividualAndAggregateBodyLimits() {
        var root = source("quote");
        assertThatThrownBy(() -> derived("中".repeat(21846), root.id())).isInstanceOfSatisfying(Problem.class, p -> assertThat(p.status()).isEqualTo(413));
        var large1 = derived("a".repeat(65536), root.id());
        var large2 = derived("b".repeat(65536), root.id());
        var large3 = derived("c".repeat(65536), root.id());
        var large4 = derived("d".repeat(65536), root.id());
        assertThat(read(large1.id(), large2.id(), large3.id(), large4.id()).status()).isEqualTo(200);
        var tooLarge = read(large1.id(), large2.id(), large3.id(), large4.id(), root.id());
        assertThat(tooLarge.status()).isEqualTo(413);
        assertThat(tooLarge.items()).isEmpty();
    }

    @Test void dependencyAndRequestBoundsApplyToTheFlattenedUnion() {
        var roots = new java.util.ArrayList<UUID>();
        for (int i = 0; i < 33; i++) {
            String id = "bounded." + i;
            update(id, 1, "synthetic", List.of("alice"));
            roots.add(source(id).id());
        }
        var first = derived("first sixteen", roots.subList(0, 16).toArray(UUID[]::new));
        var second = derived("second sixteen", roots.subList(16, 32).toArray(UUID[]::new));
        var union = derived("all thirty-two", first.id(), second.id());
        assertThat(union.sources()).hasSize(32);
        assertThatThrownBy(() -> derived("too many sources", union.id(), roots.get(32)))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("TOO_MANY_SOURCES"));
        assertThatThrownBy(() -> read(roots.subList(0, 17).toArray(UUID[]::new))).isInstanceOf(Problem.class);
        var batch = read(union.id(), roots.get(32));
        assertThat(batch.code()).isEqualTo("TOO_MANY_SOURCES");
        assertThat(batch.items()).isEmpty();
    }

    @Test void expiredParentCannotBeUsedToExtendItsLifetime() {
        var root = source("quote");
        db.jdbc().update("update context_items set expires_at=clock_timestamp()-interval '1 second' where tenant=? and id=?", alice.tenant(), root.id());
        assertThatThrownBy(() -> derived("expired dependency", root.id()))
                .isInstanceOfSatisfying(Problem.class, p -> assertThat(p.code()).isEqualTo("CONTEXT_EXPIRED"));
    }
}
