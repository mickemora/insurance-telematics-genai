.PHONY: install test synth deploy destroy generate scores trips local

PY ?= python3

install:
	$(PY) -m pip install -r requirements-dev.txt

test:
	$(PY) -m pytest -q

synth:
	cdk synth

deploy:
	cdk deploy --require-approval never

destroy:
	cdk destroy --force

# Invoke the deployed generator Lambda (defaults: 20 drivers, 28 days)
generate:
	$(PY) scripts/demo.py generate

# Weekly scores for every driver
scores:
	$(PY) scripts/demo.py scores

# Generate + score locally without AWS (writes to ./local_data)
local:
	$(PY) scripts/demo.py local
