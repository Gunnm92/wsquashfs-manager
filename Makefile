# Build et publication de l'image, comme SteamBox : démon Docker joint via
# docker-socket-proxy depuis le conteneur de dev.
REGISTRY ?= registry.elfenn.eu
IMAGE    ?= wsquashfs-manager
TAG      ?= latest
DOCKER   ?= DOCKER_HOST=tcp://docker-socket-proxy:2375 DOCKER_TLS_VERIFY= DOCKER_CERT_PATH= docker
FULL_IMAGE := $(REGISTRY)/$(IMAGE):$(TAG)

.PHONY: build push test
# Builder buildx « docker-container » : --load pour récupérer l'image en
# local, --push pour la publier (comme SteamBox).
build:
	$(DOCKER) buildx build --tag $(FULL_IMAGE) --load .

push:
	$(DOCKER) buildx build --tag $(FULL_IMAGE) --push .

test:
	cd backend && .venv/bin/python -m pytest -q
