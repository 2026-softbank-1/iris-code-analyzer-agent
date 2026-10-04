FROM node:22-slim
WORKDIR /app
COPY package.json .
RUN npm install --omit=dev
COPY index.js .
EXPOSE 3100
CMD ["node", "index.js"]
