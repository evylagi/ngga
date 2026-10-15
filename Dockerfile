FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git bash \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g opencode-ai && opencode --version

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY index.js ./

ENV NODE_ENV=production

CMD ["npm", "start"]
