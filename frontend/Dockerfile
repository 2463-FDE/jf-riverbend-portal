FROM node:22-slim

# node:22-slim bundles its own npm CLI, which vendors its OWN copies of
# tar/sigstore/ip-address/brace-expansion/picomatch (used for npm's package
# integrity/provenance verification) - entirely separate from anything in
# this project's own package-lock.json, and invisible to `npm ls`/`npm
# audit` run against this project. Upgrading npm itself is the only fix
# (P5 follow-up, container-image findings).
RUN npm install -g npm@12.0.2

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
# Internal gateway URL must be present at build time too: Next can constant-fold
# process.env into the standalone bundle, so an unset value bakes the localhost
# fallback. Compose still overrides this at runtime.
ENV GATEWAY_URL=http://gateway:8070
RUN npm run build

EXPOSE 3070
CMD ["npm", "run", "start"]
