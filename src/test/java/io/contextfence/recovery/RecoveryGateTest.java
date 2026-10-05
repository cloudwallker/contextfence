package io.contextfence.recovery;

import java.nio.file.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.mock.web.*;
import static org.assertj.core.api.Assertions.*;

class RecoveryGateTest {
    @TempDir Path directory;

    @Test void configuredGateDeniesMissingMalformedAndClosedFiles() throws Exception {
        Path file = directory.resolve("app-gate.json");
        RecoveryGate gate = new RecoveryGate(file.toString());
        assertThat(gate.isOpen()).isFalse();
        for (String value : new String[]{"{}", "{\"format_version\":1,\"state\":\"CLOSED\"}",
                "{\"format_version\":2,\"state\":\"OPEN\"}", "{\"format_version\":1,\"state\":\"open\"}",
                "{\"format_version\":1,\"state\":\"OPEN\",\"state\":\"CLOSED\"}",
                "{\"format_version\":1,\"state\":\"OPEN\",\"unknown\":true}", "x".repeat(4097)}) {
            Files.writeString(file, value);
            assertThat(gate.isOpen()).isFalse();
        }
        assertThat(new RecoveryGate(" ").isOpen()).isFalse();
    }

    @Test void readsAtomicDirectoryReplacementsAndPreservesUnconfiguredDevelopmentMode() throws Exception {
        assertThat(new RecoveryGate((String) null).isOpen()).isTrue();
        Path file = directory.resolve("app-gate.json");
        RecoveryGate gate = new RecoveryGate(file.toString());
        Files.writeString(file, "{\"format_version\":1,\"state\":\"OPEN\"}");
        assertThat(gate.isOpen()).isTrue();
        Path replacement = directory.resolve("next.json");
        Files.writeString(replacement, "{\"format_version\":1,\"state\":\"CLOSED\"}");
        Files.move(replacement, file, StandardCopyOption.REPLACE_EXISTING);
        assertThat(gate.isOpen()).isFalse();
    }

    @Test void filterBlocksBusinessAndReadinessBeforeDownstreamButLeavesLiveAndMetricsAccessible() throws Exception {
        RecoveryGateFilter filter = new RecoveryGateFilter(new RecoveryGate(directory.resolve("missing").toString()));
        for (String route : new String[]{"/v1/contexts/assemble", "/v1/source-events", "/v1", "/health/ready"}) {
            MockHttpServletRequest request = new MockHttpServletRequest("POST", route);
            MockHttpServletResponse response = new MockHttpServletResponse();
            boolean[] downstream = {false};
            filter.doFilter(request, response, (req, res) -> downstream[0] = true);
            assertThat(downstream[0]).isFalse();
            assertThat(response.getStatus()).isEqualTo(503);
            assertThat(response.getHeader("Cache-Control")).isEqualTo("no-store");
            assertThat(response.getContentAsString()).isEqualTo("{\"code\":\"RECOVERY_REQUIRED\"}");
        }
        for (String route : new String[]{"/health/live", "/actuator/prometheus", "/not-a-route"}) {
            boolean[] downstream = {false};
            filter.doFilter(new MockHttpServletRequest("GET", route), new MockHttpServletResponse(), (req, res) -> downstream[0] = true);
            assertThat(downstream[0]).isTrue();
        }
    }

    @Test void encodedAndNormalizedBusinessPathsCannotBypassAClosedGate() throws Exception {
        RecoveryGateFilter filter = new RecoveryGateFilter(new RecoveryGate(directory.resolve("missing").toString()));
        for (String route : new String[]{"/v%31/contexts/assemble", "/v1%2Fsource-events", "/unused/../v1/contexts/source"}) {
            boolean[] downstream = {false};
            MockHttpServletResponse response = new MockHttpServletResponse();
            filter.doFilter(new MockHttpServletRequest("POST", route), response, (req, res) -> downstream[0] = true);
            assertThat(downstream[0]).isFalse();
            assertThat(response.getStatus()).isEqualTo(503);
        }
    }
}
