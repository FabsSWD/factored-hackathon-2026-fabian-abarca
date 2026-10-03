# The browser app (M14-M16) and the public entry point (M19). Build context: frontend/, plus
# this directory as the named context "nginx" (docker-compose.yml, additional_contexts).
#
# Stage 1 builds the static bundle; stage 2 serves it with nginx, which also proxies /api,
# /auth and /health to the API on the same origin and applies the public per-IP rate limit.
FROM node:22-alpine AS build
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.29-alpine
COPY --from=build /build/dist /usr/share/nginx/html
COPY --from=nginx nginx.conf.template /etc/nginx/templates/default.conf.template
# Defaults of the public rate limit (requests per second per client IP, and the burst allowed).
ENV PUBLIC_API_RATE_PER_SECOND=10 \
    PUBLIC_API_BURST=40
EXPOSE 80
