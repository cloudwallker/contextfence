package io.contextfence.common;

import com.fasterxml.jackson.core.type.TypeReference;
import org.junit.jupiter.api.Test;
import java.time.Instant;
import java.util.List;
import static org.assertj.core.api.Assertions.*;

class JsonTest {
    record Payload(String sourceId, Integer ttlSeconds, Instant freshUntil) {}

    @Test void rejectsUnknownDuplicateTrailingAndCoercedFieldsWithoutEcho() {
        int caseNumber = 0;
        for (String input : List.of(
                "{\"source_id\":\"private-body-marker\",\"tenant\":\"forged\"}",
                "{\"source_id\":\"a\",\"source_id\":\"private-body-marker\"}",
                "{\"source_id\":\"private-body-marker\"} {}",
                "{\"source_id\":123}", "{\"source_id\":1.5}", "{\"source_id\":true}", "{\"ttl_seconds\":\"15\"}",
                "{\"ttl_seconds\":1.5}", "{\"ttl_seconds\":true}", "null")) {
            Problem problem = catchThrowableOfType(Problem.class, () -> Json.read(input, Payload.class));
            assertThat(problem).as("invalid scalar or syntax case %s", ++caseNumber).isNotNull();
            assertThat(problem.status()).isEqualTo(400);
            assertThat(problem.code()).isEqualTo("INVALID_JSON");
            assertThat(problem.getMessage()).isEqualTo("INVALID_JSON");
            assertThat(problem.getCause()).isNull();
        }
    }

    @Test void usesSnakeCaseAndIsoTimeForRecordAndGenericCollection() {
        String wire = "{\"source_id\":\"policy\",\"ttl_seconds\":15,\"fresh_until\":\"2026-09-29T10:00:00Z\"}";
        Payload value = Json.read(wire, Payload.class);
        assertThat(value.sourceId()).isEqualTo("policy");
        assertThat(value.freshUntil()).isEqualTo(Instant.parse("2026-09-29T10:00:00Z"));
        assertThat(Json.write(value)).isEqualTo(wire);
        List<Payload> values = Json.read("[" + wire + "]", new TypeReference<>() {});
        assertThat(values).containsExactly(value);
    }
}
