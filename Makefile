.PHONY: test test-all once api worker help

help:
	@echo "Keel Trader targets:"
	@echo "  make test      - run Keel core tests (tests/test_keel_*.py)"
	@echo "  (fallback: sh scripts/run_keel_tests.sh)"
	@echo "  make test-all  - run full pytest suite"
	@echo "  make once      - one paper/demo vertical cycle"
	@echo "  make api       - start keel.api.app on :8080"
	@echo "  make worker    - start sole keel.worker scheduler"

test:
	python -m pytest tests/test_keel_*.py -v

test-all:
	python -m pytest tests/ -v

once:
	python -m keel.worker --once

api:
	python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080

worker:
	python -m keel.worker
