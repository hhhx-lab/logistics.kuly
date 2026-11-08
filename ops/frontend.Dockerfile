FROM node:22.16-alpine AS build
WORKDIR /workspace/src/frontend
COPY src/frontend/package.json src/frontend/package-lock.json ./
RUN npm ci
COPY src/frontend ./
RUN npm run build
FROM nginx:1.28-alpine
COPY ops/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/src/frontend/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
