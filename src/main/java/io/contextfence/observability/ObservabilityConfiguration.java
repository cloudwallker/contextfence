package io.contextfence.observability;

import io.contextfence.api.HealthController;
import io.micrometer.common.KeyValues;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.binder.MeterBinder;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.server.observation.ServerRequestObservationContext;
import org.springframework.http.server.observation.ServerRequestObservationConvention;

@Configuration(proxyBeanMethods = false)
public class ObservabilityConfiguration {
    @Bean ServerRequestObservationConvention safeHttpObservationConvention() {
        return new ServerRequestObservationConvention() {
            @Override public String getName() { return "http.server.requests"; }
            @Override public KeyValues getLowCardinalityKeyValues(ServerRequestObservationContext context) {
                var request = context.getCarrier();
                int status = RequestOutcome.status(context.getResponse() == null ? 500 : context.getResponse().getStatus());
                return KeyValues.of("method", RequestOutcome.method(request), "uri", RequestOutcome.route(request),
                        "status", Integer.toString(status), "reason", RequestOutcome.reason(request, status));
            }
            @Override public KeyValues getHighCardinalityKeyValues(ServerRequestObservationContext context) { return KeyValues.empty(); }
        };
    }

    @Bean MeterBinder readinessMetrics(ObjectProvider<HealthController> health) {
        return registry -> {
            HealthController controller = health.getIfAvailable();
            if (controller != null) Gauge.builder("contextfence.ready", controller, current -> current.isReady() ? 1 : 0)
                    .description("Whether the database, schema and recovery gate permit serving requests")
                    .register(registry);
        };
    }
}
