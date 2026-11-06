EMACS ?= emacs
PYTHON ?= python3
DEPS ?= ..
LOAD_PATH = -L . -L acm -L $(DEPS)/markdown-mode -L $(DEPS)/yasnippet -L $(DEPS)/posframe

.PHONY: test test-python test-elisp integration-test

# Offline regression suites; no running language servers or package downloads.
test: test-python test-elisp

test-python:
	$(PYTHON) -m unittest test.test_source test.test_server_extensions test.test_org_babel test.test_signature_help test.test_langserver -v

test-elisp:
	$(EMACS) -Q --batch --eval '(setq load-prefer-newer t)' $(LOAD_PATH) \
	  -l test/source-tests.el -l test/completion-command-tests.el \
	  -l test/lsp-bridge-org-babel-test.el -f ert-run-tests-batch-and-exit

# The original end-to-end runner installs packages and starts language servers.
integration-test:
	$(PYTHON) -m test.test
