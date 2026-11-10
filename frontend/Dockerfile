FROM node:22-alpine AS deps
RUN corepack enable
WORKDIR /app
RUN chown node:node /app
USER node
COPY --chown=node:node package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Development image: runs `next dev`. Compose Watch (infra/docker-compose.dev.yml) copies edits into /app/src.
FROM deps AS dev
COPY --chown=node:node . .
EXPOSE 3000
CMD ["node_modules/.bin/next", "dev", "--hostname", "0.0.0.0"]

FROM deps AS build
COPY --chown=node:node . .
RUN pnpm build

# Production image. Keep this stage last so a build without a target produces it.
FROM node:22-alpine AS production
# Next's standalone server binds to HOSTNAME, which Docker sets to the container id unless overridden.
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
