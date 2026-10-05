package io.contextfence.recovery;

import java.io.*;
import java.nio.file.*;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import static org.assertj.core.api.Assertions.*;

class RecoveryMainTest {
    @TempDir Path directory;
    ByteArrayOutputStream output = new ByteArrayOutputStream();
    int run(String[] args, Map<String,String> env) {
        return RecoveryMain.run(args, env, new PrintStream(output));
    }

    @Test void reconciliationRequiresAnExplicitClosedExternalGateBeforeAnyDatabaseConnection() throws Exception {
        Path ledger = directory.resolve("ledger.json");
        Files.writeString(ledger, SourceLedgerTest.document("[" + SourceLedgerTest.FIRST + "]"));
        assertThat(run(new String[]{"--reconcile-source-ledger", ledger.toString()}, Map.of())).isEqualTo(1);
        assertThat(output.toString()).contains("RECOVERY_GATE_REQUIRED");
        output.reset();
        Path gate = directory.resolve("gate.json");
        Files.writeString(gate, "{\"format_version\":1,\"state\":\"OPEN\"}");
        assertThat(run(new String[]{"--reconcile-source-ledger", ledger.toString()}, Map.of("CONTEXTFENCE_GATE_FILE", gate.toString()))).isEqualTo(1);
        assertThat(output.toString()).contains("RECOVERY_GATE_MUST_BE_CLOSED");
    }

    @Test void malformedLedgerAndIncompleteDatabaseConfigurationReturnFixedCodesWithoutInputs() throws Exception {
        Path gate = directory.resolve("gate.json");
        Files.writeString(gate, "{\"format_version\":1,\"state\":\"CLOSED\"}");
        Path ledger = directory.resolve("bad.json");
        Files.writeString(ledger, "private-input-marker");
        assertThat(run(new String[]{"--reconcile-source-ledger", ledger.toString()}, Map.of("CONTEXTFENCE_GATE_FILE", gate.toString()))).isEqualTo(1);
        assertThat(output.toString()).contains("INVALID_SOURCE_LEDGER").doesNotContain("private-input-marker", ledger.toString());
        output.reset();
        assertThat(run(new String[]{"--migrate-only"}, Map.of("CONTEXTFENCE_DATABASE_PASSWORD", "synthetic-password-marker"))).isEqualTo(1);
        assertThat(output.toString()).contains("INVALID_RECOVERY_CONFIGURATION").doesNotContain("synthetic-password-marker");
    }

    @Test void unknownArgumentsFailWithoutStartingSpring() {
        assertThat(run(new String[]{"--migrate-only", "private-token-marker"}, Map.of())).isEqualTo(2);
        assertThat(output.toString()).contains("RECOVERY_USAGE").doesNotContain("private-token-marker");
    }
}
