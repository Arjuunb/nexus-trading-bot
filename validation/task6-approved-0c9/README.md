# Task 6 image cache verification for run 39813894000

This helper pins the already built and scanned packet for source
`0c9ff8b1f5c29f07fb11b0cd14136457e24251e4` and source fingerprint
`5eb5f4429238db93d3467ef48fec97286d6f09a4d0740826c2887a8b41d449c8`.
The packet ZIP SHA-256 is
`d3a970b68f6036d24a8c074445a6d1271eecf8c7c69b570b4618548a524acc25`
and its size is 176461808 bytes. These match GitHub artifact 12443990750.

Download `vps-image-preload.py` and the unchanged loader from
`validation/task6-approved-4ece/vps-image-preload.py` into a private temporary
directory, naming the latter `preload-engine.py`. The wrapper checks its SHA-256
before importing it. The existing loader retains the full ZIP seal, source,
scan and OCI manifest/config/rootfs identity checks and supports Docker's
classic and containerd stores. Both images are cached; the running worker
continues using its existing image.

Run without `--load` to verify only the packet. With `--load`, the helper caches
the images and runs source probes without network, credentials, state mounts
or application imports. It checks that production and staging containers are
unchanged and retains the API's 1536 MiB memory/swap limits and disk reserve.
It never runs Compose or updates services, starts trading, or accesses financial
databases. It does not grant GHCR publication approval or complete Task 6.

Financial recovery, external provider isolation, API replacement and
authenticated VPS PRD acceptance remain separate outstanding requirements.
