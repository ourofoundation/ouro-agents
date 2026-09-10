# Agent sandbox images.
#
#   make              base + every overlay
#   make base         shared science stack only
#   make apollo       base, then the apollo overlay
#   make hermes
#   make list         local sandbox images
#   make NO_CACHE=1   rebuild without Docker cache
#
# Overlay Dockerfiles are Dockerfile.sandbox.<name> and tag as
# ouro-agents-sandbox-<name>:latest. Add a new overlay file and `make`
# picks it up.

IMAGE_PREFIX ?= ouro-agents-sandbox
TAG ?= latest
DOCKER ?= docker

ifeq ($(NO_CACHE),1)
BUILD_FLAGS += --no-cache
endif

BASE_DOCKERFILE := Dockerfile.sandbox
BASE_IMAGE := $(IMAGE_PREFIX):$(TAG)

OVERLAY_DOCKERFILES := $(wildcard Dockerfile.sandbox.*)
OVERLAYS := $(patsubst Dockerfile.sandbox.%,%,$(OVERLAY_DOCKERFILES))

.PHONY: all base $(OVERLAYS) list help

all: base $(OVERLAYS)

base:
	$(DOCKER) build $(BUILD_FLAGS) -f $(BASE_DOCKERFILE) -t $(BASE_IMAGE) .

$(OVERLAYS): base
	$(DOCKER) build $(BUILD_FLAGS) -f Dockerfile.sandbox.$@ -t $(IMAGE_PREFIX)-$@:$(TAG) .

list:
	$(DOCKER) images '$(IMAGE_PREFIX)*'

help:
	@printf '%s\n' \
	  'Build agent sandbox images (run from the ouro-agents directory).' \
	  '' \
	  '  make              base + overlays ($(OVERLAYS))' \
	  '  make base         $(BASE_IMAGE)' \
	  '  make <overlay>    $(IMAGE_PREFIX)-<overlay>:$(TAG)' \
	  '  make list         show local sandbox images' \
	  '' \
	  'Variables:' \
	  '  NO_CACHE=1        rebuild without Docker cache' \
	  '  BUILD_FLAGS=      extra docker build flags' \
	  '  TAG=$(TAG)        image tag'
