ARG MAVEN_IMAGE=public.ecr.aws/docker/library/maven@sha256:3a4ab3276a087bf276f79cae96b1af04f53731bec53fb2e651aca79e4b10211e
ARG JAVA_RUNTIME_IMAGE=public.ecr.aws/docker/library/eclipse-temurin@sha256:f04fb34e053148344e83317976114ec3f37e4b830ec8bdab5a2fe3cecd7d010b
FROM ${MAVEN_IMAGE} AS build
WORKDIR /build
COPY pom.xml ./
COPY src ./src
RUN mvn --batch-mode --no-transfer-progress -DskipTests package

FROM ${JAVA_RUNTIME_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 contextfence \
    && useradd --uid 10001 --gid contextfence --no-create-home --shell /usr/sbin/nologin contextfence
WORKDIR /app
COPY --from=build --chown=10001:10001 /build/target/context-fence.jar /app/context-fence.jar
COPY --chown=10001:10001 ops/runtime/entrypoint.sh /app/entrypoint.sh
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
