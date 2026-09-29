package io.contextfence.identity;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

@Configuration(proxyBeanMethods = false)
@EnableWebSecurity
public class SecurityConfiguration {
    @Bean SecurityFilterChain contextFenceSecurity(HttpSecurity http, IdentityStore identities) throws Exception {
        var bearer = new BearerIdentityFilter(identities);
        http.csrf(csrf -> csrf.disable())
                .formLogin(form -> form.disable())
                .httpBasic(basic -> basic.disable())
                .logout(logout -> logout.disable())
                .requestCache(cache -> cache.disable())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(HttpMethod.GET, "/health/live", "/health/ready").permitAll()
                        .requestMatchers(HttpMethod.POST, "/v1/source-events").hasRole("SOURCE_WRITER")
                        .requestMatchers(HttpMethod.POST, "/v1/contexts/derived").hasRole("PRODUCER")
                        .requestMatchers(HttpMethod.POST, "/v1/contexts/source", "/v1/contexts/assemble").hasAnyRole("READER", "PRODUCER")
                        .anyRequest().authenticated())
                .exceptionHandling(errors -> errors
                        .authenticationEntryPoint((request, response, failure) -> SecurityResponses.write(response, 401, "UNAUTHENTICATED"))
                        .accessDeniedHandler((request, response, failure) -> SecurityResponses.write(response, 403, "FORBIDDEN")))
                .addFilterBefore(bearer, UsernamePasswordAuthenticationFilter.class)
                .addFilterBefore(new BodySizeFilter(), BearerIdentityFilter.class);
        return http.build();
    }
}
