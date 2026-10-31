# Third-party notices

NetRadar-FusionCenter's own code is licensed under Apache-2.0 (see `LICENSE`).
It bundles no third-party source code. The components below are downloaded
when you build/run the stack and keep their own licences:

| Component | Source | Licence |
| :--- | :--- | :--- |
| NanoMQ broker | Docker image `emqx/nanomq:0.25.6` | MIT |
| PostgreSQL | Docker image `postgres:16-alpine` | PostgreSQL License |
| Python base image | Docker image `python:3.12-slim` | PSF License (and Debian package licences) |
| Python packages | PyPI (see `*/requirements.txt`) | their respective licences |
| Socket.IO client | `cdn.socket.io`, loaded by the dashboard page in your browser | MIT |
