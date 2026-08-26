FROM node:24-bookworm-slim

ENV NODE_ENV=production \
    PORT=8000

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY api ./api
COPY server.mjs ./server.mjs

EXPOSE 8000

CMD ["npm", "start"]
