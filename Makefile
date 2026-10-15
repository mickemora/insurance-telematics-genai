.PHONY: install test synth deploy destroy generate scores explain eval claims eval-claims site ask verify-clean local

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

# Day 2: AI explanation for one driver (make explain DRIVER=DRV-0003)
DRIVER ?= DRV-0020
explain:
	$(PY) scripts/demo.py explain $(DRIVER)

# Day 2: explain every driver and check against the hidden ground truth
eval:
	$(PY) scripts/demo.py eval

# Day 3: claims opened by the crash workflow
claims:
	$(PY) scripts/demo.py claims

# Day 3: triage + statement cross-check against the hidden ground truth (run right after `make generate`)
eval-claims:
	$(PY) scripts/demo.py eval-claims

# Day 4: web app URL
site:
	$(PY) scripts/demo.py site

# Day 4: ask the copilot (make ask Q="Which claims have statements that don't match the data?")
Q ?= Summarize DRV-0020's last 10 trips
ask:
	$(PY) scripts/demo.py ask "$(Q)"

# After `cdk destroy`: show anything left behind
verify-clean:
	$(PY) scripts/demo.py verify-clean

# Generate + score locally without AWS (writes to ./local_data)
local:
	$(PY) scripts/demo.py local
