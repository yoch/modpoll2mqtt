PROJECT_NAME ?= modpoll2mqtt

.PHONY: install
install: ## Install the poetry environment and pre-commit hooks (see .tool-versions for tool versions)
	@echo "🚀 Creating in-project virtual environment with Poetry (asdf: see .tool-versions)"
	@poetry install
	@poetry run pre-commit install --allow-missing-config
	@echo "✅ Done. Activate with: source .venv/bin/activate"

.PHONY: check
check: ## Run code quality tools.
	@echo "🚀 Checking Poetry lock file consistency with 'pyproject.toml': Running poetry check --lock"
	@poetry check --lock
	@echo "🚀 Exporting 'requirements.txt' file: Running poetry export"
	@poetry export -f requirements.txt -o requirements.txt --without-hashes
	@echo "🚀 Linting code: Running pre-commit"
	@poetry run pre-commit run -a
	@echo "🚀 Checking for obsolete dependencies: Running deptry"
	@poetry run deptry .

.PHONY: test
test: ## Run unit tests (excludes integration tests)
	@echo "🚀 Testing code: Running pytest"
	@poetry run pytest -m "not integration"

.PHONY: test-integration-modbus
test-integration-modbus: ## Run Modbus integration tests (auto-starts a local simulator on port 1502 if needed)
	@echo "🚀 Running Modbus integration tests"
	@poetry run pytest -m "integration and modbus"

.PHONY: test-integration-mqtt
test-integration-mqtt: ## Run MQTT integration tests (requires broker; default broker.emqx.io)
	@echo "🚀 Running MQTT integration tests"
	@poetry run pytest -m "integration and mqtt"

.PHONY: test-integration
test-integration: test-integration-modbus test-integration-mqtt ## Run all integration tests

.PHONY: ci
ci: check test ## Run the same quality gate as CI (check + unit tests)

.PHONY: build
build: clean-build ## Build wheel file using poetry
	@echo "🚀 Creating wheel file"
	@poetry build

.PHONY: clean-build
clean-build: ## clean build artifacts
	@rm -rf dist

.PHONY: docs-changelog
docs-changelog: ## Regenerate docs/changelog.rst from CHANGELOG.md
	@awk '/<!-- end-docs-changelog -->/{exit} \
	      /^## \[Unreleased\]$$/{skip=1; next} \
	      skip && /^## \[/ {skip=0} \
	      !skip {print}' CHANGELOG.md | poetry run pandoc --from=markdown --to=rst -o docs/changelog.rst

.PHONY: docs
docs: docs-changelog ## Build docs into html files
	@rm -rf docs/_build
	@poetry run sphinx-build docs/ docs/_build/html
	@rm -rf docs/_build/html/.doctrees docs/_build/html/_sources

.PHONY: docs-serve
docs-serve: docs-changelog ## Build and serve the docs for local dev
	@poetry run sphinx-autobuild docs/ docs/_build/html

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
