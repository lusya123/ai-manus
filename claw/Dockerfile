# Pin the verified multi-platform OCI index. Floating `latest` previously moved
# the Gateway from protocol v3 to v4 without any corresponding plugin change.
FROM ghcr.io/openclaw/openclaw:2026.6.34@sha256:25f5bacf51742c3231d7dd1d319fede009a9d5aeab8b6232cd309ac6475fe82c

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
