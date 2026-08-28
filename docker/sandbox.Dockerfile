FROM python:3.11-slim

RUN python -m pip install --no-cache-dir "pytest==9.1.1"

USER 65532:65532
WORKDIR /workspace

ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]
CMD ["tests"]
