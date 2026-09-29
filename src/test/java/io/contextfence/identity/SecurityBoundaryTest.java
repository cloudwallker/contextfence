package io.contextfence.identity;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockServletContext;
import org.springframework.security.core.Authentication;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.context.support.AnnotationConfigWebApplicationContext;
import org.springframework.web.servlet.config.annotation.EnableWebMvc;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.security.test.web.servlet.setup.SecurityMockMvcConfigurers.springSecurity;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.webAppContextSetup;

class SecurityBoundaryTest {
    AnnotationConfigWebApplicationContext context;
    MockMvc mvc;
    @BeforeEach void open() {
        context = new AnnotationConfigWebApplicationContext();
        context.setServletContext(new MockServletContext());
        context.register(TestConfiguration.class, SecurityConfiguration.class);
        context.refresh();
        mvc = webAppContextSetup(context).apply(springSecurity()).build();
    }
    @AfterEach void close() { if (context != null) context.close(); }

    @Test void grantsOnlyExactGetHealthRoutesAnonymousAccess() throws Exception {
        mvc.perform(get("/health/live")).andExpect(status().isOk());
        mvc.perform(get("/health/ready")).andExpect(status().isOk());
        mvc.perform(post("/health/live")).andExpect(status().isUnauthorized());
        mvc.perform(get("/health/live/extra")).andExpect(status().isUnauthorized());
        mvc.perform(get("/protected")).andExpect(status().isUnauthorized())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(content().json("{\"code\":\"UNAUTHENTICATED\"}"));
    }

    @Test void usesTrustedPrincipalAndDoesNotPermitHeaderSpoofingOrSessions() throws Exception {
        var result = mvc.perform(get("/protected").header("Authorization", "Bearer " + IdentityStoreTest.TOKEN)
                .header("X-Tenant", "other").header("X-Subject", "eve"))
                .andExpect(status().isOk()).andExpect(content().string("acme/alice")).andReturn();
        assertThat(result.getRequest().getSession(false)).isNull();
        mvc.perform(get("/protected").header("X-Tenant", "acme").header("X-Subject", "alice"))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/protected").header("Authorization", "Bearer secret-input-marker"))
                .andExpect(status().isUnauthorized()).andExpect(content().json("{\"code\":\"UNAUTHENTICATED\"}"));
        mvc.perform(post("/v1/source-events").header("Authorization", "Bearer " + IdentityStoreTest.TOKEN))
                .andExpect(status().isForbidden()).andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(content().json("{\"code\":\"FORBIDDEN\"}"));
    }

    @Test void boundsBodyBeforeItReachesBusinessCode() throws Exception {
        mvc.perform(post("/protected").header("Authorization", "Bearer " + IdentityStoreTest.TOKEN)
                .contentType(MediaType.TEXT_PLAIN).content("x".repeat(300 * 1024)))
                .andExpect(status().isOk()).andExpect(content().string("307200"));
        mvc.perform(post("/protected").header("Authorization", "Bearer " + IdentityStoreTest.TOKEN)
                .contentType(MediaType.TEXT_PLAIN).content("x".repeat(300 * 1024 + 1)))
                .andExpect(status().isContentTooLarge()).andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(content().json("{\"code\":\"REQUEST_TOO_LARGE\"}"));
    }

    @Configuration @EnableWebMvc
    static class TestConfiguration {
        @Bean IdentityStore identities() { return IdentityStore.fromJson(IdentityStoreTest.VALID); }
        @Bean TestController controller() { return new TestController(); }
    }
    @RestController
    static class TestController {
        @GetMapping({"/health/live", "/health/ready"}) String health() { return "ok"; }
        @GetMapping("/protected") String caller(Authentication auth) {
            Caller caller = (Caller) auth.getPrincipal();
            return caller.tenant() + "/" + caller.subject();
        }
        @PostMapping("/v1/source-events") String writer() { return "writer"; }
        @PostMapping("/protected") String body(@RequestBody String body) { return Integer.toString(body.length()); }
    }
}
