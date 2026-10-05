package io.contextfence.api;

import io.contextfence.common.Problem;
import io.contextfence.observability.RequestOutcome;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.dao.DataAccessException;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.transaction.TransactionException;
import org.springframework.web.HttpMediaTypeNotAcceptableException;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.servlet.NoHandlerFoundException;
import org.springframework.web.servlet.resource.NoResourceFoundException;
import java.sql.SQLException;
import java.util.Map;

/** Failures expose only fixed application codes; input and exception text stay private. */
@RestControllerAdvice
public class ExceptionAdvice {
    @ExceptionHandler(Problem.class)
    public ResponseEntity<String> problem(Problem failure, HttpServletRequest request) { return error(request, failure.status(), failure.code()); }

    @ExceptionHandler({DataAccessException.class, TransactionException.class, SQLException.class})
    public ResponseEntity<String> database(Exception failure, HttpServletRequest request) { return error(request, 503, "DATABASE_UNAVAILABLE"); }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<String> json(Exception failure, HttpServletRequest request) { return error(request, 400, "INVALID_JSON"); }

    @ExceptionHandler({MethodArgumentTypeMismatchException.class, MissingServletRequestParameterException.class})
    public ResponseEntity<String> request(Exception failure, HttpServletRequest request) { return error(request, 400, "INVALID_REQUEST"); }

    @ExceptionHandler({NoResourceFoundException.class, NoHandlerFoundException.class})
    public ResponseEntity<String> missing(Exception failure, HttpServletRequest request) { return error(request, 404, "NOT_FOUND"); }

    @ExceptionHandler(HttpRequestMethodNotSupportedException.class)
    public ResponseEntity<String> method(Exception failure, HttpServletRequest request) { return error(request, 405, "METHOD_NOT_ALLOWED"); }

    @ExceptionHandler(HttpMediaTypeNotSupportedException.class)
    public ResponseEntity<String> media(Exception failure, HttpServletRequest request) { return error(request, 415, "UNSUPPORTED_MEDIA_TYPE"); }

    @ExceptionHandler(HttpMediaTypeNotAcceptableException.class)
    public ResponseEntity<String> accept(Exception failure, HttpServletRequest request) { return error(request, 406, "NOT_ACCEPTABLE"); }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<String> unexpected(Exception failure, HttpServletRequest request) {
        Throwable cause = failure;
        for (int depth = 0; cause != null && depth < 16; depth++, cause = cause.getCause()) {
            if (cause instanceof SQLException || cause instanceof DataAccessException || cause instanceof TransactionException)
                return error(request, 503, "DATABASE_UNAVAILABLE");
        }
        return error(request, 500, "INTERNAL_ERROR");
    }

    private static ResponseEntity<String> error(HttpServletRequest request, int status, String code) {
        RequestOutcome.mark(request, code);
        return ContextController.json(status, Map.of("code", code));
    }
}
