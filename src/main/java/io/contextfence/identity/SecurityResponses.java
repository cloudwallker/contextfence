package io.contextfence.identity;

import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;

final class SecurityResponses {
    private SecurityResponses() {}

    static void write(HttpServletResponse response, int status, String fixedCode) throws IOException {
        response.setStatus(status);
        response.setHeader("Cache-Control", "no-store");
        response.setContentType("application/json");
        response.setCharacterEncoding("UTF-8");
        response.getWriter().write("{\"code\":\"" + fixedCode + "\"}");
    }
}
