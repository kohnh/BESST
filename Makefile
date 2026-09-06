# Backfill window: predispatch has no archive and only ~15 days in Current (besst/nemweb.py), so the range is
# computed from today and capped there. It ends two days back, where the dispatch/p5min daily archives stop --
# the ingest thread in app.py fills the last two days from Current on first launch.
FROM := $(shell python3 -c "import datetime as d; print(d.date.today() - d.timedelta(days=11))")
TO   := $(shell python3 -c "import datetime as d; print(d.date.today() - d.timedelta(days=2))")

PORT ?= 8050

.PHONY: setup demo up up-bg down snapshot test

setup:     ## fresh clone: install deps, backfill ~10 days from nemweb (~5 min), then serve
	@command -v uv >/dev/null || { echo "uv not found -- https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	uv sync --extra dev
	@test -s data/nem.sqlite || uv run python -m besst.ingest --from $(FROM) --to $(TO)
	@$(MAKE) up

demo:      ## serve the committed snapshot, no backfill; runs on a scratch copy so the snapshot stays put
	uv sync --extra dev
	cp data/snapshot.sqlite data/demo.sqlite
	BESST_DB=data/demo.sqlite BESST_DEMO=1 uv run python app.py

up:        ## serve http://127.0.0.1:$(PORT) against data/nem.sqlite, until you close the terminal
	PORT=$(PORT) uv run python app.py

# `make up` dies with its terminal: closing the tab HUPs the foreground process group. This survives it.
# Same command as `up`, just detached -- the pid recorded is uv's, which forwards the signal to python.
up-bg:     ## serve in the background; logs to app.log, stop with `make down`
	@# Guard on the port, not on .app.pid: it also catches a foreground `make up` and a stale pidfile,
	@# and without it a second start would bind-fail into app.log while orphaning the first server.
	@# No lsof (some Linux boxes) means no guard rather than no server -- same behaviour as before.
	@if lsof -ti :$(PORT) >/dev/null 2>&1; then \
	  echo "port $(PORT) is already serving (pid $$(lsof -ti :$(PORT) | tr '\n' ' ')) -- 'make down', or: make up-bg PORT=8051"; \
	  exit 1; \
	fi
	@nohup env PORT=$(PORT) uv run python app.py > app.log 2>&1 & echo $$! > .app.pid
	@echo "http://127.0.0.1:$(PORT) -- pid $$(cat .app.pid), logs: app.log, stop: make down"

# Stops the background server AND a foreground `make up`, which leaves no pidfile -- so the port is the
# second place to look. Each candidate is checked against its own command line before being signalled: port
# 8050 is a common default, and a stray dev server of someone else's is not ours to kill.
down:      ## stop every server this repo started on $(PORT)
	@pids=`{ cat .app.pid 2>/dev/null; lsof -ti :$(PORT) 2>/dev/null; } | sort -u`; \
	got=; for p in $$pids; do \
	  case "`ps -o command= -p $$p 2>/dev/null`" in \
	    *app.py*) kill $$p 2>/dev/null && got="$$got $$p";; \
	    ""|"("*) ;; \
	    *) echo "left pid $$p alone -- not this app: `ps -o command= -p $$p | cut -c1-60`";; \
	  esac; \
	done; \
	rm -f .app.pid; \
	test -n "$$got" && echo "stopped$$got" || echo "nothing running"

snapshot:  ## re-cut data/snapshot.sqlite from the live store
	uv run python snapshot.py

test:
	uv run pytest -q
