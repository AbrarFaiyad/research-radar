FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Placeholder pin: the owner should bump this to the current Claude Code release.
RUN npm install -g @anthropic-ai/claude-code@2.0.0 && npm cache clean --force

RUN useradd --uid 1000 --no-create-home --home-dir /tmp/builder --shell /bin/sh builder
COPY --chmod=0755 entry.sh /usr/local/bin/entry.sh

USER builder
ENV HOME=/tmp/builder
WORKDIR /work
CMD ["/usr/local/bin/entry.sh"]
