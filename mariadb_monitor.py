#!/usr/bin/env python3
"""
mariadb_monitor.py
Polls performance_schema for slow/long-running queries and sends Slack alerts.
"""

import os
import json
import time
import logging
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    import pymysql
except ImportError:
    raise SystemExit("pymysql not installed. Run: pip3 install pymysql")

# ─── Configuration ─────────────────────────────────────────────────────────────
# Override with env vars or CLI args (see bottom of file)

DEFAULTS = {
    "db_host":          os.getenv("DB_HOST", "127.0.0.1"),
    "db_port":          int(os.getenv("DB_PORT", "3306")),
    "db_user":          os.getenv("DB_USER", "monitor"),
    "db_password":      os.getenv("DB_PASSWORD", ""),
    "db_name":          os.getenv("DB_NAME", "performance_schema"),

    "slack_webhook":    os.getenv("SLACK_WEBHOOK", ""),

    # Alert thresholds
    "slow_threshold_sec":   float(os.getenv("SLOW_THRESHOLD_SEC", "2.0")),   # avg execution time
    "long_running_sec":     float(os.getenv("LONG_RUNNING_SEC", "30.0")),    # currently running
    "min_exec_count":       int(os.getenv("MIN_EXEC_COUNT", "5")),           # ignore rarely-run queries
    "poll_interval_sec":    int(os.getenv("POLL_INTERVAL_SEC", "300")),      # 5 minutes

    # State file to track already-alerted digest hashes (avoids duplicate alerts)
    "state_file":           os.getenv("STATE_FILE", "/var/lib/mariadb-monitor/state.json"),
    "state_ttl_sec":        int(os.getenv("STATE_TTL_SEC", "3600")),         # re-alert after 1h
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mariadb-monitor")

AVG_EPSILON = 0.001


# ─── Database ──────────────────────────────────────────────────────────────────

def get_connection(cfg):
    return pymysql.connect(
        host=cfg["db_host"],
        port=cfg["db_port"],
        user=cfg["db_user"],
        password=cfg["db_password"],
        database=cfg["db_name"],
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_slow_digests(conn, threshold_sec, min_count):
    """Queries that exceed avg execution time threshold."""
    sql = """
        SELECT
            digest,
            LEFT(digest_text, 200)          AS digest_text,
            schema_name,
            count_star                       AS exec_count,
            ROUND(avg_timer_wait / 1e12, 3)  AS avg_sec,
            ROUND(min_timer_wait / 1e12, 3)  AS min_sec,
            ROUND(max_timer_wait / 1e12, 3)  AS max_sec,
            ROUND(sum_timer_wait / 1e12, 3)  AS total_sec,
            first_seen,
            last_seen,
            ROUND(sum_rows_examined / count_star) AS avg_rows_examined,
            ROUND(sum_rows_sent / count_star)     AS avg_rows_sent,
            sum_no_good_index_used + sum_no_index_used AS no_index_count
        FROM performance_schema.events_statements_summary_by_digest
        WHERE
            digest_text IS NOT NULL
            AND count_star >= %s
            AND avg_timer_wait / 1e12 >= %s
            AND schema_name NOT IN ('performance_schema', 'information_schema', 'mysql', 'sys')
        ORDER BY avg_timer_wait DESC
        LIMIT 10
    """
    with conn.cursor() as cur:
        cur.execute(sql, (min_count, threshold_sec))
        return cur.fetchall()


def fetch_long_running(conn, threshold_sec):
    """Queries currently executing beyond threshold."""
    sql = """
        SELECT
            id,
            user,
            host,
            db,
            command,
            time,
            LEFT(info, 200) AS query_text
        FROM information_schema.processlist
        WHERE
            command NOT IN ('Sleep', 'Binlog Dump', 'Daemon')
            AND time >= %s
            AND info IS NOT NULL
        ORDER BY time DESC
        LIMIT 5
    """
    with conn.cursor() as cur:
        cur.execute(sql, (threshold_sec,))
        return cur.fetchall()


# ─── State (deduplication) ─────────────────────────────────────────────────────

def load_state(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


def should_alert(state, key, ttl_sec):
    """Return True if we haven't alerted for this key recently (long-running queries)."""
    now = time.time()
    last = state.get(key, 0)
    if isinstance(last, dict):
        last = last.get("alerted_at", 0)
    if now - last > ttl_sec:
        state[key] = now
        return True
    return False


def _digest_snapshot(prev):
    """Return previous snapshot dict, or None if missing / legacy timestamp-only."""
    if prev is None:
        return None
    if isinstance(prev, (int, float)):
        return None
    if isinstance(prev, dict) and "exec_count" in prev:
        return prev
    return None


def should_alert_slow_digest(state, row):
    """
    Alert only when digest stats change (new executions, higher max, or avg shift).
    Returns (should_alert, delta_info).
    """
    key = f"digest:{row['digest']}"
    prev = _digest_snapshot(state.get(key))
    now = time.time()

    if prev is None:
        state[key] = {
            "alerted_at": now,
            "exec_count": row["exec_count"],
            "avg_sec": float(row["avg_sec"]),
            "max_sec": float(row["max_sec"]),
        }
        return True, {"first_alert": True}

    exec_delta = row["exec_count"] - prev["exec_count"]
    max_increased = float(row["max_sec"]) > float(prev["max_sec"])
    avg_changed = abs(float(row["avg_sec"]) - float(prev["avg_sec"])) > AVG_EPSILON

    if exec_delta == 0 and not max_increased and not avg_changed:
        return False, None

    delta = {
        "first_alert": False,
        "exec_delta": exec_delta,
        "prev_max_sec": prev["max_sec"],
        "prev_avg_sec": prev["avg_sec"],
    }
    state[key] = {
        "alerted_at": now,
        "exec_count": row["exec_count"],
        "avg_sec": float(row["avg_sec"]),
        "max_sec": float(row["max_sec"]),
    }
    return True, delta


def format_ps_timestamp(ts):
    """Format performance_schema first_seen / last_seen for Slack."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    return str(ts)


# ─── Slack ─────────────────────────────────────────────────────────────────────

def post_slack(webhook, blocks):
    log.info("Sending Slack alert (%d blocks)", len(blocks))
    log.info(json.dumps(blocks, indent=2))
    if not webhook:
        log.warning("SLACK_WEBHOOK not set — alert not sent to Slack")
        return
    payload = json.dumps({"blocks": blocks}).encode()
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.error("Slack returned %s", resp.status)
            else:
                log.info("Slack alert sent successfully")
    except urllib.error.URLError as e:
        log.error("Slack POST failed: %s", e)


def build_startup_test_blocks(cfg):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "MariaDB Monitor Started — " + cfg["db_host"]},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "This is a startup test alert."},
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"Poll every {cfg['poll_interval_sec']}s | "
                    f"Slow ≥{cfg['slow_threshold_sec']}s | "
                    f"Long-running ≥{cfg['long_running_sec']}s | "
                    f"Min exec count {cfg['min_exec_count']} | {now}"
                ),
            }],
        },
    ]


def send_startup_test_alert(cfg):
    log.info("Sending startup test alert")
    post_slack(cfg["slack_webhook"], build_startup_test_blocks(cfg))


def _format_slow_digest_delta(delta):
    if not delta or delta.get("first_alert"):
        return None
    parts = []
    exec_delta = delta.get("exec_delta", 0)
    if exec_delta > 0:
        parts.append(f"+{exec_delta} execution{'s' if exec_delta != 1 else ''}")
    prev_max = delta.get("prev_max_sec")
    if prev_max is not None and delta.get("max_increased"):
        parts.append(f"max was {prev_max}s")
    prev_avg = delta.get("prev_avg_sec")
    if prev_avg is not None and delta.get("avg_changed"):
        parts.append(f"avg was {prev_avg}s")
    return ", ".join(parts) if parts else None


def build_slow_digest_blocks(rows, threshold_sec, host):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🐢 Slow Query Alert — " + host},
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"Slow digests (lifetime avg ≥{threshold_sec}s, alert on new activity) | {now}"
                ),
            }],
        },
    ]
    for r in rows:
        no_index = " ⚠️ *no index*" if r["no_index_count"] > 0 else ""
        delta = r.get("_delta") or {}
        if not delta.get("first_alert"):
            delta = dict(delta)
            delta["max_increased"] = (
                delta.get("prev_max_sec") is not None
                and float(r["max_sec"]) > float(delta["prev_max_sec"])
            )
            delta["avg_changed"] = (
                delta.get("prev_avg_sec") is not None
                and abs(float(r["avg_sec"]) - float(delta["prev_avg_sec"])) > AVG_EPSILON
            )
        delta_line = _format_slow_digest_delta(delta)
        last_seen = format_ps_timestamp(r.get("last_seen"))
        lines = [
            f"*Schema:* `{r['schema_name'] or 'n/a'}`{no_index}",
            f"*Query:* `{r['digest_text']}`",
            (
                f"*Lifetime avg:* {r['avg_sec']}s  *min:* {r['min_sec']}s  "
                f"*max:* {r['max_sec']}s  *total executions:* {r['exec_count']}"
            ),
        ]
        if delta_line:
            lines.append(f"*New since last alert:* {delta_line}")
        if last_seen:
            lines.append(f"*Last seen:* {last_seen}")
        lines.append(f"*Avg rows examined:* {r['avg_rows_examined']}")
        text = "\n".join(lines)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})
    return blocks


def build_long_running_blocks(rows, threshold_sec, host):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔥 Long-Running Query Alert — " + host},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Queries running >{threshold_sec}s right now | {now}"}],
        },
    ]
    for r in rows:
        text = (
            f"*PID:* `{r['id']}`  *User:* `{r['user']}@{r['host']}`  *DB:* `{r['db'] or 'n/a'}`\n"
            f"*Running for:* {r['time']}s\n"
            f"*Query:* `{r['query_text']}`"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})
    return blocks


# ─── Main loop ─────────────────────────────────────────────────────────────────

def run_once(cfg):
    state = load_state(cfg["state_file"])
    changed = False

    try:
        conn = get_connection(cfg)
    except Exception as e:
        log.error("DB connection failed: %s", e)
        return

    with conn:
        # 1. Slow digest check
        try:
            slow_rows = fetch_slow_digests(
                conn,
                cfg["slow_threshold_sec"],
                cfg["min_exec_count"],
            )
            new_slow = []
            for r in slow_rows:
                alert, delta = should_alert_slow_digest(state, r)
                if alert:
                    row = dict(r)
                    row["_delta"] = delta
                    new_slow.append(row)
            if new_slow:
                log.info("Alerting on %d slow digest(s)", len(new_slow))
                blocks = build_slow_digest_blocks(new_slow, cfg["slow_threshold_sec"], cfg["db_host"])
                post_slack(cfg["slack_webhook"], blocks)
                changed = True
        except Exception as e:
            log.error("Slow digest query failed: %s", e)

        # 2. Long-running process check
        try:
            long_rows = fetch_long_running(conn, cfg["long_running_sec"])
            new_long = [
                r for r in long_rows
                if should_alert(state, f"pid:{r['id']}:{r['time']//60}", cfg["state_ttl_sec"])
            ]
            if new_long:
                log.info("Alerting on %d long-running query/queries", len(new_long))
                blocks = build_long_running_blocks(new_long, cfg["long_running_sec"], cfg["db_host"])
                post_slack(cfg["slack_webhook"], blocks)
                changed = True
        except Exception as e:
            log.error("Long-running query failed: %s", e)

    if changed:
        save_state(cfg["state_file"], state)


def main():
    parser = argparse.ArgumentParser(description="MariaDB → Slack query monitor")
    parser.add_argument("--db-host",            default=DEFAULTS["db_host"])
    parser.add_argument("--db-port",            type=int, default=DEFAULTS["db_port"])
    parser.add_argument("--db-user",            default=DEFAULTS["db_user"])
    parser.add_argument("--db-password",        default=DEFAULTS["db_password"])
    parser.add_argument("--slack-webhook",      default=DEFAULTS["slack_webhook"])
    parser.add_argument("--slow-threshold-sec", type=float, default=DEFAULTS["slow_threshold_sec"])
    parser.add_argument("--long-running-sec",   type=float, default=DEFAULTS["long_running_sec"])
    parser.add_argument("--min-exec-count",     type=int,   default=DEFAULTS["min_exec_count"])
    parser.add_argument("--poll-interval-sec",  type=int,   default=DEFAULTS["poll_interval_sec"])
    parser.add_argument("--state-file",         default=DEFAULTS["state_file"])
    parser.add_argument("--state-ttl-sec",      type=int,   default=DEFAULTS["state_ttl_sec"])
    parser.add_argument("--once", action="store_true", help="Run once and exit (for cron)")
    args = parser.parse_args()

    cfg = {
        "db_host":          args.db_host,
        "db_port":          args.db_port,
        "db_user":          args.db_user,
        "db_password":      args.db_password,
        "db_name":          "performance_schema",
        "slack_webhook":    args.slack_webhook,
        "slow_threshold_sec": args.slow_threshold_sec,
        "long_running_sec": args.long_running_sec,
        "min_exec_count":   args.min_exec_count,
        "poll_interval_sec": args.poll_interval_sec,
        "state_file":       args.state_file,
        "state_ttl_sec":    args.state_ttl_sec,
    }

    send_startup_test_alert(cfg)

    if args.once:
        run_once(cfg)
        return

    log.info("Starting MariaDB monitor (poll every %ds)", cfg["poll_interval_sec"])
    while True:
        run_once(cfg)
        time.sleep(cfg["poll_interval_sec"])


if __name__ == "__main__":
    main()
