.PHONY: up down logs ps build seed seed-gen demo-reset psql test frontend-dev config phi-backfill

up:            ## start the whole stack
	docker compose up -d
	@# w8-planner-2 P2 (adr/0012), review round 1 fix (B1): postgres's own
	@# docker-entrypoint-initdb.d self-seeds db/seed/seed.sql verbatim —
	@# plain SQL, no way to reach libs/phi_crypto from inside that init
	@# sequence — so a fresh volume's seeded PHI (including the Maria
	@# Gonzalez duplicate cluster, adr/0004) would otherwise sit plaintext
	@# with no blind index, breaking duplicate detection for every seeded
	@# row. phi-backfill is idempotent — a no-op on an already-encrypted or
	@# not-yet-existing volume — so running it unconditionally here is safe.
	@$(MAKE) phi-backfill

down:          ## stop the stack
	docker compose down

logs:          ## tail logs
	docker compose logs -f

ps:            ## service status
	docker compose ps

build:         ## build all images
	docker compose build

seed:          ## load schema + demo data into an EMPTY db (fresh volumes self-seed)
	@# schema.sql runs as the admin role (P3 role separation, w8-planner-2):
	@# CREATE OR REPLACE FUNCTION / ALTER TABLE etc. need ownership, which
	@# riverbend_app no longer has once role separation has been applied.
	docker compose exec -T postgres psql -U $${DB_ADMIN_USER:-riverbend_admin} -d $${DB_NAME:-riverbend} < db/schema.sql
	docker compose exec -T postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend} < db/seed/seed.sql
	@$(MAKE) phi-backfill

phi-backfill:  ## encrypt any plaintext patients.ssn/dob/notes and agent-draft rows (idempotent — safe to run repeatedly)
	docker compose exec -T intake-service python3 db/migrations/scripts/encrypt_existing_phi.py
	@# adr/0012 follow-up ("agent draft text encryption"): a no-op when there
	@# are no plaintext agent_draft_provenance rows, same idempotent-by-
	@# construction reasoning as the line above.
	docker compose exec -T records-service python3 db/migrations/scripts/encrypt_agent_draft_text.py

demo-reset:    ## return all four canonical demo patients (1042, 1737, 1738, 1739) to a clean pre-demo state
	@# Review decisions are durable by design, so every rehearsal and every
	@# integration run consumes demo state. Run this before each rehearsal.
	@# Does not re-seed: the charts, trends, and staff/clinician grants come from seed.sql.
	docker compose exec -T postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend} -q < db/seed/demo_reset.sql

seed-gen:      ## regenerate db/seed/seed.sql from the generator (deterministic)
	python3 db/seed/generate_seed.py > db/seed/seed.sql

psql:          ## open a psql shell
	docker compose exec postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend}

test:          ## run unit tests (no infra needed)
	pip install -r requirements-dev.txt >/dev/null
	pytest -m "not integration" -q

frontend-dev:  ## run the Next.js dev server
	cd frontend && npm install && npm run dev

config:        ## validate the compose file
	docker compose config -q && echo "compose OK"
