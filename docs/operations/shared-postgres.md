# Shared PostgreSQL

The `postgres` service hosts `litellm` for production and `litellm_staging`
for staging. The gateways keep independent routing state.
They share the server, database user, capacity, and maintenance windows.

A fresh PostgreSQL volume initializes both databases. To migrate from the
previous separate staging server, follow these steps once.

1. Stop `gateway-staging` to quiesce its writes and leave its PostgreSQL running.
2. Save a custom-format `pg_dump` of `litellm_staging` outside Docker.
3. Create `litellm_staging` on the shared server and restore the dump with
   `pg_restore --exit-on-error --single-transaction`.
4. Compare application table counts before resuming writes.
5. Start the staging-profile gateway and check its database connection.
   Retain the old data volume and dump for rollback.

Keep staging running while a forwarder uses port
`4005`. Other deployments can stop it with
`docker compose --profile staging stop gateway-staging`.
