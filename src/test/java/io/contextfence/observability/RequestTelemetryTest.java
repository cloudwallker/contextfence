package io.contextfence.observability;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import io.contextfence.identity.IdentityStore;
import io.contextfence.identity.SecurityConfiguration;
import io.contextfence.common.Json;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.Filter;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.FilterType;
import org.springframework.boot.logging.logback.StructuredLogEncoder;
import org.springframework.core.env.Environment;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.mock.web.MockServletContext;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.http.server.observation.ServerRequestObservationContext;
import org.springframework.http.server.observation.ServerRequestObservationConvention;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.context.support.AnnotationConfigWebApplicationContext;
import org.springframework.web.servlet.config.annotation.EnableWebMvc;
import static org.assertj.core.api.Assertions.*;
import static org.springframework.security.test.web.servlet.setup.SecurityMockMvcConfigurers.springSecurity;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.webAppContextSetup;

class RequestTelemetryTest {
    static final String TOKEN = "synthetic-observability-token-with-at-least-32-characters";
    static final String PRIVATE = "synthetic-protected-marker";
    AnnotationConfigWebApplicationContext context;
    MockMvc mvc;
    Logger logger;
    Level previousLevel;
    ListAppender<ILoggingEvent> events;

    @BeforeEach void open() {
        context = new AnnotationConfigWebApplicationContext();
        context.setServletContext(new MockServletContext());
        context.register(TestConfiguration.class, SecurityConfiguration.class);
        context.refresh();
        Filter[] filters = context.getBeansOfType(Filter.class).entrySet().stream()
                .filter(entry -> !entry.getKey().equals("springSecurityFilterChain"))
                .map(Map.Entry::getValue).toArray(Filter[]::new);
        mvc = webAppContextSetup(context).addFilters(filters).apply(springSecurity()).build();
        logger = (Logger) LoggerFactory.getLogger("io.contextfence.observability.RequestLogFilter");
        previousLevel = logger.getLevel();
        logger.setLevel(Level.INFO);
        events = new ListAppender<>() {
            @Override protected void append(ILoggingEvent event) {
                event.prepareForDeferredProcessing();
                super.append(event);
            }
        };
        events.start();
        logger.addAppender(events);
    }
    @AfterEach void close() {
        MDC.clear();
        if (logger != null) { logger.detachAppender(events); logger.setLevel(previousLevel); }
        if (context != null) context.close();
    }

    @Test void logsEarlyAuthenticationDenialWithoutUntrustedInputAndRestoresMdc() throws Exception {
        MDC.put("requestId", "outer-request");
        var result = mvc.perform(get("/unknown/" + PRIVATE).queryParam("token", TOKEN)
                        .header("X-Request-Id", TOKEN).header("Authorization", "Bearer " + PRIVATE))
                .andExpect(status().isUnauthorized()).andExpect(header().exists("X-Request-Id")).andReturn();
        String requestId = result.getResponse().getHeader("X-Request-Id");
        assertThatCode(() -> UUID.fromString(requestId)).doesNotThrowAnyException();
        assertThat(MDC.get("requestId")).isEqualTo("outer-request");
        assertThat(events.list).hasSize(1);
        var fields = fields(events.list.getFirst());
        assertThat(fields).containsEntry("requestId", requestId).containsEntry("route", "UNKNOWN")
                .containsEntry("status", 401).containsEntry("reason", "UNAUTHENTICATED");
        assertThat(fields).containsKey("instance").containsKey("durationMs");
        assertThat(events.list.getFirst().getFormattedMessage() + fields).doesNotContain(TOKEN, PRIVATE);
    }

    @Test void logsBodyLimitDenialWithoutReadingProtectedContentIntoLogs() throws Exception {
        mvc.perform(post("/v1/contexts/derived").header("Authorization", "Bearer " + TOKEN)
                        .content(PRIVATE + "x".repeat(300 * 1024)))
                .andExpect(status().isContentTooLarge()).andExpect(header().exists("X-Request-Id"));
        assertThat(events.list).hasSize(1);
        assertThat(fields(events.list.getFirst())).containsEntry("reason", "REQUEST_TOO_LARGE").containsEntry("status", 413);
        assertThat(events.list.toString() + fields(events.list.getFirst())).doesNotContain(PRIVATE, TOKEN);
        assertThat(MDC.getCopyOfContextMap()).isNullOrEmpty();
    }

    @Test void logsUnexpectedFailureOnceAndCleansMdc() {
        assertThatThrownBy(() -> mvc.perform(get("/crash").header("Authorization", "Bearer " + TOKEN)))
                .hasRootCauseInstanceOf(IllegalStateException.class);
        assertThat(events.list).hasSize(1);
        assertThat(fields(events.list.getFirst())).containsEntry("status", 500).containsEntry("reason", "INTERNAL_ERROR");
        assertThat(events.list.getFirst().getThrowableProxy()).isNull();
        assertThat(fields(events.list.getFirst()).toString()).doesNotContain(PRIVATE, TOKEN);
        assertThat(MDC.getCopyOfContextMap()).isNullOrEmpty();
    }

