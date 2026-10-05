package io.contextfence.recovery;
import java.util.*;
public final class RecoveryPlan {
    public record Existing(long currentSequence, long currentEpoch, long maximumContextEpoch) {
        public Existing(long currentEpoch, long maximumContextEpoch) { this(0, currentEpoch, maximumContextEpoch); }
    }
    public record Source(SourceLedger.Projection projection, long rebuiltEpoch, long restoredEpoch) {}
    public static List<Source> create(SourceLedger ledger, Map<SourceLedger.Key,Existing> existing) {
        Map<SourceLedger.Key,SourceLedger.Projection> latest = new HashMap<>();
        ledger.projections().forEach(p -> latest.put(p.key(), p));
        for (var entry : existing.entrySet()) {
            var projection = latest.get(entry.getKey());
            if (projection == null || entry.getValue().currentSequence() > projection.sequence())
                throw new IllegalArgumentException("INCOMPLETE_SOURCE_LEDGER");
        }
        List<Source> result = new ArrayList<>();
        for (var projection : ledger.projections()) {
            var previous = existing.get(projection.key());
            long upper = previous == null ? projection.epoch()
                    : Math.max(projection.epoch(), Math.max(previous.currentEpoch(), previous.maximumContextEpoch()));
            if (upper == Long.MAX_VALUE) throw new IllegalArgumentException("RECOVERY_EPOCH_EXHAUSTED");
            result.add(new Source(projection, projection.epoch(), upper + 1));
        }
        return List.copyOf(result);
    }
}
