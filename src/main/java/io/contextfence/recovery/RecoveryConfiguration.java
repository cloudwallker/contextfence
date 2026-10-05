package io.contextfence.recovery;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.Environment;

@Configuration(proxyBeanMethods = false)
public class RecoveryConfiguration {
    @Bean RecoveryGate recoveryGate(Environment environment) {
        return new RecoveryGate(environment.getProperty("CONTEXTFENCE_GATE_FILE"));
    }
}
