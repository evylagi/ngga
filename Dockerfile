FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates bash git \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g opencode-ai

WORKDIR /workspace

ENV HOSTNAME=0.0.0.0
ENV PORT=4096
ENV NODE_ENV=production

EXPOSE 4096

CMD ["sh", "-c", "opencode web --hostname 0.0.0.0 --port ${PORT:-4096}"]
