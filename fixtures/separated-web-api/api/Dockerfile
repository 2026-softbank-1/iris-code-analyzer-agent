FROM node:24
WORKDIR /app
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["node", "dist/index.js"]
