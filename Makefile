# Makefile  --  microbiome demo shortcuts
#
# Prerequisites:
#   uv       (pip install uv  or  brew install uv)
#   spawn    (brew install spore-host/tap/spawn)
#   AWS credentials configured for the aws profile
#
# Typical flow:
#   make install        install Python dependencies
#   make ami            bake the pre-installed AMI (run once, ~45 min)
#   make demo           start the live dashboard (opens browser)
#   make demo-fake      rehearse the UI with no AWS calls (DEMO_FAKE=1)
#   make teardown       clean up all AWS resources after the talk
#   make lint           ruff check + format
#   make test           run the test suite
#
# Note: there is no corpus staging step.  Workers pull HMP data directly
# from the SRA Open Data bucket (RODA) at analysis time — no copying needed.

.PHONY: install ami demo demo-fake demo-headless teardown lint lint-fix test clean

# Use uv for all Python invocations
PYTHON := uv run python
UV     := uv

install:
	$(UV) pip install -e ".[dev]"

ami:
	AWS_PROFILE=aws $(PYTHON) build_ami.py

demo:
	AWS_PROFILE=aws $(PYTHON) -m microbiome_demo.app

demo-fake:
	DEMO_FAKE=1 $(PYTHON) -m microbiome_demo.app

demo-headless:
	AWS_PROFILE=aws $(PYTHON) run_headless.py

teardown:
	AWS_PROFILE=aws $(PYTHON) teardown.py

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

lint-fix:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

test:
	$(UV) run pytest tests/ -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
