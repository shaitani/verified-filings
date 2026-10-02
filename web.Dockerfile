# The web container: the Web Client ([A]) built, served by Caddy, with /api passed
# to the Web Server. docker-compose.prod.yml names this file for its "web" service;
# web.Caddyfile says how it serves; web.Dockerfile.dockerignore -- the ignore file
# Docker pairs with this Dockerfile by its name -- keeps the build to web/.
#
#   docker compose -f docker-compose.prod.yml build web

# 1. Build the Angular app. Fonts are inlined from Google at build time, so this
#    stage needs the network; the result is plain files.
FROM node:24.18.0-slim AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npx ng build --configuration production

# 2. Serve them. Caddy as a user of its own, on 8080: nothing here needs root.
FROM caddy:2.11.4-alpine
RUN addgroup -S web && adduser -S -G web web \
    && mkdir -p /data /config && chown web:web /data /config
COPY web.Caddyfile /etc/caddy/Caddyfile
COPY --from=build /web/dist/web/browser /srv
USER web
EXPOSE 8080
