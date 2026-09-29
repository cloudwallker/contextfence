package io.contextfence.common;

/** An application failure whose public message contains only a fixed code. */
public final class Problem extends RuntimeException {
    private final int status;
    private final String code;

    public Problem(int status, String code) {
        super(code, null, false, false);
        this.status = status;
        this.code = code;
    }

    public int status() { return status; }
    public String code() { return code; }
}
