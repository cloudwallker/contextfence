package io.contextfence.recovery;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 10)
public final class RecoveryGateFilter extends OncePerRequestFilter {
    private final RecoveryGate gate;
    public RecoveryGateFilter(RecoveryGate gate) { this.gate = gate; }
    @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain) throws ServletException, IOException {
        if (!gate.isOpen() && protectedRequest(request)) {
            request.setAttribute("io.contextfence.observability.reason", "RECOVERY_REQUIRED");
            response.setStatus(503);
            response.setHeader("Cache-Control", "no-store");
            response.setContentType("application/json");
            response.getWriter().write("{\"code\":\"RECOVERY_REQUIRED\"}");
            return;
        }
        chain.doFilter(request, response);
    }

    private static boolean protectedRequest(HttpServletRequest request) {
        if (protectedPath(request.getServletPath())) return true;
        try {
            String decoded = java.net.URI.create(request.getRequestURI()).getPath();
            String normalized = java.net.URI.create(decoded).normalize().getPath();
            return protectedPath(decoded) || protectedPath(normalized);
        } catch (IllegalArgumentException malformed) { return true; }
    }

    private static boolean protectedPath(String path) {
        return path != null && ("/v1".equals(path) || path.startsWith("/v1/") || "/health/ready".equals(path));
    }
}
