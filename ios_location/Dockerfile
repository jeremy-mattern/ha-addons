ARG BUILD_FROM=docker.io/library/python:3.13-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/data

# usbmuxd talks to the iPhone over USB. Every Python dependency ships prebuilt
# wheels for amd64 and aarch64 on Python 3.13, so no compiler is needed.
COPY requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends usbmuxd libusb-1.0-0 ca-certificates \
    && pip install --only-binary=:all: -r /tmp/requirements.txt \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

COPY app /app

CMD ["python3", "-u", "/app/bridge.py"]

LABEL io.hass.type="addon" \
      io.hass.name="iOS Location Bridge"
