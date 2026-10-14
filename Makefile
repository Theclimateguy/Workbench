# Research chat dev helpers (run on the Mac). HOST = ssh target of the GB10 box.
HOST ?= spark
.PHONY: lint deploy

lint:
	python3 -m py_compile app.py tools/*.py
	bash -n install.sh && shellcheck -S warning install.sh
	@echo lint ok

# Copy to the box and run the installer there.
deploy:
	rsync -a --delete --exclude .git --exclude __pycache__ --exclude data ./ $(HOST):~/Workbench/research/
	ssh $(HOST) '~/Workbench/research/install.sh'
