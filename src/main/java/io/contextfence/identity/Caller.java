package io.contextfence.identity;

import io.contextfence.common.Problem;
import java.util.Set;

/** Trusted server-side identity. It is never populated from a business request. */
public record Caller(String tenant, String subject, Set<Role> roles) {
    public enum Role { READER, PRODUCER, SOURCE_WRITER }

    public Caller { roles = Set.copyOf(roles); }

    public void requireAny(Role... accepted) {
        for (Role role : accepted) if (roles.contains(role)) return;
        throw new Problem(403, "FORBIDDEN");
    }
}
