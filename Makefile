PREFIX ?= $(HOME)/.local/bin

install:
	mkdir -p $(PREFIX)
	cp claude-swap $(PREFIX)/claude-swap
	chmod +x $(PREFIX)/claude-swap
	@echo ""
	@echo "Installed to $(PREFIX)/claude-swap"
	@echo "Run 'claude-swap init' to set up your accounts."

uninstall:
	rm -f $(PREFIX)/claude-swap
	@echo "Removed $(PREFIX)/claude-swap"
	@echo "Note: ~/.claude/claude-swap.json was preserved"

.PHONY: install uninstall
