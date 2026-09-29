package io.contextfence.identity;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.Environment;
import java.nio.file.Path;

@Configuration(proxyBeanMethods = false)
public class IdentityConfiguration {
    @Bean IdentityStore identityStore(Environment environment) {
        String configured = environment.getProperty("CONTEXTFENCE_IDENTITIES_FILE");
        try {
            return IdentityStore.fromPath(configured == null || configured.isBlank() ? null : Path.of(configured));
        } catch (Exception invalid) {
            throw new IllegalStateException("INVALID_IDENTITY_CONFIGURATION");
        }
    }
}
