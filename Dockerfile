# Model Hub container: Python, dependencies, oc, rsync and the app in one image.
# Build:  docker build -t model-hub:latest .
# Run:    docker run -d --name model-hub -p 127.0.0.1:8767:8767 model-hub:latest
# Models are stored INSIDE the container (/models, no VOLUME on purpose) so that
# `docker commit` + `docker save` (export_with_models.ps1) carries them to an air-gapped site.
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/python-312:latest
FROM ${BASE_IMAGE}

ARG OC_VERSION=4.22.18
ARG OC_SHA256_AMD64=07751676809553e012448cdb3a755ffa7b11d7e25b8a6cc60a5a62a9f4b18159
ARG OC_SHA256_ARM64=d000fd4294136a48bf13631c40d23d0a9e3440378aba142563e2fbe45e330d77

USER 0

# System tools: rsync + tar make `oc rsync` fast and resumable.
RUN (dnf install -y --nodocs rsync tar gzip || microdnf install -y --nodocs rsync tar gzip) \
    && (dnf clean all || microdnf clean all) && rm -rf /var/cache/dnf /var/cache/yum

# OpenShift CLI, version pinned and checksum verified.
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64)  f="openshift-client-linux-${OC_VERSION}.tar.gz";       sha="${OC_SHA256_AMD64}" ;; \
      aarch64) f="openshift-client-linux-arm64-${OC_VERSION}.tar.gz"; sha="${OC_SHA256_ARM64}" ;; \
      *) echo "Unsupported architecture $(uname -m)"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/oc.tgz "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/${f}"; \
    echo "${sha}  /tmp/oc.tgz" | sha256sum -c -; \
    tar -xzf /tmp/oc.tgz -C /usr/local/bin oc; \
    rm -f /tmp/oc.tgz; \
    oc version --client

WORKDIR /app
COPY requirements.txt requirements-online.txt ./
RUN pip install --no-cache-dir -r requirements-online.txt

COPY main.py settings.py downloader.py packages.py deployer.py storage.py ./
COPY static ./static

# Writable by any UID in group 0 (OpenShift random-UID compatible).
RUN mkdir -p /models /opt/app-root/src/.kube \
    && chgrp -R 0 /app /models /opt/app-root/src \
    && chmod -R g=u /app /models /opt/app-root/src

ENV MODEL_HUB_MODELS_DIR=/models \
    MODEL_HUB_OFFLINE=0 \
    HOST=0.0.0.0 \
    PORT=8767 \
    HOME=/opt/app-root/src \
    HF_HOME=/tmp/hf \
    PYTHONUNBUFFERED=1

USER 1001
EXPOSE 8767

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/info' % os.environ.get('PORT','8767'), timeout=4)"

ENTRYPOINT ["sh", "-c", "exec python -m uvicorn main:app --host ${HOST} --port ${PORT}"]
