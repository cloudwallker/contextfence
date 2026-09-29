package io.contextfence.common;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.HexFormat;
import java.util.List;
import java.util.HashSet;

public final class Values {
    private Values() {}
    public static String id(String value) {
        if (value == null || !value.matches("[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")) throw new Problem(400, "INVALID_ID");
        return value;
    }
    public static String text(String value) {
        if (value == null || value.indexOf('\0') >= 0) throw new Problem(400, "INVALID_CONTENT");
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (Character.isHighSurrogate(c)) {
                if (++i >= value.length() || !Character.isLowSurrogate(value.charAt(i))) throw new Problem(400, "INVALID_CONTENT");
            } else if (Character.isLowSurrogate(c)) throw new Problem(400, "INVALID_CONTENT");
        }
        if (bytes(value) > 65536) throw new Problem(413, "CONTENT_TOO_LARGE");
        return value;
    }
    public static int bytes(String text) { return text.getBytes(StandardCharsets.UTF_8).length; }
    public static String hash(String content) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content.getBytes(StandardCharsets.UTF_8))); }
        catch (NoSuchAlgorithmException e) { throw new IllegalStateException("SHA256_UNAVAILABLE"); }
    }
    public static int ttl(Integer seconds) {
        int ttl = seconds == null ? 900 : seconds;
        if (ttl < 1 || ttl > 900) throw new Problem(400, "INVALID_TTL");
        return ttl;
    }
    public static <T> List<T> items(List<T> values, int max) {
        if (values == null || values.isEmpty() || values.size() > max || values.stream().anyMatch(java.util.Objects::isNull)
                || new HashSet<>(values).size() != values.size()) throw new Problem(400, "INVALID_ITEMS");
        return List.copyOf(values);
    }
    public static Instant timestamp(Instant value) {
        if (value == null || value.isBefore(Instant.EPOCH) || value.isAfter(Instant.parse("2100-01-01T00:00:00Z")))
            throw new Problem(400, "INVALID_TIMESTAMP");
        return value.truncatedTo(ChronoUnit.MICROS);
    }
}
