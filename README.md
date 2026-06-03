# mariadb-queries-slack-monitor

Polls MariaDB `performance_schema` for slow queries and `information_schema.processlist` for long-running queries, then sends formatted alerts to Slack.

## What it monitors

| Alert type               | Source                                                   | Default threshold                                      |
| ------------------------ | -------------------------------------------------------- | ------------------------------------------------------ |
| **Slow query digests**   | `performance_schema.events_statements_summary_by_digest` | Lifetime avg execution time ≥ 2s, ≥ 5 total executions |
| **Long-running queries** | `information_schema.processlist`                         | Currently running ≥ 30s                                |

**Slow digest stats are lifetime cumulative** (since MariaDB server start or last `performance_schema` reset), not per poll interval. Execution counts and avg/max/min only change when the query runs again on the server.

**Slow digest deduplication** alerts only when stats change: new executions, a higher max time, or a meaningful shift in lifetime avg. Unchanged digests are not re-sent on a timer. **Long-running** alerts still use a TTL (default 1 hour) per process key so repeated notifications for the same long job are limited.

`Avg rows examined: 0` on `CALL` / stored procedures is common at the outer statement level in `performance_schema`.

## Prerequisites

- MariaDB with `performance_schema` enabled (default on MariaDB 10.x+)
- A read-only database user with access to `performance_schema` and `PROCESS`
- A Slack [Incoming Webhook](https://api.slack.com/messaging/webhooks)

### Create the monitor user

```sql
CREATE USER 'monitor'@'127.0.0.1' IDENTIFIED BY 'your_password_here';
GRANT SELECT ON performance_schema.* TO 'monitor'@'127.0.0.1';
GRANT PROCESS ON *.* TO 'monitor'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Adjust the host (`127.0.0.1`, `%`, etc.) to match where the monitor connects from.

## Configuration

Copy the example env file and edit values:

```bash
cp .env.example .env
chmod 600 .env
```

| Variable             | Default                               | Description                                                                               |
| -------------------- | ------------------------------------- | ----------------------------------------------------------------------------------------- |
| `DB_HOST`            | `127.0.0.1`                           | MariaDB host                                                                              |
| `DB_PORT`            | `3306`                                | MariaDB port                                                                              |
| `DB_USER`            | `monitor`                             | Database user                                                                             |
| `DB_PASSWORD`        | —                                     | Database password                                                                         |
| `SLACK_WEBHOOK`      | —                                     | Slack incoming webhook URL                                                                |
| `SLOW_THRESHOLD_SEC` | `2.0`                                 | Min avg execution time to alert (seconds)                                                 |
| `LONG_RUNNING_SEC`   | `30.0`                                | Min current runtime to alert (seconds)                                                    |
| `MIN_EXEC_COUNT`     | `5`                                   | Ignore rarely-run query digests                                                           |
| `POLL_INTERVAL_SEC`  | `300`                                 | Poll interval in loop mode (seconds)                                                      |
| `STATE_TTL_SEC`      | `3600`                                | Long-running dedup: re-alert after this many seconds (slow digests use change-only dedup) |
| `STATE_FILE`         | `/var/lib/mariadb-monitor/state.json` | Dedup state path (stores per-digest snapshots for slow alerts)                            |

If `SLACK_WEBHOOK` is unset, alerts are logged to stdout but not sent to Slack (useful for testing).

CLI flags override env vars. Run `python3 mariadb_monitor.py --help` for the full list.

## Startup

On every launch (including `--once`/cron runs), the monitor sends a **startup test alert** to Slack (when `SLACK_WEBHOOK` is set) and logs the full alert payload to stdout. Use this to confirm webhook connectivity after deploy or container restart.

## Run with Docker (recommended)

```bash
docker compose up -d --build
docker compose logs -f mariadb-monitor
```

The container uses `network_mode: host` so it can reach MariaDB on `127.0.0.1:3306`. Alert state is stored in a named Docker volume.

## Run locally

```bash
pip3 install pymysql
python3 mariadb_monitor.py
```

Run a single check and exit (e.g. for cron):

```bash
python3 mariadb_monitor.py --once
```

Example cron entry (every 5 minutes):

```cron
*/5 * * * * /usr/bin/python3 /path/to/mariadb_monitor.py --once >> /var/log/mariadb-monitor.log 2>&1
```

## Slack alert examples

**Slow query digest** — lifetime min/avg/max, total execution count, delta since last alert (when applicable), last seen time, avg rows examined, and a no-index warning when applicable.

**Long-running query** — includes PID, user, host, database, runtime, and truncated query text.
