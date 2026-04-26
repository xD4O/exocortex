from __future__ import annotations

# OTel API without SDK = no-op spans. Instrumentation sites are real; configuring
# an SDK + exporter (OTLP to a collector) comes in Phase 8 hardening. Code that
# wraps operations in `with tracer.start_as_current_span(...)` works unchanged
# when the SDK gets wired in.

try:
    from opentelemetry import trace as _otel_trace  # type: ignore[import-not-found]

    def get_tracer(name: str = "exocortex") -> _otel_trace.Tracer:
        return _otel_trace.get_tracer(name)

except ImportError:  # pragma: no cover - OTel is a soft dep
    from contextlib import contextmanager

    class _NoopSpan:
        def set_attribute(self, *_args: object, **_kwargs: object) -> None:
            return None

        def record_exception(self, *_args: object, **_kwargs: object) -> None:
            return None

    class _NoopTracer:
        @contextmanager
        def start_as_current_span(self, *_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield _NoopSpan()

    def get_tracer(name: str = "exocortex") -> _NoopTracer:  # type: ignore[misc]
        return _NoopTracer()
