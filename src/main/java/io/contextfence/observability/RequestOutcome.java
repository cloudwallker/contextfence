package io.contextfence.observability;

import io.contextfence.common.Values;
import io.contextfence.identity.Caller;
import jakarta.servlet.http.HttpServletRequest;
import java.util.Set;

/** Telemetry accepts only fixed application values, never request text or URLs. */
public final class RequestOutcome {
    private static final String REASON = "io.contextfence.observability.reason";
    private static final String ACTOR = "io.contextfence.observability.actor";
    private static final Set<String> ROUTES = Set.of("/health/live", "/health/ready", "/actuator/prometheus",
            "/v1/source-events", "/v1/contexts/source", "/v1/contexts/derived", "/v1/contexts/assemble");
    private static final Set<String> METHODS = Set.of("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT");
    private static final Set<String> REASONS = Set.of("SUCCESS", "ALLOWED", "UNAUTHENTICATED", "FORBIDDEN",
            "REQUEST_TOO_LARGE", "DATABASE_UNAVAILABLE", "INVALID_JSON", "INVALID_REQUEST", "NOT_FOUND",
            "METHOD_NOT_ALLOWED", "UNSUPPORTED_MEDIA_TYPE", "NOT_ACCEPTABLE", "INTERNAL_ERROR", "NOT_READY",
            "SOURCE_DELETED", "SOURCE_ACCESS_DENIED", "SOURCE_UNVERIFIED", "CONTEXT_EXPIRED", "CONTEXT_STALE",
            "CONTEXT_RETIRED", "RECOVERY_REQUIRED", "TOO_MANY_SOURCES", "BATCH_TOO_LARGE", "MAX_DEPTH_EXCEEDED",
            "EVENT_CONFLICT", "INVALID_FRESHNESS", "INVALID_EVENT", "INVALID_STATE", "INVALID_READERS",
            "INVALID_DELETION", "INVALID_ID", "INVALID_CONTENT", "CONTENT_TOO_LARGE", "INVALID_TTL",
            "INVALID_ITEMS", "INVALID_TIMESTAMP", "SERIALIZATION_FAILURE");

    private RequestOutcome() {}

    public static void mark(HttpServletRequest request, String fixedCode) {
        request.setAttribute(REASON, REASONS.contains(fixedCode) ? fixedCode : "INTERNAL_ERROR");
    }

    public static void markCaller(HttpServletRequest request, Caller caller) {
        request.setAttribute(ACTOR, Values.hash(caller.tenant() + '\0' + caller.subject()));
    }

    static String actor(HttpServletRequest request) {
        Object value = request.getAttribute(ACTOR);
        return value instanceof String text && text.matches("[0-9a-f]{64}") ? text : "ANONYMOUS";
    }

    static String route(HttpServletRequest request) {
        String path = request.getRequestURI();
        if (ROUTES.contains(path)) return path;
        if (path.startsWith("/v1/receipts/") && path.indexOf('/', "/v1/receipts/".length()) < 0)
            return "/v1/receipts/{id}";
        return "UNKNOWN";
    }

    static String method(HttpServletRequest request) {
        String method = request.getMethod();
        return METHODS.contains(method) ? method : "OTHER";
    }

    static int status(int status) { return status >= 100 && status <= 599 ? status : 500; }

    static String reason(HttpServletRequest request, int status) {
        Object value = request.getAttribute(REASON);
        if (value instanceof String text && REASONS.contains(text)) return text;
        if (status >= 200 && status < 400) return "SUCCESS";
        if (status == 503 && route(request).equals("/health/ready")) return "NOT_READY";
        return switch (status) {
            case 401 -> "UNAUTHENTICATED";
            case 403 -> "FORBIDDEN";
            case 404 -> "NOT_FOUND";
            case 405 -> "METHOD_NOT_ALLOWED";
            case 406 -> "NOT_ACCEPTABLE";
            case 413 -> "REQUEST_TOO_LARGE";
            case 415 -> "UNSUPPORTED_MEDIA_TYPE";
            default -> "INTERNAL_ERROR";
        };
    }
}
