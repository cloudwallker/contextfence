package io.contextfence.identity;

import io.contextfence.common.Problem;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import static org.assertj.core.api.Assertions.*;

class IdentityStoreTest {
    // Deliberately public, synthetic fixture. Never valid outside this test.
    static final String TOKEN = "test-only-context-fence-token-0000000001";
    static final String VALID = "{\"principals\":[{\"token\":\"" + TOKEN + "\",\"tenant\":\"acme\",\"subject\":\"alice\",\"roles\":[\"READER\",\"PRODUCER\"]}]}";

    @Test void mapsTokenToTrustedCallerAndDoesNotAcceptUnknownCredentials() {
        IdentityStore store = IdentityStore.fromJson(VALID);
        Caller caller = store.authenticate(TOKEN).orElseThrow();
        assertThat(caller).isEqualTo(new Caller("acme", "alice", Set.of(Caller.Role.READER, Caller.Role.PRODUCER)));
        assertThat(store.authenticate(TOKEN + "x")).isEmpty();
        assertThat(store.authenticate(null)).isEmpty();
        caller.requireAny(Caller.Role.READER);
        Problem denied = catchThrowableOfType(Problem.class, () -> caller.requireAny(Caller.Role.SOURCE_WRITER));
        assertThat(denied.status()).isEqualTo(403);
        assertThat(denied.code()).isEqualTo("FORBIDDEN");
        assertThat(caller.toString()).doesNotContain(TOKEN);
    }

    @Test void rejectsAmbiguousWeakOrMalformedConfigurationWithoutEcho() {
        String principal = VALID.substring(15, VALID.length() - 2);
        for (String input : new String[]{
                "{}", "{\"principals\":[]}",
                VALID.replace("\"READER\",\"PRODUCER\"", ""),
                VALID.replace("alice", "用户"), VALID.replace("acme", "tenant with spaces"),
                VALID.replace(TOKEN, "short"), VALID.replace("READER", "ADMIN"),
                "{\"principals\":[" + principal + "," + principal + "]}",
                VALID.replace("\"tenant\":", "\"unknown\":123,\"tenant\":")}) {
            IllegalStateException invalid = catchThrowableOfType(IllegalStateException.class, () -> IdentityStore.fromJson(input));
            assertThat(invalid).isNotNull();
            assertThat(invalid.getMessage()).isEqualTo("INVALID_IDENTITY_CONFIGURATION");
            assertThat(invalid.getCause()).isNull();
        }
    }

    @Test void requiresAnExplicitReadableConfigurationFile(@TempDir Path directory) throws Exception {
        assertThatThrownBy(() -> IdentityStore.fromPath(null)).isInstanceOf(IllegalStateException.class)
                .hasMessage("INVALID_IDENTITY_CONFIGURATION");
        assertThatThrownBy(() -> IdentityStore.fromPath(directory.resolve("missing"))).isInstanceOf(IllegalStateException.class)
                .hasMessage("INVALID_IDENTITY_CONFIGURATION");
        Path file = directory.resolve("identities.json");
        Files.writeString(file, VALID);
        assertThat(IdentityStore.fromPath(file).authenticate(TOKEN)).isPresent();
    }
}
