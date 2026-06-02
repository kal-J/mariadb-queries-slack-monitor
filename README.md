# mariadb-queries-slack-monitor

Polls MariaDB `performance_schema` for slow queries and `information_schema.processlist` for long-running queries, then sends formatted alerts to Slack.

## What it monitors

| Alert type               | Source                                                   | Default threshold                       |
| ------------------------ | -------------------------------------------------------- | --------------------------------------- |
| **Slow query digests**   | `performance_schema.events_statements_summary_by_digest` | Avg execution time ≥ 2s, ≥ 5 executions |
| **Long-running queries** | `information_schema.processlist`                         | Currently running ≥ 30s                 |

Duplicate alerts are suppressed using a state file. The same digest or process is not re-alerted until the TTL expires (default: 1 hour).

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

| Variable             | Default                               | Description                               |
| -------------------- | ------------------------------------- | ----------------------------------------- |
| `DB_HOST`            | `127.0.0.1`                           | MariaDB host                              |
| `DB_PORT`            | `3306`                                | MariaDB port                              |
| `DB_USER`            | `monitor`                             | Database user                             |
| `DB_PASSWORD`        | —                                     | Database password                         |
| `SLACK_WEBHOOK`      | —                                     | Slack incoming webhook URL                |
| `SLOW_THRESHOLD_SEC` | `2.0`                                 | Min avg execution time to alert (seconds) |
| `LONG_RUNNING_SEC`   | `30.0`                                | Min current runtime to alert (seconds)    |
| `MIN_EXEC_COUNT`     | `5`                                   | Ignore rarely-run query digests           |
| `POLL_INTERVAL_SEC`  | `300`                                 | Poll interval in loop mode (seconds)      |
| `STATE_TTL_SEC`      | `3600`                                | Re-alert after this many seconds          |
| `STATE_FILE`         | `/var/lib/mariadb-monitor/state.json` | Dedup state path                          |

If `SLACK_WEBHOOK` is unset, alerts are printed to stdout as JSON (useful for testing).

CLI flags override env vars. Run `python3 mariadb_monitor.py --help` for the full list.

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

**Slow query digest** — includes schema, truncated query text, avg/max time, execution count, rows examined, and a no-index warning when applicable.

**Long-running query** — includes PID, user, host, database, runtime, and truncated query text.
