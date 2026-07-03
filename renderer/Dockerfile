FROM node:20-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY renderer/package*.json ./
RUN npm ci --omit=dev
COPY renderer/src ./src
COPY ["agents/Graphics designer agent/Causten Font Family/", "./fonts/"]
ENV FONTS_DIR=/srv/fonts
EXPOSE 8090
CMD ["node", "src/server.js"]
