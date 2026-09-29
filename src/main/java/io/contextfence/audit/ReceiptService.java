package io.contextfence.audit;

import com.fasterxml.jackson.core.type.TypeReference;
import io.contextfence.api.Contracts.*;
import io.contextfence.common.Json;
import io.contextfence.common.Problem;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import java.sql.Timestamp;
import java.util.*;
import org.springframework.stereotype.Service;

@Service
public class ReceiptService {
    private final Database db;
    public ReceiptService(Database db) { this.db = db; }
    public void save(Caller caller, Receipt receipt) {
        db.jdbc().update("insert into admission_receipts values(?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb)",
                caller.tenant(), receipt.id(), caller.subject(), Timestamp.from(receipt.checkedAt()), receipt.decision(),
                Json.write(receipt.contextIds()), Json.write(receipt.sources()), Json.write(receipt.reasons()));
    }
    public Receipt get(Caller caller, UUID id) {
        caller.requireAny(Role.READER, Role.PRODUCER);
        var rows = db.jdbc().query("select * from admission_receipts where tenant=? and owner_subject=? and id=?",
                (rs, n) -> new Receipt(id, rs.getTimestamp("checked_at").toInstant(), rs.getString("decision"),
                        Json.read(rs.getString("context_ids"), new TypeReference<List<UUID>>() {}),
                        Json.read(rs.getString("source_versions"), new TypeReference<List<SourceVersion>>() {}),
                        Json.read(rs.getString("reasons"), new TypeReference<List<Reason>>() {})), caller.tenant(), caller.subject(), id);
        if (rows.isEmpty()) throw new Problem(404, "NOT_FOUND");
        return rows.getFirst();
    }
}
