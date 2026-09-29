FROM maven:3.9.16-eclipse-temurin-21 AS build
WORKDIR /build
COPY pom.xml ./
COPY src ./src
RUN mvn --batch-mode --no-transfer-progress -DskipTests package

FROM eclipse-temurin:21-jre-jammy AS runtime
RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 contextfence \
    && useradd --uid 10001 --gid contextfence --no-create-home --shell /usr/sbin/nologin contextfence
WORKDIR /app
COPY --from=build --chown=10001:10001 /build/target/context-fence.jar /app/context-fence.jar
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/context-fence.jar"]
