package io.contextfence.identity;

import io.contextfence.common.Json;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;

/** Startup-only credential mapping. Only fixed-size token digests are retained. */
public final class IdentityStore {
    private static final Pattern ID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}");
    private static final Pattern TOKEN = Pattern.compile("[\\x21-\\x7E]{32,4096}");
    private static final int CONFIG_LIMIT = 1024 * 1024;
    private final Map<String, Caller> byDigest;

    private IdentityStore(Map<String, Caller> principals) { byDigest = Map.copyOf(principals); }

    public static IdentityStore fromPath(Path file) {
        try {
            if (file == null || !Files.isRegularFile(file)) throw invalid();
            byte[] bytes;
            try (var stream = Files.newInputStream(file)) { bytes = stream.readNBytes(CONFIG_LIMIT + 1); }
            if (bytes.length > CONFIG_LIMIT) throw invalid();
            return fromJson(new String(bytes, StandardCharsets.UTF_8));
        } catch (Exception failure) { throw invalid(); }
    }

    public static IdentityStore fromJson(String input) {
        try {
            Config config = Json.read(input, Config.class);
            if (config.principals() == null || config.principals().isEmpty()) throw invalid();
            Map<String, Caller> identities = new HashMap<>();
            for (Principal principal : config.principals()) {
                if (principal == null || !validId(principal.tenant()) || !validId(principal.subject())
                        || principal.token() == null || !TOKEN.matcher(principal.token()).matches()
                        || principal.roles() == null || principal.roles().isEmpty()) throw invalid();
                Set<Caller.Role> roles = Set.copyOf(principal.roles());
                Caller caller = new Caller(principal.tenant(), principal.subject(), roles);
                if (identities.putIfAbsent(digest(principal.token()), caller) != null) throw invalid();
            }
            return new IdentityStore(identities);
        } catch (Exception failure) { throw invalid(); }
    }

    public Optional<Caller> authenticate(String token) {
        if (token == null || !TOKEN.matcher(token).matches()) return Optional.empty();
        return Optional.ofNullable(byDigest.get(digest(token)));
    }

    private static boolean validId(String id) { return id != null && ID.matcher(id).matches(); }
    private static IllegalStateException invalid() { return new IllegalStateException("INVALID_IDENTITY_CONFIGURATION"); }
    private static String digest(String token) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(token.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException impossible) { throw invalid(); }
    }

    private record Config(List<Principal> principals) {}
    private record Principal(String token, String tenant, String subject, List<Caller.Role> roles) {}
}
