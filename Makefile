PYTHON ?= python3
VENV ?= .venv
PYTEST ?= $(VENV)/bin/python -m pytest
IMAGE ?= david-family-photos:development
BASE_IMAGE ?= python:3.13-alpine3.22
BASE_IMAGE_DIGEST ?= unverified
TRIVY ?= trivy
CANDIDATE_IMAGE ?=
CANDIDATE_METADATA ?= build/candidate-build-metadata.json
CANDIDATE_TEST_IMAGE ?= david-pi-candidate-runtime-test:local
PREVIOUS_IMAGE ?=
PROMOTION_SERVICES ?= photo-portal,chat-notifier,audiobook-preparer,slideshow-worker,david-pi-maintenance,device-backup-worker
PLATFORMS ?= linux/amd64,linux/arm64
RENAMEAT2_EXPECTED_ARCH ?=

.PHONY: bootstrap test test-fast test-media test-candidate-runtime android-release-verify image image-pinned-base publish-pinned-candidate manifest sbom \
	secret-scan vulnerability-scan verify promotion-base-evidence promotion-sbom \
	promotion-release-manifest promotion-vulnerability rollback-metadata \
	promotion-manifest promotion

bootstrap:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt pytest

test:
	$(PYTEST) -q

test-fast:
	$(PYTEST) -q --ignore=tests/test_slideshow_ffmpeg.py
	node --test tests/*.test.mjs

test-media:
	docker build --target runtime-test -t $(IMAGE)-test .
	docker run --rm --network none --read-only --user 10002:10001 --entrypoint python $(IMAGE)-test -c 'import modules.maintenance_worker'
	docker run --rm --entrypoint python \
		--tmpfs /tmp/david-pi-test:rw,size=64m,mode=0700,uid=10001,gid=10001 \
		-e PHOTO_DATA=/tmp/david-pi-test \
		-e TMPDIR=/tmp/david-pi-test \
		-e DAVID_PI_PLATFORM_DATA=/tmp/david-pi-test/platform \
		$(IMAGE)-test -m pytest -q -p no:cacheprovider tests/test_slideshow_ffmpeg.py
	docker run --rm --entrypoint python \
		--tmpfs /tmp/david-pi-test:rw,size=128m,mode=0700,uid=10001,gid=10001 \
		-e PHOTO_DATA=/tmp/david-pi-test \
		-e TMPDIR=/tmp/david-pi-test \
		-e DAVID_PI_PLATFORM_DATA=/tmp/david-pi-test/platform \
		-e DAVID_PI_EXPECT_RENAMEAT2_ARCH="$(RENAMEAT2_EXPECTED_ARCH)" \
		$(IMAGE)-test -m pytest -q -p no:cacheprovider tests/test_app.py \
		-k 'audiobook_cleanup_uses_kernel_renameat2 or audiobook_cleanup_fails_closed_without_renameat2_interface or audiobook_cleanup_syscall_collision_preserves_both_files or audiobook_unlink_preserves_staging_without_renameat2_interface or audiobook_cleanup_unsupported_platform_never_calls_raw_syscall or audiobook_cleanup_release_image_matches_expected_abi'

test-candidate-runtime:
	$(PYTHON) -c 'import sys; sys.path.insert(0,"scripts"); from release_contract import image_digest; print(image_digest(sys.argv[1]))' "$(CANDIDATE_IMAGE)"
	docker build --pull --file deploy/Dockerfile.candidate-test \
		--build-arg CANDIDATE_IMAGE="$(CANDIDATE_IMAGE)" \
		--tag "$(CANDIDATE_TEST_IMAGE)" .
	docker run --rm --entrypoint python \
		--tmpfs /tmp/david-pi-test:rw,size=64m,mode=0700,uid=10001,gid=10001 \
		-e PHOTO_DATA=/tmp/david-pi-test \
		-e TMPDIR=/tmp/david-pi-test \
		-e DAVID_PI_PLATFORM_DATA=/tmp/david-pi-test/platform \
		"$(CANDIDATE_TEST_IMAGE)" -m pytest -q -p no:cacheprovider tests/test_slideshow_ffmpeg.py
	docker run --rm --entrypoint python \
		--tmpfs /tmp/david-pi-test:rw,size=128m,mode=0700,uid=10001,gid=10001 \
		-e PHOTO_DATA=/tmp/david-pi-test \
		-e TMPDIR=/tmp/david-pi-test \
		-e DAVID_PI_PLATFORM_DATA=/tmp/david-pi-test/platform \
		-e DAVID_PI_EXPECT_RENAMEAT2_ARCH="$(RENAMEAT2_EXPECTED_ARCH)" \
		"$(CANDIDATE_TEST_IMAGE)" -m pytest -q -p no:cacheprovider tests/test_app.py \
		-k 'audiobook_cleanup_uses_kernel_renameat2 or audiobook_cleanup_fails_closed_without_renameat2_interface or audiobook_cleanup_syscall_collision_preserves_both_files or audiobook_unlink_preserves_staging_without_renameat2_interface or audiobook_cleanup_unsupported_platform_never_calls_raw_syscall or audiobook_cleanup_release_image_matches_expected_abi'

image:
	docker build --target runtime \
		--build-arg PYTHON_BASE_IMAGE="$(BASE_IMAGE)" \
		--build-arg PYTHON_BASE_DIGEST="$(BASE_IMAGE_DIGEST)" \
		--build-arg DAVID_PI_VERSION="$(IMAGE)" \
		--build-arg DAVID_PI_VCS_REF="$$(git rev-parse HEAD)" \
		-t $(IMAGE) .

image-pinned-base:
	$(PYTHON) -c 'import sys; sys.path.insert(0,"scripts"); from release_contract import image_digest; print(image_digest(sys.argv[1]))' "$(BASE_IMAGE)"
	PINNED_BASE="$(BASE_IMAGE)"; \
	docker build --target runtime \
		--build-arg PYTHON_BASE_IMAGE="$${PINNED_BASE}" \
		--build-arg PYTHON_BASE_DIGEST="$${PINNED_BASE##*@}" \
		--build-arg DAVID_PI_VERSION="$(IMAGE)" \
		--build-arg DAVID_PI_VCS_REF="$$(git rev-parse HEAD)" \
		-t $(IMAGE) .

publish-pinned-candidate: stable-release-gate
	$(PYTHON) -c 'import sys; sys.path.insert(0,"scripts"); from release_contract import image_digest; print(image_digest(sys.argv[1]))' "$(BASE_IMAGE)"
	mkdir -p "$$(dirname "$(CANDIDATE_METADATA)")"
	docker buildx build --platform $(PLATFORMS) --target runtime --pull --no-cache \
		--build-arg PYTHON_BASE_IMAGE="$(BASE_IMAGE)" \
		--build-arg PYTHON_BASE_DIGEST="$$(printf '%s' "$(BASE_IMAGE)" | sed 's/^.*@//')" \
		--build-arg DAVID_PI_VERSION="$(IMAGE)" \
		--build-arg DAVID_PI_VCS_REF="$$(git rev-parse HEAD)" \
		--metadata-file "$(CANDIDATE_METADATA)" \
		--provenance=false --tag "$(IMAGE)" --push .

sbom: image
	mkdir -p build
	docker run --rm --network none --entrypoint python $(IMAGE) -c 'import importlib.metadata as m,json; print(json.dumps(sorted([{"name":d.metadata["Name"],"version":d.version} for d in m.distributions()], key=lambda x:x["name"].lower())))' > build/python-packages.json
	docker run --rm --network none --entrypoint awk $(IMAGE) -F: \
		'$$1=="P"{p=$$2} $$1=="V"{v=$$2} $$1=="A"{a=$$2} $$0==""{if(p&&v&&a) print p "\t" v "\t" a; p=v=a=""} END{if(p&&v&&a) print p "\t" v "\t" a}' \
		/lib/apk/db/installed > build/os-packages.tsv
	$(VENV)/bin/python scripts/generate_sbom.py \
		--python-packages build/python-packages.json \
		--os-packages build/os-packages.tsv \
		--os-package-type apk \
		--image "$(IMAGE)" \
		--revision "$$(git rev-parse HEAD)" \
		--output build/david-pi.cdx.json

android-release-verify:
	$(VENV)/bin/python scripts/android_release.py verify

manifest: android-release-verify
	$(VENV)/bin/python scripts/release_manifest.py --image "$(IMAGE)" --output build/release-manifest.json

secret-scan:
	$(VENV)/bin/python scripts/check_tracked_secrets.py

vulnerability-scan: image
	$(VENV)/bin/python scripts/vulnerability_scan.py \
		--scanner "$(TRIVY)" \
		--image "$(IMAGE)" \
		--output build/vulnerability-scan.json

verify: secret-scan test-fast test-media image sbom manifest vulnerability-scan

promotion-base-evidence:
	$(VENV)/bin/python scripts/base_image_provenance.py \
		--reference "$(BASE_IMAGE)" \
		--output build/base-image-provenance.json

promotion-sbom:
	mkdir -p build
	docker run --rm --network none --entrypoint python "$(CANDIDATE_IMAGE)" \
		-c 'import importlib.metadata as m,json; print(json.dumps(sorted([{"name":d.metadata["Name"],"version":d.version} for d in m.distributions()], key=lambda x:x["name"].lower())))' > build/promotion-python-packages.json
	docker run --rm --network none --entrypoint awk "$(CANDIDATE_IMAGE)" -F: \
		'$$1=="P"{p=$$2} $$1=="V"{v=$$2} $$1=="A"{a=$$2} $$0==""{if(p&&v&&a) print p "\t" v "\t" a; p=v=a=""} END{if(p&&v&&a) print p "\t" v "\t" a}' \
		/lib/apk/db/installed > build/promotion-os-packages.tsv
	$(VENV)/bin/python scripts/generate_sbom.py \
		--python-packages build/promotion-python-packages.json \
		--os-packages build/promotion-os-packages.tsv \
		--os-package-type apk \
		--image "$(CANDIDATE_IMAGE)" \
		--revision "$$(git rev-parse HEAD)" \
		--output build/promotion.cdx.json

promotion-release-manifest: android-release-verify
	$(VENV)/bin/python scripts/release_manifest.py \
		--image "$(CANDIDATE_IMAGE)" \
		--require-image-digest \
		--output build/promotion-release-manifest.json

promotion-vulnerability:
	$(VENV)/bin/python scripts/vulnerability_scan.py \
		--scanner "$(TRIVY)" \
		--image "$(CANDIDATE_IMAGE)" \
		--output build/promotion-vulnerability.json

rollback-metadata:
	$(VENV)/bin/python scripts/rollback_metadata.py \
		--candidate-image "$(CANDIDATE_IMAGE)" \
		--previous-image "$(PREVIOUS_IMAGE)" \
		--revision "$$(git rev-parse HEAD)" \
		--services "$(PROMOTION_SERVICES)" \
		--candidate-override build/candidate-images.compose.json \
		--rollback-override build/rollback-images.compose.json \
		--legacy-readiness deploy/david-pi-legacy-writer-readiness \
		--output build/rollback-metadata.json

promotion-manifest: promotion-base-evidence promotion-sbom promotion-release-manifest promotion-vulnerability rollback-metadata
	$(VENV)/bin/python scripts/promotion_manifest.py create \
		--release-manifest build/promotion-release-manifest.json \
		--sbom build/promotion.cdx.json \
		--vulnerability-evidence build/promotion-vulnerability.json \
		--base-image-evidence build/base-image-provenance.json \
		--rollback-metadata build/rollback-metadata.json \
		--candidate-build-metadata "$(CANDIDATE_METADATA)" \
		--output build/promotion-manifest.json

promotion: promotion-manifest
	$(VENV)/bin/python scripts/promotion_manifest.py check \
		--manifest build/promotion-manifest.json

.PHONY: stable-release-gate
stable-release-gate:
	$(PYTHON) scripts/stable_release_gate.py
	$(PYTHON) scripts/android_release.py verify
