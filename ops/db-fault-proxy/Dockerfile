ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844
FROM ${PYTHON_IMAGE}
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
COPY scripts/db_fault_proxy.py /app/db_fault_proxy.py
USER 65532:65532
ENTRYPOINT ["python", "/app/db_fault_proxy.py"]
