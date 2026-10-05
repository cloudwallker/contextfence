package io.contextfence.recovery;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.common.Values;
import java.time.Instant;
import java.util.*;

/** Bounded, complete synthetic upstream history kept independently of database backups. */
public final class SourceLedger {
    private static final int MAX_BYTES = 16 * 1024 * 1024;
    private static final int MAX_EVENTS = 10000;
    public record Event(String tenant, String sourceId, Long sequence, String content, List<String> readers,
                        String state, String freshUntil) {}
    private record Document(Integer formatVersion, Boolean complete, String eventsSha256, List<Event> events) {}
    public record Key(String tenant, String sourceId) {}
    public record Projection(Key key, long sequence, long version, long epoch, SourceEvent event) {}
    public record History(Key key, SourceEvent event, String fingerprint, SourceResult result) {}
    private final List<Projection> projections;
    private final List<History> histories;

    private SourceLedger(List<Projection> projections, List<History> histories) {
        this.projections = List.copyOf(projections); this.histories = List.copyOf(histories);
    }

    public static SourceLedger fromJson(String input) {
        try {
            if (input == null || Values.bytes(input) > MAX_BYTES) throw invalid();
            Document document = Json.read(input, Document.class);
            if (!Integer.valueOf(1).equals(document.formatVersion()) || !Boolean.TRUE.equals(document.complete())
                    || document.events() == null || document.events().isEmpty() || document.events().size() > MAX_EVENTS
                    || document.eventsSha256() == null || !document.eventsSha256().matches("[0-9a-f]{64}")) throw invalid();
            // Checksum preserves event array order and original timestamp/reader representation.
            // Python equivalent: json.dumps(events, sort_keys=True, ensure_ascii=False, separators=(',', ':')).
            List<Map<String,Object>> canonical = new ArrayList<>();
            for (Event event : document.events()) {
                if (event == null) throw invalid();
                Map<String,Object> fields = new TreeMap<>();
                fields.put("tenant", event.tenant()); fields.put("source_id", event.sourceId());
                fields.put("sequence", event.sequence()); fields.put("content", event.content());
                fields.put("readers", event.readers()); fields.put("state", event.state()); fields.put("fresh_until", event.freshUntil());
                canonical.add(fields);
            }
            if (!Values.hash(Json.write(canonical)).equals(document.eventsSha256())) throw invalid();
            Map<Key, Projection> state = new TreeMap<>(Comparator.comparing(Key::tenant).thenComparing(Key::sourceId));
            List<History> history = new ArrayList<>();
            for (Event raw : document.events()) {
                Key key = new Key(Values.id(raw.tenant()), Values.id(raw.sourceId()));
                if (raw.sequence() == null || raw.sequence() < 1 || raw.readers() == null || raw.readers().size() > 100
                        || raw.readers().stream().anyMatch(Objects::isNull)
                        || new HashSet<>(raw.readers()).size() != raw.readers().size()
                        || (!"ACTIVE".equals(raw.state()) && !"DELETED".equals(raw.state()))) throw invalid();
                var readers = raw.readers().stream().map(Values::id).sorted().toList();
                String content = Values.text(raw.content());
                if ("DELETED".equals(raw.state()) && (!content.isEmpty() || !readers.isEmpty())) throw invalid();
                Instant deadline = Values.timestamp(Instant.parse(raw.freshUntil()));
                SourceEvent event = new SourceEvent(key.sourceId(), raw.sequence(), content, readers, raw.state(), deadline);
                Projection previous = state.get(key);
                if (previous == null ? raw.sequence() != 1 : raw.sequence() != Math.addExact(previous.sequence(), 1)) throw invalid();
                if (previous != null && "DELETED".equals(previous.event().state())) throw invalid();
                long version = previous == null ? 1 : previous.version() + (previous.event().content().equals(content) ? 0 : 1);
                long epoch = previous == null ? 1 : previous.epoch() +
                        (previous.event().readers().equals(readers) && previous.event().state().equals(event.state()) ? 0 : 1);
                Projection projection = new Projection(key, event.sequence(), version, epoch, event);
                state.put(key, projection);
                history.add(new History(key, event, Values.hash(Json.write(event)),
                        new SourceResult(event.sourceId(), event.sequence(), "APPLIED", version, epoch)));
            }
            return new SourceLedger(new ArrayList<>(state.values()), history);
        } catch (Exception invalid) {
            throw invalid();
        }
    }

    private static IllegalArgumentException invalid() { return new IllegalArgumentException("INVALID_SOURCE_LEDGER"); }
    public List<Projection> projections() { return projections; }
    public List<History> histories() { return histories; }
}
