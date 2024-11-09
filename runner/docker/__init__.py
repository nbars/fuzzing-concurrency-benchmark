# We need multiple version.
# - with overlayfs
# - with host bind mount
# - and both with one container for each fuzzer
# TODO: Take processes per container as arg
# 1. Gets BuildArtifactInfo
# 2. Copy root dir into docker image
# 3. Execute eval.py with same args except that we only run the plain runner (add --runners argument to eval.py)
# 4. Copy results to local dir such that they can be received by the "real" eval.py instance