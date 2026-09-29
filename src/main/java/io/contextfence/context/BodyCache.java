package io.contextfence.context;

import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import com.github.benmanes.caffeine.cache.stats.CacheStats;
import io.contextfence.common.Values;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.stereotype.Component;

/** Stores immutable text only. Possessing a cache entry never authorizes a response. */
@Component
public class BodyCache {
    private record Key(String tenant, UUID id, String hash) {}
    private final Cache<Key,String> cache = Caffeine.newBuilder().maximumWeight(16 * 1024 * 1024)
            .weigher((Key k, String v) -> 256 + Values.bytes(v)).recordStats().build();
    public String get(String tenant, ContextRepository.Item item, Supplier<String> loader) {
        return cache.get(new Key(tenant, item.id(), item.hash()), ignored -> loader.get());
    }
    public CacheStats stats() { return cache.stats(); }
}