    @Test void normalizesDynamicAndUnknownRoutesAndRestrictsMetricLabels() {
        var conventions = context.getBeansOfType(ServerRequestObservationConvention.class);
        assertThat(conventions).hasSize(1);
        var convention = conventions.values().iterator().next();
        for (String path : new String[]{"/v1/receipts/11111111-1111-1111-1111-111111111111",
                "/v1/receipts/22222222-2222-2222-2222-222222222222"}) {
            var request = new MockHttpServletRequest("GET", path);
            request.setQueryString("token=" + TOKEN);
            var observation = new ServerRequestObservationContext(request, new MockHttpServletResponse());
            Map<String, String> tags = new LinkedHashMap<>();
            convention.getLowCardinalityKeyValues(observation).forEach(value -> tags.put(value.getKey(), value.getValue()));
            assertThat(tags).containsOnlyKeys("method", "uri", "status", "reason")
                    .containsEntry("uri", "/v1/receipts/{id}").containsEntry("status", "200");
            assertThat(tags.toString()).doesNotContain(path, TOKEN);
            assertThat(convention.getHighCardinalityKeyValues(observation)).isEmpty();
        }
        var unknown = new MockHttpServletRequest("synthetic-private-method", "/" + PRIVATE);
        unknown.setAttribute("io.contextfence.observability.reason", PRIVATE);
        var observation = new ServerRequestObservationContext(unknown, new MockHttpServletResponse());
        Map<String, String> tags = new LinkedHashMap<>();
        convention.getLowCardinalityKeyValues(observation).forEach(value -> tags.put(value.getKey(), value.getValue()));
        assertThat(tags).containsEntry("uri", "UNKNOWN").containsEntry("method", "OTHER");
        assertThat(tags.toString()).doesNotContain(PRIVATE);
    }

    @Test void emitsParseableBootJsonWithSafeCorrelationAndCallerMetadata() throws Exception {
        mvc.perform(get("/v1/receipts/11111111-1111-1111-1111-111111111111")
                .header("Authorization", "Bearer " + TOKEN)).andExpect(status().isNotFound());
        assertThat(events.list).hasSize(1);
        var loggerContext = new LoggerContext();
        loggerContext.putObject(Environment.class.getName(), new MockEnvironment());
        var encoder = new StructuredLogEncoder();
        encoder.setContext(loggerContext);
        encoder.setFormat("logstash");
        encoder.start();
        try {
            assertThatCode(() -> encoder.encode(events.list.getFirst())).doesNotThrowAnyException();
            String json = new String(encoder.encode(events.list.getFirst()), StandardCharsets.UTF_8);
            assertThatCode(() -> Json.read(json, Map.class)).doesNotThrowAnyException();
            Map<String, Object> data = Json.read(json, new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() {});
            assertThat(data).containsEntry("route", "/v1/receipts/{id}").containsEntry("status", 404)
                    .containsKeys("requestId", "instance", "version", "actor", "durationMs");
            assertThat(data.get("actor").toString()).matches("[0-9a-f]{64}");
            assertThat(json).doesNotContain(PRIVATE, TOKEN, "11111111-1111-1111-1111-111111111111", "alice", "\"test\"");
        } finally { encoder.stop(); loggerContext.stop(); }
    }

    static Map<String, Object> fields(ILoggingEvent event) {
        var result = new LinkedHashMap<String, Object>();
        result.putAll(event.getMDCPropertyMap());
        if (event.getKeyValuePairs() != null) event.getKeyValuePairs().forEach(pair -> result.put(pair.key, pair.value));
        return result;
    }

    @Configuration @EnableWebMvc @ComponentScan(basePackages = "io.contextfence.observability", useDefaultFilters = false,
            includeFilters = @ComponentScan.Filter(type = FilterType.REGEX,
                    pattern = "io\\.contextfence\\.observability\\.(RequestLogFilter|ObservabilityConfiguration)"))
    static class TestConfiguration {
        @Bean SimpleMeterRegistry registry() { return new SimpleMeterRegistry(); }
        @Bean IdentityStore identities() {
            return IdentityStore.fromJson("{\"principals\":[{\"token\":\"" + TOKEN + "\",\"tenant\":\"test\",\"subject\":\"alice\",\"roles\":[\"READER\"]}]}");
        }
        @Bean TestController controller() { return new TestController(); }
    }
    @RestController static class TestController {
        @GetMapping("/crash") String crash() { throw new IllegalStateException(PRIVATE); }
    }
}
