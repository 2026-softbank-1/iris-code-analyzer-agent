FROM node:24
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js ./
USER 1000
EXPOSE 3000
CMD ["node", "index.js"]
