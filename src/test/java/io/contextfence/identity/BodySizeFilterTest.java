package io.contextfence.identity;

import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import java.util.concurrent.atomic.AtomicInteger;
import static org.assertj.core.api.Assertions.assertThat;

class BodySizeFilterTest {
    @Test void rejectsChunkedBodyEvenWithoutContentLength() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/v1/contexts/derived") {
            @Override public int getContentLength() { return -1; }
            @Override public long getContentLengthLong() { return -1; }
        };
        request.setContent(new byte[300 * 1024 + 1]);
        MockHttpServletResponse response = new MockHttpServletResponse();
        AtomicInteger called = new AtomicInteger();
        new BodySizeFilter().doFilter(request, response, (req, res) -> called.incrementAndGet());
        assertThat(response.getStatus()).isEqualTo(413);
        assertThat(response.getHeader("Cache-Control")).isEqualTo("no-store");
        assertThat(response.getContentAsString()).isEqualTo("{\"code\":\"REQUEST_TOO_LARGE\"}");
        assertThat(called.get()).isZero();
    }
}
