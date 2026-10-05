package io.contextfence.context;

import io.micrometer.core.instrument.binder.MeterBinder;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.assertThat;

class BodyCacheMetricsTest {
    @Test void exportsHitsAndMissesWithoutCacheKeysOrBodies() {
        var registry = new SimpleMeterRegistry();
        var cache = new BodyCache();
        assertThat(cache).isInstanceOf(MeterBinder.class);
        ((MeterBinder) cache).bindTo(registry);
        String privateTenant = "synthetic-private-tenant";
        String privateBody = "synthetic-private-body";
        UUID privateId = UUID.randomUUID();
        var item = new ContextRepository.Item(privateId, "SOURCE", Instant.now().plusSeconds(30),
                0, "synthetic-private-hash", 22, List.of(), false);
        assertThat(cache.get(privateTenant, item, () -> privateBody)).isEqualTo(privateBody);
        assertThat(cache.get(privateTenant, item, () -> { throw new AssertionError("A hit must not reload"); })).isEqualTo(privateBody);
        assertThat(registry.get("cache.gets").tags("cache", "context-body", "result", "miss").functionCounter().count()).isEqualTo(1);
        assertThat(registry.get("cache.gets").tags("cache", "context-body", "result", "hit").functionCounter().count()).isEqualTo(1);
        assertThat(registry.getMeters()).isNotEmpty().allSatisfy(meter -> {
            assertThat(meter.getId().getTags().toString()).doesNotContain(privateTenant, privateBody, privateId.toString(), "synthetic-private-hash");
            assertThat(meter.getId().getTags()).allSatisfy(tag -> assertThat(tag.getKey()).isIn("cache", "result"));
        });
    }
}
