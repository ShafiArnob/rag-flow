# The collector image has no shell or wget, so borrow busybox for the container health check.
FROM busybox:1.37-musl AS tools

FROM otel/opentelemetry-collector-contrib:0.139.0
COPY --from=tools /bin/busybox /busybox
COPY config.yaml /etc/otelcol-contrib/config.yaml
