package io.contextfence;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class ContextFenceApplication {
    public static void main(String[] args) {
        if (args.length > 0 && ("--migrate-only".equals(args[0]) || "--reconcile-source-ledger".equals(args[0]))) {
            io.contextfence.recovery.RecoveryMain.main(args);
            return;
        }
        SpringApplication.run(ContextFenceApplication.class, args);
    }
}
