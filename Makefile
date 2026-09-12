# Shared sandbox image used by `ouro-agents build-sandbox`.
#
#   make              build ouro-agents-sandbox:latest from the repo Dockerfile
#   make NO_CACHE=1   rebuild without Docker cache
#
# Installed users should prefer `ouro-agents build-sandbox`, which builds the
# same Dockerfile shipped in the wheel and tags it with the package version.
# Extra packages belong in an agent repo's Dockerfile.agent overlay.

IMAGE_PREFIX ?= ouro-agents-sandbox
TAG ?= latest
DOCKER ?= docker

ifeq ($(NO_CACHE),1)
BUILD_FLAGS += --no-cache
endif

BASE_DOCKERFILE := Dockerfile.sandbox
BASE_IMAGE := $(IMAGE_PREFIX):$(TAG)

.PHONY: all base list help

all: base

base:
	$(DOCKER) build $(BUILD_FLAGS) -f $(BASE_DOCKERFILE) -t $(BASE_IMAGE) .

list:
	$(DOCKER) images '$(IMAGE_PREFIX)*'

help:
	@printf '%s\n' \
	  'Build the shared sandbox image (run from the ouro-agents directory).' \
	  '' \
	  '  make              $(BASE_IMAGE)' \
	  '  make list         show local sandbox images' \
	  '' \
	  'Installed users: ouro-agents build-sandbox' \
	  '' \
	  'Variables:' \
	  '  NO_CACHE=1        rebuild without Docker cache' \
	  '  BUILD_FLAGS=      extra docker build flags' \
	  '  TAG=$(TAG)        image tag'
