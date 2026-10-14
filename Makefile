# Workbench dev helpers (run on the Mac). HOST = ssh alias of a GX10 with the repo deployed.
HOST ?= gx10
.PHONY: lint manual deploy push

lint:
	bash -n install/*.sh agent/bin/support-ctl agent/bin/support-resolve-target agent/bin/onboard-target.sh
	shellcheck -S warning install/*.sh agent/bin/support-ctl agent/bin/support-resolve-target agent/bin/onboard-target.sh
	python3 -m py_compile agent/mcp/target_host.py agent/mcp/library.py agent/mcp/library_core.py agent/ui/support_ui.py tests/bench.py tests/mcp_smoke.py
	@echo lint ok

manual:
	./build/manual.sh

# Push the working tree to a device and run deploy.sh there (no git needed on the device side).
deploy:
	rsync -a --delete --exclude .git --exclude inventory --exclude __pycache__ ./ $(HOST):~/Workbench/
	ssh $(HOST) 'echo ~/Workbench | sudo tee /etc/support/workbench.path >/dev/null; ~/Workbench/install/deploy.sh'

push:
	git push origin HEAD
