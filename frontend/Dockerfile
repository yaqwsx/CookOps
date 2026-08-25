FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS build

WORKDIR /app
COPY package.json package-lock.json ./
# Registry reads can transiently time out while building the demo image.
RUN npm ci --fetch-retries=5 --fetch-retry-factor=2 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000 --fetch-timeout=600000
COPY . ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine@sha256:8059701048a6cc7d64e8ab2812a44be70161bfa9ecfc01d69e95b8ff2a648705

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY runtime-config-entrypoint.sh /usr/local/bin/runtime-config-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/runtime-config-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
EXPOSE 8080
