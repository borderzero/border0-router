# Variables
BINARY_NAME=webui
BIN_DIR=webui/bin
DAEMON_DIR=webui/daemon
GO=go
CGO_ENABLED=0

# Default target
.PHONY: all
all: build ## Build the binary for the current platform

# Build targets
.PHONY: build
build: ## Build binary for current platform
	cd $(DAEMON_DIR) && $(GO) build -a -ldflags '-extldflags "-static"' -o ../../$(BIN_DIR)/$(BINARY_NAME).bin main.go

.PHONY: build-arm64
build-arm64: ## Build binary for Linux ARM64
	cd $(DAEMON_DIR) && CGO_ENABLED=$(CGO_ENABLED) GOOS=linux GOARCH=arm64 $(GO) build -a -ldflags '-extldflags "-static"' -o ../../$(BIN_DIR)/$(BINARY_NAME)_aarm64.bin main.go

.PHONY: build-amd64
build-amd64: ## Build binary for Linux AMD64
	cd $(DAEMON_DIR) && CGO_ENABLED=$(CGO_ENABLED) GOOS=linux GOARCH=amd64 $(GO) build -a -ldflags '-extldflags "-static"' -o ../../$(BIN_DIR)/$(BINARY_NAME)_amd64.bin main.go

.PHONY: build-all
build-all: build-arm64 build-amd64 ## Build binaries for all platforms

# Utility targets
.PHONY: clean
clean: ## Remove built binaries
	rm -f $(BIN_DIR)/$(BINARY_NAME)*.bin

.PHONY: help
help: ## Display this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

.PHONY: build-iso
build-iso: ## Build the ISO image
	cd build && sudo -E./build_iso.sh

.PHONY: download-iso
download-iso: ## Download the ISO image
	cd build && ./download_iso.sh

