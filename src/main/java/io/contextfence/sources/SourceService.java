package io.contextfence.sources;

import io.contextfence.api.Contracts.SourceEvent;
import io.contextfence.api.Contracts.SourceResult;
import io.contextfence.common.Json;
import io.contextfence.common.Problem;
import io.contextfence.common.Values;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import java.sql.Timestamp;
import java.util.HashSet;
import org.springframework.stereotype.Service;

@Service
public class SourceService {
    private final Database db;
    private final SourceRepository sources;
    public SourceService(Database db) { this.db = db; sources = new SourceRepository(db); }

    public SourceResult apply(Caller caller, SourceEvent input) {
        caller.requireAny(Role.SOURCE_WRITER);
        SourceEvent event = normalize(input);
        String fingerprint = Values.hash(Json.write(event));
        return db.write(caller.tenant(), () -> {
            var previous = db.jdbc().query("select payload_hash, result::text from source_events where tenant=? and source_id=? and sequence=?",
                    (rs, n) -> new String[]{rs.getString(1), rs.getString(2)}, caller.tenant(), event.sourceId(), event.sequence());
            if (!previous.isEmpty()) {
                if (!previous.getFirst()[0].equals(fingerprint)) throw new Problem(409, "EVENT_CONFLICT");
                return Json.read(previous.getFirst()[1], SourceResult.class);
            }
            var current = sources.find(caller.tenant(), event.sourceId());
            if (current != null && event.sequence() < current.sequence()) {
                return record(caller.tenant(), event, fingerprint,
                        new SourceResult(event.sourceId(), event.sequence(), "IGNORED_STALE", current.version(), current.epoch()));
            }
            if (current != null && current.state().equals("DELETED")) throw new Problem(410, "SOURCE_DELETED");
            if (event.freshUntil().isAfter(db.now().plusSeconds(300))) throw new Problem(400, "INVALID_FRESHNESS");
            long version = current == null ? 1 : current.version() + (current.content().equals(event.content()) ? 0 : 1);
            long epoch = current == null ? 1 : current.epoch() +
                    (current.readers().equals(event.readers()) && current.state().equals(event.state()) ? 0 : 1);
            db.jdbc().update("""
                    insert into source_state(tenant,source_id,last_sequence,content_version,auth_epoch,state,content,content_hash,readers,fresh_until)
                    values (?,?,?,?,?,?,?,?,?::jsonb,?)
                    on conflict(tenant,source_id) do update set last_sequence=excluded.last_sequence,
                    content_version=excluded.content_version,auth_epoch=excluded.auth_epoch,state=excluded.state,
                    content=excluded.content,content_hash=excluded.content_hash,readers=excluded.readers,fresh_until=excluded.fresh_until
                    """, caller.tenant(), event.sourceId(), event.sequence(), version, epoch, event.state(), event.content(),
                    Values.hash(event.content()), Json.write(event.readers()), Timestamp.from(event.freshUntil()));
            return record(caller.tenant(), event, fingerprint,
                    new SourceResult(event.sourceId(), event.sequence(), "APPLIED", version, epoch));
        });
    }

    private SourceResult record(String tenant, SourceEvent event, String fingerprint, SourceResult result) {
        db.jdbc().update("insert into source_events(tenant,source_id,sequence,payload_hash,result) values (?,?,?,?,?::jsonb)",
                tenant, event.sourceId(), event.sequence(), fingerprint, Json.write(result));
        return result;
    }

    private static SourceEvent normalize(SourceEvent input) {
        if (input == null || input.sequence() == null || input.sequence() < 1) throw new Problem(400, "INVALID_EVENT");
        if (!"ACTIVE".equals(input.state()) && !"DELETED".equals(input.state())) throw new Problem(400, "INVALID_STATE");
        if (input.readers() == null || input.readers().size() > 100 || input.readers().stream().anyMatch(java.util.Objects::isNull)
                || new HashSet<>(input.readers()).size() != input.readers().size()) throw new Problem(400, "INVALID_READERS");
        var readers = input.readers().stream().map(Values::id).sorted().toList();
        String content = Values.text(input.content());
        if (input.state().equals("DELETED") && (!content.isEmpty() || !readers.isEmpty())) throw new Problem(400, "INVALID_DELETION");
        return new SourceEvent(Values.id(input.sourceId()), input.sequence(), content, readers, input.state(), Values.timestamp(input.freshUntil()));
    }
}
