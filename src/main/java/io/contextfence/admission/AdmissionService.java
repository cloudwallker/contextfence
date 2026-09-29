package io.contextfence.admission;

import io.contextfence.api.Contracts.*;
import io.contextfence.audit.ReceiptService;
import io.contextfence.common.Values;
import io.contextfence.context.*;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import java.util.*;
import org.springframework.stereotype.Service;

@Service
public class AdmissionService {
    private final Database db;
    private final BodyCache cache;
    private final ContextRepository repository;
    private final ContextPolicy policy;
    private final ReceiptService receipts;
    public AdmissionService(Database db, BodyCache cache) {
        this.db = db; this.cache = cache; repository = new ContextRepository(db);
        policy = new ContextPolicy(db); receipts = new ReceiptService(db);
    }
    public AdmissionDecision assemble(Caller caller, AssembleRequest request) {
        caller.requireAny(Role.READER, Role.PRODUCER);
        var ids = Values.items(request.contextIds(), 16);
        // Return a value on denial: throwing would roll back its audit record.
        // TransactionTemplate commits before this method returns to the HTTP layer.
        return db.read(caller.tenant(), () -> {
            var now = db.now();
            var items = repository.owned(caller, ids);
            var validation = policy.validate(caller, items, now);
            var failure = validation.failure();
            if (failure == null && items.stream().mapToLong(ContextRepository.Item::bytes).sum() > 262144)
                failure = new ContextPolicy.Failure(413, "BATCH_TOO_LARGE", 6);
            String code = failure == null ? "ALLOWED" : failure.code();
            var receipt = new Receipt(UUID.randomUUID(), now, code, ids, validation.sources(), validation.reasons());
            List<ContextBody> bodies = failure == null ? items.stream().map(item -> new ContextBody(item.id(),
                    cache.get(caller.tenant(), item, () -> repository.body(caller.tenant(), item)))).toList() : List.of();
            receipts.save(caller, receipt);
            return new AdmissionDecision(failure == null ? 200 : failure.status(), code, receipt, bodies);
        });
    }
}
