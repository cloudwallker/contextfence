package io.contextfence.recovery;

import io.contextfence.common.Json;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;

/** Runtime state is external to PostgreSQL, reread for every request and atomic file replacement. */
public final class RecoveryGate {
    private record State(Integer formatVersion, String state) {}
    private final boolean configured;
    private final Path path;

    public RecoveryGate(String configured) {
        this.configured = configured != null;
        Path resolved = null;
        try { if (configured != null && !configured.isBlank()) resolved = Path.of(configured); }
        catch (Exception invalid) { /* A configured invalid path stays closed. */ }
        path = resolved;
    }

    public boolean isOpen() {
        if (!configured) return true; // Existing local development mode; Compose always supplies the gate.
        return matches("OPEN");
    }

    public boolean isClosed() { return configured && matches("CLOSED"); }

    private boolean matches(String expected) {
        if (path == null) return false;
        try (var stream = Files.newInputStream(path)) {
            byte[] bytes = stream.readNBytes(4097);
            if (bytes.length > 4096) return false;
            State document = Json.read(new String(bytes, StandardCharsets.UTF_8), State.class);
            return Integer.valueOf(1).equals(document.formatVersion()) && expected.equals(document.state());
        } catch (Exception invalid) { return false; }
    }
}
