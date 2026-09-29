package io.contextfence.context;

import io.contextfence.api.Contracts.*;
import io.contextfence.common.Problem;
import io.contextfence.common.Values;
import io.contextfence.identity.Caller;
import io.contextfence.identity.Caller.Role;
import io.contextfence.persistence.Database;
import io.contextfence.sources.SourceRepository;
import java.util.*;
import org.springframework.stereotype.Service;

@Service
public class ContextService {
    private final Database db;
    private final ContextRepository repository;
    private final ContextPolicy policy;
    public ContextService(Database db) { this.db = db; repository = new ContextRepository(db); policy = new ContextPolicy(db); }
    public ContextHandle createSource(Caller caller, SourceContextRequest request) {
        caller.requireAny(Role.READER, Role.PRODUCER);
        String id = Values.id(request.sourceId()); int ttl = Values.ttl(request.ttlSeconds());
        return db.read(caller.tenant(), () -> {
            var now = db.now();
            var source = new SourceRepository(db).find(caller.tenant(), id);
            ContextPolicy.require(ContextPolicy.current(source, caller.subject(), now));
            return repository.insert(caller, "SOURCE", source.content(), List.of(), 0, now.plusSeconds(ttl),
                    List.of(new SourceVersion(id, source.version(), source.epoch())));
        });
    }
    public ContextHandle createDerived(Caller caller, DerivedContextRequest request) {
        caller.requireAny(Role.PRODUCER);
        String content = Values.text(request.content());
        var parentIds = Values.items(request.parentIds(), 16); int ttl = Values.ttl(request.ttlSeconds());
        return db.read(caller.tenant(), () -> {
            var now = db.now();
            var parents = repository.owned(caller, parentIds);
            var validation = policy.validate(caller, parents, now);
            ContextPolicy.require(validation.failure());
            int depth = parents.stream().mapToInt(ContextRepository.Item::depth).max().orElseThrow() + 1;
            if (depth > 4) throw new Problem(400, "MAX_DEPTH_EXCEEDED");
            var expiry = now.plusSeconds(ttl);
            for (var parent : parents) if (parent.expiresAt().isBefore(expiry)) expiry = parent.expiresAt();
            return repository.insert(caller, "DERIVED", content, parentIds, depth, expiry, validation.sources());
        });
    }
}
