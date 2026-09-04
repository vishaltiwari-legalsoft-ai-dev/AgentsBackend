FROM node:20-bookworm-slim
# libcairo/libpango/librsvg are node-canvas's C dependencies (the Konva /render
# path). chromium is the HTML layout engine for /pdf — node-canvas cannot lay
# out HTML, and the board report is CSS grid + @media print.
#
# Fonts are NOT optional. node:20-bookworm-slim ships essentially none, and
# FONTS_DIR below is registered into node-canvas only — Chromium resolves fonts
# through fontconfig and never sees it. The board report asks for
# sans: Inter / Segoe UI / Helvetica / Arial
# serif: Fraunces / Palatino / Georgia / Times New Roman
# mono:  IBM Plex Mono / DejaVu Sans Mono / Consolas / Courier New
# liberation2 covers Arial/Times/Courier (metric-compatible) and dejavu-core
# covers DejaVu Sans Mono, so all three stacks resolve to a real, intended face
# instead of a last-resort fallback. Drop the actual Inter / Fraunces /
# IBM Plex Mono files into the fonts dir if you want the PDF to match the
# designer's browser exactly.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
    chromium fonts-liberation2 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY renderer/package*.json ./
RUN npm ci --omit=dev
COPY renderer/src ./src
COPY ["agents/Graphics designer agent/Causten Font Family/", "./fonts/"]
ENV FONTS_DIR=/srv/fonts
ENV CHROME_PATH=/usr/bin/chromium
EXPOSE 8090
CMD ["node", "src/server.js"]
