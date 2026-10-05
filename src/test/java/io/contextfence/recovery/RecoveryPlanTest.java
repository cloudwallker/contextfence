package io.contextfence.recovery;

import java.util.*;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.*;

class RecoveryPlanTest {
    SourceLedger ledger() { return SourceLedger.fromJson(SourceLedgerTest.document("[" + SourceLedgerTest.FIRST + "]")); }

    @Test void epochMustExceedBothEveryRestoredDependencyAndPreviousRecoveryGenerations() {
        var key = new SourceLedger.Key("acme", "policy");
        var plan = RecoveryPlan.create(ledger(), Map.of(key, new RecoveryPlan.Existing(4, 90)));
        assertThat(plan).hasSize(1);
        assertThat(plan.getFirst().rebuiltEpoch()).isEqualTo(1);
        assertThat(plan.getFirst().restoredEpoch()).isEqualTo(91);
        var repeated = RecoveryPlan.create(ledger(), Map.of(key, new RecoveryPlan.Existing(91, 90)));
        assertThat(repeated.getFirst().restoredEpoch()).isEqualTo(92);
    }

    @Test void missingExistingSourceOrEpochOverflowRejectsTheEntirePlan() {
        var missing = new SourceLedger.Key("acme", "missing");
        assertThatThrownBy(() -> RecoveryPlan.create(ledger(), Map.of(missing, new RecoveryPlan.Existing(1, 1))))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("INCOMPLETE_SOURCE_LEDGER");
        var present = new SourceLedger.Key("acme", "policy");
        assertThatThrownBy(() -> RecoveryPlan.create(ledger(), Map.of(present, new RecoveryPlan.Existing(Long.MAX_VALUE, 1))))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("RECOVERY_EPOCH_EXHAUSTED");
    }

    @Test void newSourcesGetARecoveryEpochAboveTheRebuiltHistory() {
        var plan = RecoveryPlan.create(ledger(), Map.of());
        assertThat(plan).hasSize(1);
        assertThat(plan.getFirst().restoredEpoch()).isEqualTo(2);
    }
}
