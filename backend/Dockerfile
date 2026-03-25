FROM node:18-alpine


WORKDIR /usr/src/app


COPY package*.json ./
RUN npm ci --only=production


COPY . .


RUN addgroup -g 1001 -S nodejs
RUN adduser -S ozgerupt -u 1001


RUN chown -R ozgerupt:nodejs /usr/src/app
USER ozgerupt


EXPOSE 3001


HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD node healthcheck.js

# Start the app
CMD ["npm", "start"]
