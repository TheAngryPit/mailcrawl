.DEFAULT_GOAL := help

.PHONY: help test check

help:
	@printf '%s\n' \
		'Available targets:' \
		'  help   Print available targets (default).' \
		'  test   Run Python unit tests.' \
		'  check  Run unit tests and the repo hygiene gate.'

test:
	python3 -m unittest discover -s tests -v

check: test
	scripts/hygiene.sh
