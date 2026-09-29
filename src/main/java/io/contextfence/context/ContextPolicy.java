package io.contextfence.context;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Problem;
import io.contextfence.identity.Caller;
import io.contextfence.persistence.Database;
import io.contextfence.sources.SourceRepository;
import io.contextfence.sources.SourceRepository.Source;
import java.time.Instant;
import java.util.*;

public final class ContextPolicy {
    public record Failure(int status, String code, int priority) {}
    public record Validation(Failure failure, List<Reason> reasons, List<SourceVersion> sources) {}
    private final SourceRepository sources;
    public ContextPolicy(Database db) { sources = new SourceRepository(db); }
    public static Failure current(Source source, String subject, Instant checkedAt) {
        if (source == null) return new Failure(404, "NOT_FOUND", 0);
        if (source.state().equals("DELETED")) return new Failure(410, "SOURCE_DELETED", 2);
        if (!source.readers().contains(subject)) return new Failure(403, "SOURCE_ACCESS_DENIED", 1);
        if (!checkedAt.isBefore(source.freshUntil())) return new Failure(503, "SOURCE_UNVERIFIED", 5);
        return null;
    }
    public static void require(Failure failure) {
        if (failure != null) throw new Problem(failure.status(), failure.code());
    }
    public Validation validate(Caller caller, List<ContextRepository.Item> items, Instant now) {
        Map<String, Source> current = new HashMap<>();
        Set<SourceVersion> bound = new HashSet<>();
        List<Reason> reasons = new ArrayList<>();
        Failure overall = null;
        for (var item : items) {
            Failure failure = now.isBefore(item.expiresAt()) ? null : new Failure(410, "CONTEXT_EXPIRED", 3);
            for (var dependency : item.sources()) {
                bound.add(dependency);
                Source source = current.computeIfAbsent(dependency.sourceId(), id -> sources.metadata(caller.tenant(), id));
                failure = prefer(failure, current(source, caller.subject(), now));
                if (source != null && (source.version() != dependency.contentVersion() || source.epoch() != dependency.authEpoch()))
                    failure = prefer(failure, new Failure(409, "CONTEXT_STALE", 4));
            }
            if (failure != null) { reasons.add(new Reason(item.id(), failure.code())); overall = prefer(overall, failure); }
        }
        var versions = bound.stream().sorted(Comparator.comparing(SourceVersion::sourceId)
                .thenComparingLong(SourceVersion::contentVersion).thenComparingLong(SourceVersion::authEpoch)).toList();
        if (current.size() > 32) overall = prefer(overall, new Failure(400, "TOO_MANY_SOURCES", 6));
        return new Validation(overall, List.copyOf(reasons), versions);
    }
    private static Failure prefer(Failure a, Failure b) {
        if (a == null) return b;
        return b != null && b.priority() < a.priority() ? b : a;
    }
}
