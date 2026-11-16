FROM ghcr.io/charmbracelet/vhs@sha256:ea49a6a1c529be83153e88321892b5585964418f0b9055e8c1e0d732194234a3
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python-is-python3 && rm -rf /var/lib/apt/lists/*
