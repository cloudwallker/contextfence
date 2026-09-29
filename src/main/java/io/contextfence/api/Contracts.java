package io.contextfence.api;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

/** Wire records. Parsing and business validation are deliberately separate. */
public final class Contracts {
    private Contracts() {}
    public record SourceEvent(String sourceId, Long sequence, String content, List<String> readers,
                              String state, Instant freshUntil) {}
    public record SourceResult(String sourceId, long sequence, String outcome,
                               long contentVersion, long authEpoch) {}
    public record SourceContextRequest(String sourceId, Integer ttlSeconds) {}
    public record DerivedContextRequest(String content, List<UUID> parentIds, Integer ttlSeconds) {}
    public record AssembleRequest(List<UUID> contextIds) {}
    public record SourceVersion(String sourceId, long contentVersion, long authEpoch) {}
    public record ContextHandle(UUID id, String kind, Instant expiresAt, int depth,
                                List<SourceVersion> sources) {}
    public record ContextBody(UUID id, String content) {}
    public record Reason(UUID contextId, String code) {}
    public record Receipt(UUID id, Instant checkedAt, String decision, List<UUID> contextIds,
                          List<SourceVersion> sources, List<Reason> reasons) {}
    public record AdmissionDecision(int status, String code, Receipt receipt, List<ContextBody> items) {}
}
