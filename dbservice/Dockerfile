FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --production

COPY index.js ./
COPY lib/ ./lib/

RUN mkdir -p /app/db

ENV DBSERVICE_PORT=40002
ENV DBSERVICE_SAVE_PATH=/app/db
ENV NODE_ENV=production

EXPOSE 40002

CMD ["node", "index.js"]
