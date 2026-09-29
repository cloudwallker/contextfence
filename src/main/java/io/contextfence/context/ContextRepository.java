package io.contextfence.context;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.common.Problem;
import io.contextfence.common.Values;
import io.contextfence.identity.Caller;
import io.contextfence.persistence.Database;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

/** Called only inside the tenant guard transaction. Metadata queries exclude body text. */
public final class ContextRepository {
    public record Item(UUID id, String kind, Instant expiresAt, int depth, String hash,
                       int bytes, List<SourceVersion> sources) {}
    private final Database db;
    public ContextRepository(Database db) { this.db = db; }

    public List<Item> owned(Caller caller, List<UUID> ids) {
        List<Item> result = new ArrayList<>();
        for (UUID id : ids) {
            var rows = db.jdbc().query("""
                select id,kind,expires_at,depth,content_hash,octet_length(content) as bytes
                from context_items where tenant=? and owner_subject=? and id=?
                """, (rs, n) -> new Item(id, rs.getString("kind"), rs.getTimestamp("expires_at").toInstant(),
                    rs.getInt("depth"), rs.getString("content_hash"), rs.getInt("bytes"), dependencies(caller.tenant(), id)),
                    caller.tenant(), caller.subject(), id);
            if (rows.isEmpty()) throw new Problem(404, "NOT_FOUND");
            result.add(rows.getFirst());
        }
        return List.copyOf(result);
    }
    private List<SourceVersion> dependencies(String tenant, UUID id) {
        return db.jdbc().query("select source_id,content_version,auth_epoch from context_sources where tenant=? and context_id=? order by source_id",
            (rs, n) -> new SourceVersion(rs.getString(1), rs.getLong(2), rs.getLong(3)), tenant, id);
    }
    public String body(String tenant, Item item) {
        return db.jdbc().queryForObject("select content from context_items where tenant=? and id=? and content_hash=?",
                String.class, tenant, item.id(), item.hash());
    }
    public ContextHandle insert(Caller caller, String kind, String content, List<UUID> parents,
                                int depth, Instant expiry, List<SourceVersion> sources) {
        UUID id = UUID.randomUUID();
        db.jdbc().update("""
            insert into context_items(tenant,id,owner_subject,kind,content,content_hash,parent_ids,depth,expires_at)
            values(?,?,?,?,?,?,?::jsonb,?,?)
            """, caller.tenant(), id, caller.subject(), kind, content, Values.hash(content), Json.write(parents), depth, Timestamp.from(expiry));
        for (var source : sources) db.jdbc().update("insert into context_sources values(?,?,?,?,?)",
                caller.tenant(), id, source.sourceId(), source.contentVersion(), source.authEpoch());
        return new ContextHandle(id, kind, expiry, depth, List.copyOf(sources));
    }
}
