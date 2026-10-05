package io.contextfence.observability;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** Runs before authentication and body limiting, logging only bounded metadata. */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 5)
public final class RequestLogFilter extends OncePerRequestFilter {
    private static final Logger LOG = LoggerFactory.getLogger(RequestLogFilter.class);
    private final String instance;
    private final String version;

    public RequestLogFilter(@Value("${contextfence.observability.instance:local}") String instance,
                            @Value("${contextfence.observability.version:0.1.0}") String version) {
        this.instance = safeMetadata(instance, "local");
        this.version = safeMetadata(version, "unknown");
    }

    @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        var previous = MDC.getCopyOfContextMap();
        String requestId = UUID.randomUUID().toString();
        long started = System.nanoTime();
        response.setHeader("X-Request-Id", requestId);
        MDC.clear();
        MDC.put("requestId", requestId);
        MDC.put("instance", instance);
        try {
            chain.doFilter(request, response);
        } catch (IOException | ServletException | RuntimeException failure) {
            response.setStatus(500);
            RequestOutcome.mark(request, "INTERNAL_ERROR");
            throw failure;
        } finally {
            try {
                int status = RequestOutcome.status(response.getStatus());
                LOG.atInfo().addKeyValue("version", version).addKeyValue("actor", RequestOutcome.actor(request))
                        .addKeyValue("route", RequestOutcome.route(request)).addKeyValue("method", RequestOutcome.method(request))
                        .addKeyValue("status", status).addKeyValue("reason", RequestOutcome.reason(request, status))
                        .addKeyValue("durationMs", (System.nanoTime() - started) / 1_000_000.0)
                        .log("request.completed");
            } finally {
                if (previous == null) MDC.clear(); else MDC.setContextMap(previous);
            }
        }
    }

    private static String safeMetadata(String value, String fallback) {
        return value != null && value.matches("[A-Za-z0-9][A-Za-z0-9_.-]{0,63}") ? value : fallback;
    }
}
