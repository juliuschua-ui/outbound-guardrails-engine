PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: test demo

test:
	$(PY) -m pytest -q

demo:
	bash demo/run_demo.sh
