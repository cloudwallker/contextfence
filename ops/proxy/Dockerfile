ARG HAPROXY_IMAGE=public.ecr.aws/docker/library/haproxy@sha256:3a819392f5a6af2a9203d1c63ff0feba74f0c940623a558511be06d7d85e361c
FROM ${HAPROXY_IMAGE}
USER root
RUN apk add --no-cache socat curl
USER haproxy
