package io.contextfence.api;

import io.contextfence.api.Contracts.*;
import io.contextfence.admission.AdmissionService;
import io.contextfence.audit.ReceiptService;
import io.contextfence.common.Json;
import io.contextfence.context.ContextService;
import io.contextfence.identity.Caller;
import io.contextfence.observability.RequestOutcome;
import jakarta.servlet.http.HttpServletRequest;
import io.contextfence.sources.SourceService;
import org.springframework.http.CacheControl;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.*;
import java.util.UUID;

/** Parsing and serialization always use the strict application wire format. */
@RestController
@RequestMapping("/v1")
public class ContextController {
    private final SourceService sources;
    private final ContextService contexts;
    private final AdmissionService admission;
    private final ReceiptService receipts;

    public ContextController(SourceService sources, ContextService contexts, AdmissionService admission, ReceiptService receipts) {
        this.sources = sources; this.contexts = contexts; this.admission = admission; this.receipts = receipts;
    }

    @PostMapping(value = "/source-events", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> event(@AuthenticationPrincipal Caller caller, @RequestBody String body) {
        return json(200, sources.apply(caller, Json.read(body, SourceEvent.class)));
    }

    @PostMapping(value = "/contexts/source", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> source(@AuthenticationPrincipal Caller caller, @RequestBody String body) {
        return json(201, contexts.createSource(caller, Json.read(body, SourceContextRequest.class)));
    }

    @PostMapping(value = "/contexts/derived", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> derived(@AuthenticationPrincipal Caller caller, @RequestBody String body) {
        return json(201, contexts.createDerived(caller, Json.read(body, DerivedContextRequest.class)));
    }

    @PostMapping(value = "/contexts/assemble", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> assemble(@AuthenticationPrincipal Caller caller, @RequestBody String body, HttpServletRequest request) {
        AdmissionDecision decision = admission.assemble(caller, Json.read(body, AssembleRequest.class));
        RequestOutcome.mark(request, decision.code());
        return json(decision.status(), decision);
    }

    @GetMapping("/receipts/{id}")
    public ResponseEntity<String> receipt(@AuthenticationPrincipal Caller caller, @PathVariable UUID id) {
        return json(200, receipts.get(caller, id));
    }

    static ResponseEntity<String> json(int status, Object value) {
        return ResponseEntity.status(status).cacheControl(CacheControl.noStore()).contentType(MediaType.APPLICATION_JSON)
                .body(Json.write(value));
    }
}
