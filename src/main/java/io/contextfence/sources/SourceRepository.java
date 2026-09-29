package io.contextfence.sources;

import com.fasterxml.jackson.core.type.TypeReference;
import io.contextfence.common.Json;
import io.contextfence.persistence.Database;
import java.time.Instant;
import java.util.List;

public final class SourceRepository {
    public record Source(String id, long sequence, long version, long epoch, String state,
                         String content, String hash, List<String> readers, Instant freshUntil) {}
    private final Database db;
    public SourceRepository(Database db) { this.db = db; }
    public Source find(String tenant, String id) {
        return find(tenant, id, true);
    }
    /** Admission reads current authority, never source text. */
    public Source metadata(String tenant, String id) { return find(tenant, id, false); }
    private Source find(String tenant, String id, boolean includeBody) {
        String projection = includeBody ? "*" : "source_id,last_sequence,content_version,auth_epoch,state,null::text as content,content_hash,readers,fresh_until";
        var rows = db.jdbc().query("select " + projection + " from source_state where tenant=? and source_id=?", (rs, n) ->
                new Source(rs.getString("source_id"), rs.getLong("last_sequence"), rs.getLong("content_version"),
                        rs.getLong("auth_epoch"), rs.getString("state"), rs.getString("content"), rs.getString("content_hash"),
                        Json.read(rs.getString("readers"), new TypeReference<List<String>>() {}),
                        rs.getTimestamp("fresh_until").toInstant()), tenant, id);
        return rows.isEmpty() ? null : rows.getFirst();
    }
}
