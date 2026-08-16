# racunai.hr intermediary

Fiscal Gateway for Fiskal 2.0: public MPS/AS4 metadata plus the private canonical `/v1` API.

Feature work lands on `develop` (WSL). Production is `main` on dedicated-hel1.

## Surfaces

- Public Traefik: `/EracunMPS`, `/health`
- Private Docker network only: `/v1` (racunai-api). Not published on the public host.
- `/admin/ams` stays on the existing AMS admin path and is not under gateway JWT.

## Database

The app connects as `racunai_intermediary` to `postgis:5432/racunai_intermediary`, schema `gateway`.

```text
postgresql+psycopg://racunai_intermediary:<secret>@postgis:5432/racunai_intermediary
```

`GATEWAY_DATABASE_URL` is required. SQLite is rejected. Provision the database with [`deploy/provision_database.py`](deploy/provision_database.py) from deploy/CI — not by hand inside the production container. Then the container runs `alembic upgrade head` as the gateway role.

## Tests

```bash
pip install -r mps/requirements-dev.txt
cd mps
GATEWAY_DATABASE_URL=postgresql+psycopg://racunai_intermediary:<secret>@127.0.0.1:5432/racunai_intermediary \
  pytest tests -q
```

Host tests may use `127.0.0.1:5432` for local admin mapping. Application containers must use host `postgis`.
