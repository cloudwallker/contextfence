package io.contextfence.identity;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;
import java.io.IOException;
import java.util.Collections;

final class BearerIdentityFilter extends OncePerRequestFilter {
    private final IdentityStore identities;
    BearerIdentityFilter(IdentityStore identities) { this.identities = identities; }

    @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        var authorization = Collections.list(request.getHeaders("Authorization"));
        if (authorization.isEmpty()) { chain.doFilter(request, response); return; }
        if (authorization.size() != 1) { SecurityResponses.write(response, 401, "UNAUTHENTICATED"); return; }
        String header = authorization.getFirst();
        if (header.length() < 8 || !header.regionMatches(true, 0, "Bearer ", 0, 7)) {
            SecurityResponses.write(response, 401, "UNAUTHENTICATED"); return;
        }
        var caller = identities.authenticate(header.substring(7));
        if (caller.isEmpty()) { SecurityResponses.write(response, 401, "UNAUTHENTICATED"); return; }
        var trusted = caller.get();
        var authorities = trusted.roles().stream().map(role -> new SimpleGrantedAuthority("ROLE_" + role.name())).toList();
        var context = SecurityContextHolder.createEmptyContext();
        context.setAuthentication(UsernamePasswordAuthenticationToken.authenticated(trusted, null, authorities));
        SecurityContextHolder.setContext(context);
        chain.doFilter(request, response);
    }
}
