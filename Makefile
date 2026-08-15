.PHONY: up down logs ps build seed seed-gen demo-reset psql test frontend-dev config

up:            ## start the whole stack
	docker compose up -d

down:          ## stop the stack
	docker compose down

logs:          ## tail logs
	docker compose logs -f

ps:            ## service status
	docker compose ps

build:         ## build all images
	docker compose build

seed:          ## load schema + demo data into an EMPTY db (fresh volumes self-seed)
	docker compose exec -T postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend} < db/schema.sql
	docker compose exec -T postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend} < db/seed/seed.sql

demo-reset:    ## return the demo patient (1737) to a clean pre-demo state
	@# Review decisions are durable by design, so every rehearsal and every
	@# integration run consumes demo state. Run this before each rehearsal.
	@# Does not re-seed: the chart, the A1c trend and drkim come from seed.sql.
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
