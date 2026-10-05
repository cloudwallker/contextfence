package io.contextfence.identity;

import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import io.contextfence.observability.RequestOutcome;
import java.io.IOException;

final class SecurityResponses {
    private SecurityResponses() {}

    static void write(HttpServletRequest request, HttpServletResponse response, int status, String fixedCode) throws IOException {
        RequestOutcome.mark(request, fixedCode);
        response.setStatus(status);
        response.setHeader("Cache-Control", "no-store");
        response.setContentType("application/json");
        response.setCharacterEncoding("UTF-8");
        response.getWriter().write("{\"code\":\"" + fixedCode + "\"}");
    }
}
