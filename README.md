# Docker

A [labdex](https://github.com/reykimedev/labdex) plugin. Read-only. A Docker host's containers become hosts that run on it (plan/apply), with their published ports and Traefik labels as services; container state as host status. Use a read-only socket proxy for the API.

**Version** 1.0 · **Works with** labdex >=1.3,<2 · **Plugin id** `docker`

> [!WARNING]
> Built with AI, for home labs only: labdex and its plugins were written largely with an AI
> coding assistant (Claude), directed and reviewed by a human. They haven't had an independent
> security audit and may contain security problems. Run them on a trusted home network.

## Install

It ships with labdex: **Plugins › Marketplace**, then *Install* (nothing is downloaded). Updates arrive through the official Marketplace (signed with the labdex project key).

## Settings

| Setting | Notes |
| --- | --- |
| Docker API | The Engine API, best through a read-only socket proxy (containers only) |
| Bearer token | If the proxy in front of the API wants one |
| Check the TLS certificate |  |
| The Docker host | Inventory host the containers run on. Empty: the host the API address points at |
| Containers as hosts | Each container becomes a host (type ct) running on the Docker host; off: only services on the Docker host |
| Category for new hosts |  |
| Services from published ports |  |
| Services from Traefik labels |  |
| Include stopped containers |  |
| Ignore containers | Name patterns, e.g. *-db buildx_* |

## Actions

- **Check connection** (run)
- **Import containers** (plan): Containers as hosts and their services; links existing hosts by name; never removes anything
- **Read container state** (run): Running / stopped as host status of the container hosts

## Releasing

Bump `version` in `plugin.yaml`, add a `## <version>` section to `CHANGELOG.md`, commit, then
`git tag v<version> && git push --tags`. The release workflow signs the plugin with the
`LABDEX_SIGNING_KEY` secret and attaches `docker-<version>.zip` to a GitHub release.
