# Use Node.js as base image
FROM docker.1ms.run/node:18-alpine AS dev-stage

# Set working directory
WORKDIR /app

# Copy package.json and package-lock.json files
COPY package*.json ./

# Keep downloaded package tarballs across package-lock changes. npm ci still installs
# exactly the lockfile versions and verifies their integrity hashes.
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci --prefer-offline --no-audit --registry="${NPM_REGISTRY}"

# Copy project files
COPY . .

# Development stage ends here, no build needed

FROM dev-stage AS build-stage

# Build application - skip type checking
RUN npm run build

# Use nginx as production environment image
FROM docker.1ms.run/nginx:stable-alpine AS production-stage

# Copy built files to nginx
COPY --from=build-stage /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf.template

# Copy entrypoint script
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Expose port 80
EXPOSE 80

# Start nginx with environment variable substitution
CMD ["/docker-entrypoint.sh"]
