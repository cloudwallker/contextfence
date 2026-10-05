ARG PYTHON_IMAGE=python:3.13.7-alpine
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY scripts/alert_receiver.py /app/alert_receiver.py
CMD ["python", "/app/alert_receiver.py"]
