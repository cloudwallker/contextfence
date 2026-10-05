package io.contextfence.recovery;

import io.contextfence.common.Json;
import java.time.Instant;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.*;

class SourceLedgerTest {
    static final String FRESH = "2026-10-04T00:00:00Z";
    static final String FIRST = "{\"tenant\":\"acme\",\"source_id\":\"policy\",\"sequence\":1,\"content\":\"synthetic-v1\",\"readers\":[\"bob\",\"alice\"],\"state\":\"ACTIVE\",\"fresh_until\":\"" + FRESH + "\"}";

    static String document(String events) {
        // Independent wire checksum: recursive key sort, compact UTF-8 JSON.
        var values = Json.read(events, new com.fasterxml.jackson.core.type.TypeReference<List<Map<String,Object>>>() {});
        var sorted = values.stream().map(TreeMap::new).toList();
        String hash = java.util.HexFormat.of().formatHex(digest(Json.write(sorted)));
        return "{\"format_version\":1,\"complete\":true,\"events_sha256\":\"" + hash + "\",\"events\":" + events + "}";
    }
    static byte[] digest(String value) {
        try { return java.security.MessageDigest.getInstance("SHA-256").digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8)); }
        catch (Exception failure) { throw new AssertionError(failure); }
    }

    @Test void completeHistoryRebuildsIndependentContentAndAuthorizationChangesWithoutRenewingFreshness() {
        String second = FIRST.replace("\"sequence\":1", "\"sequence\":2").replace("synthetic-v1", "synthetic-v2");
        String third = second.replace("\"sequence\":2", "\"sequence\":3").replace("[\"bob\",\"alice\"]", "[\"bob\"]");
        String fourth = third.replace("\"sequence\":3", "\"sequence\":4").replace("[\"bob\"]", "[\"alice\",\"bob\"]");
        var ledger = SourceLedger.fromJson(document("[" + FIRST + "," + second + "," + third + "," + fourth + "]"));
        assertThat(ledger.projections()).hasSize(1);
        var latest = ledger.projections().getFirst();
        assertThat(latest.version()).isEqualTo(2);
        assertThat(latest.epoch()).isEqualTo(3);
        assertThat(latest.sequence()).isEqualTo(4);
        assertThat(latest.event().readers()).containsExactly("alice", "bob");
        assertThat(latest.event().freshUntil()).isEqualTo(Instant.parse(FRESH));
        assertThat(ledger.histories()).hasSize(4);
        assertThat(ledger.histories().getFirst().result().authEpoch()).isEqualTo(1);
    }

    @Test void rejectsHashMismatchAndMalformedEnvelopesWithoutEchoingInput() {
        String valid = document("[" + FIRST + "]");
        for (String bad : List.of(valid.replace("synthetic-v1", "private-input-marker"),
                valid.replace("\"complete\":true", "\"complete\":false"),
                valid.replace("\"format_version\":1", "\"format_version\":2"),
                valid.replace("\"format_version\":1", "\"format_version\":1,\"format_version\":1"),
                valid.replace("\"complete\":true", "\"complete\":true,\"unexpected\":1"))) {
            assertThatThrownBy(() -> SourceLedger.fromJson(bad)).isInstanceOf(IllegalArgumentException.class)
                    .hasMessage("INVALID_SOURCE_LEDGER");
        }
    }

    @Test void rejectsGapsDuplicateSequencesInvalidFieldsAndResurrectionAfterDeletion() {
        String second = FIRST.replace("\"sequence\":1", "\"sequence\":2");
        String deleted = second.replace("synthetic-v1", "").replace("[\"bob\",\"alice\"]", "[]").replace("ACTIVE", "DELETED");
        for (String events : List.of("[]", "[" + second + "]", "[" + FIRST + "," + FIRST + "]",
                "[" + FIRST + "," + second.replace("\"sequence\":2", "\"sequence\":3") + "]",
                "[" + FIRST.replace("[\"bob\",\"alice\"]", "[\"alice\",\"alice\"]") + "]",
                "[" + FIRST.replace("\"tenant\":\"acme\"", "\"tenant\":\"invalid tenant\"") + "]",
                "[" + FIRST.replace("\"state\":\"ACTIVE\"", "\"state\":\"OTHER\"") + "]",
                "[" + FIRST + "," + deleted + "," + FIRST.replace("\"sequence\":1", "\"sequence\":3") + "]")) {
            assertThatThrownBy(() -> SourceLedger.fromJson(document(events))).isInstanceOf(IllegalArgumentException.class)
                    .hasMessage("INVALID_SOURCE_LEDGER");
        }
    }

    @Test void tenantAndSourceAreIndependentHistoryKeys() {
        String other = FIRST.replace("\"tenant\":\"acme\"", "\"tenant\":\"beta\"").replace("synthetic-v1", "synthetic-other");
        var ledger = SourceLedger.fromJson(document("[" + FIRST + "," + other + "]"));
        assertThat(ledger.projections()).hasSize(2);
        assertThat(ledger.projections()).extracting(p -> p.key().tenant()).containsExactly("acme", "beta");
    }
}
