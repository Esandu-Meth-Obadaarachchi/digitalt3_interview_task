# =============================================================================
# The review interface: built once, served as static files
# =============================================================================
# The dev server is not a production server, and running `vite dev` in a
# container would ship the toolchain to serve a page. Vite builds to static
# files, so nginx serves those and proxies the API through, which also means
# the browser talks to one origin and CORS never enters the picture.
# =============================================================================

FROM node:20-alpine AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
# npm ci rather than npm install: it installs exactly the lock file and fails
# if the lock and the manifest disagree, which is the whole point of a
# reproducible build.
RUN npm ci

COPY frontend/ ./
RUN npm run build


# =============================================================================
FROM nginx:1.27-alpine AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# 127.0.0.1 rather than localhost. Inside the container localhost resolves to
# ::1 first, nginx listens on IPv4 only, and busybox wget does not fall back
# the way curl does. The container served every request correctly and reported
# itself unhealthy for four minutes.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -q --spider http://127.0.0.1/ || exit 1
