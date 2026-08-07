# Pin the verified multi-platform OCI index. Floating `latest` previously moved
# the Gateway protocol without a corresponding plugin change.
FROM ghcr.io/openclaw/openclaw:2026.7.1-2@sha256:8789721d2e9b24b780a1504b56deb4c6bd5c7dbf96a1dd117e7c45c2ed72c8ac

# Switch to root to install plugin dependencies
USER root

# Setup manus-claw plugin
RUN mkdir -p /home/node/.openclaw/extensions/manus-claw
COPY manus-claw/package.json manus-claw/package-lock.json /home/node/.openclaw/extensions/manus-claw/
RUN cd /home/node/.openclaw/extensions/manus-claw && npm ci --omit=dev --ignore-scripts
COPY manus-claw/src/ /home/node/.openclaw/extensions/manus-claw/src/
COPY manus-claw/openclaw.plugin.json /home/node/.openclaw/extensions/manus-claw/openclaw.plugin.json
RUN chown -R node:node /home/node/.openclaw

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Switch back to non-root user
USER node

# Expose manus-claw HTTP server port and OpenClaw gateway port
EXPOSE 18788
EXPOSE 18789

# Environment variables with defaults
ENV CLAW_TTL_SECONDS=0
ENV CLAW_SHUTDOWN_GRACE_SECONDS=30
ENV MANUS_API_BASE_URL=""
ENV MANUS_API_KEY=""
ENV OPENCLAW_GATEWAY_TOKEN=""

ENTRYPOINT ["/entrypoint.sh"]
