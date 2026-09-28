#!/usr/bin/python
"""ravendb_backup -- Ansible module that triggers and restores RavenDB backups."""

import json
import os
import random
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request
from ansible.module_utils.polling import poll_until


def log_line(progress_log, message):
    """Append a timestamped line to `progress_log` if one is configured."""
    if not progress_log:
        return
    with open(progress_log, "a") as f:
        f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), message))


def wait_for_operation(p, target, base_path, op_id, timeout, interval, progress_log):
    """Poll /operations/state for `op_id` until it reaches a terminal state; return (state, elapsed)."""
    state_path = "%s/operations/state?id=%d" % (base_path, op_id)

    log_line(progress_log,
             "op %d  STARTED waiting (target=%s, path=%s, timeout=%ds, interval=%ds)" %
             (op_id, target, state_path, timeout, interval))

    def predicate():
        log_line(progress_log, "op %d  polling..." % op_id)
        try:
            status, body = request("GET", target, p["ravendb_domain"], state_path,
                                   p["client_cert"], p["ca_cert"])
        except Exception as e:
            log_line(progress_log, "op %d  poll FAILED: %s" % (op_id, repr(e)))
            return False, {"error": repr(e)}

        if status != 200:
            log_line(progress_log, "op %d  poll HTTP %d  body=%s" %
                     (op_id, status, body[:200] if body else ""))
            return False, {"http_status": status}

        data = json.loads(body)
        op_status = data.get("Status", "?")
        progress = data.get("Progress") or {}
        log_line(progress_log, "op %d  status=%s  progress=%s" %
                 (op_id, op_status, json.dumps(progress)))

        if op_status in ("Completed", "Faulted", "Canceled"):
            return True, data
        return False, data

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    log_line(progress_log, "op %d  FINISHED (done=%s, elapsed=%.1fs)" % (op_id, done, elapsed))
    if not done:
        raise RuntimeError("operation %d did not finish within %.1fs; last state: %s" %
                           (op_id, elapsed, value))
    return value, elapsed


def get_backup_status(p, target, db, task_id):
    """Return the backup Status dict, or None when the task has never produced a backup yet."""
    status_path = "/periodic-backup/status?name=%s&taskId=%d" % (quote(db), task_id)
    status, body = request("GET", target, p["ravendb_domain"], status_path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "get_backup_status: %s/%s taskId=%d returned HTTP %d body=%s -- "
            "can't distinguish in-progress from real server failure"
            % (target, db, task_id, status, (body or b"")[:300]))
    data = json.loads(body)
    return data.get("Status") or None


def k_backup(p):
    """Create a periodic-backup task, trigger it, wait for completion, and clean up the task."""
    target = p["target"]
    db = p["db_name"]
    backup_type = p["backup_type"] or "Backup"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = p["backup_path"] or "/backups/%s-%s" % (db, timestamp)
    timeout = p["timeout"]
    interval = p["poll_interval"]
    progress_log = p["progress_log"]

    task_name = "manual-%s-%d" % (timestamp, random.randint(0, 9999))

    # 1. Create a periodic-backup task scheduled far in the future.
    task_body = {
        "Name": task_name,
        "BackupType": backup_type,
        "Disabled": False,
        "FullBackupFrequency": "0 0 1 1 *",
        "IncrementalBackupFrequency": None,
        "LocalSettings": {
            "FolderPath": backup_path,
            "Disabled": False,
        },
        "S3Settings": None,
        "AzureSettings": None,
        "GoogleCloudSettings": None,
        "FtpSettings": None,
        "GlacierSettings": None,
    }
    create_path = "/databases/%s/admin/periodic-backup" % db
    status, body = request("POST", target, p["ravendb_domain"], create_path,
                           p["client_cert"], p["ca_cert"], body=task_body)
    if status not in (200, 201):
        raise RuntimeError("create periodic-backup task failed: HTTP %d" % status)
    task_id = int(json.loads(body)["TaskId"])
    log_line(progress_log, "task created  name=%s  taskId=%d" % (task_name, task_id))

    # 2. Capture the baseline LastFullBackup (may be None for a fresh task).
    baseline = get_backup_status(p, target, db, task_id)
    baseline_lfb = (baseline or {}).get("LastFullBackup")
    log_line(progress_log, "baseline LastFullBackup=%s" % baseline_lfb)

    # 3. Trigger the backup.
    trigger_path = "/databases/%s/admin/backup/database?taskId=%d" % (db, task_id)
    status, _ = request("POST", target, p["ravendb_domain"], trigger_path,
                        p["client_cert"], p["ca_cert"])
    if status not in (200, 201):
        raise RuntimeError("trigger backup failed: HTTP %d" % status)
    log_line(progress_log, "backup TRIGGERED  taskId=%d" % task_id)

    # 4. Poll until LastFullBackup advances or the local backup records an Exception.
    def predicate():
        log_line(progress_log, "polling  taskId=%d" % task_id)
        s = get_backup_status(p, target, db, task_id)
        if s is None:
            log_line(progress_log, "status fetch returned no data")
            return False, None

        local = s.get("LocalBackup") or {}
        exception = local.get("Exception")
        if exception:
            log_line(progress_log, "FAILED  exception=%s" % exception)
            return True, s

        current_lfb = s.get("LastFullBackup")
        log_line(progress_log, "LastFullBackup=%s  node=%s  dir=%s" % (
            current_lfb, s.get("NodeTag"), local.get("BackupDirectory")))
        if current_lfb and current_lfb != baseline_lfb:
            return True, s
        return False, s

    done, last_status, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    log_line(progress_log, "FINISHED  done=%s  elapsed=%.1fs" % (done, elapsed))

    # 5. Delete the periodic-backup task (best-effort cleanup).
    cleanup_path = ("/databases/%s/admin/tasks?id=%d&type=PeriodicBackup&taskName=%s" %
                    (db, task_id, quote(task_name)))
    request("DELETE", target, p["ravendb_domain"], cleanup_path,
            p["client_cert"], p["ca_cert"])

    if not done:
        raise RuntimeError("backup did not complete within %.1fs; last status: %s" %
                           (elapsed, json.dumps(last_status)))

    local = (last_status or {}).get("LocalBackup") or {}
    exception = local.get("Exception")
    if exception:
        raise RuntimeError("backup failed: %s" % exception)

    folder = local.get("BackupDirectory") or backup_path
    duration_ms = local.get("FullBackupDurationInMs")
    return ("BACKED UP %s (%s) on responsible node %s -> %s "
            "(TaskId=%d, server reports %dms, total wall-clock %.1fs)" %
            (db, backup_type, last_status.get("NodeTag"), folder,
             task_id, duration_ms or 0, elapsed))


def k_restore(p):
    """Trigger a local restore into a new database and wait for the operation to complete."""
    target = p["target"]
    backup_path = p["backup_path"]
    new_db = p["new_db_name"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    if not backup_path or not new_db:
        raise ValueError("kind=restore requires `backup_path` and `new_db_name`")

    # 1. Trigger the restore.
    body = {
        "Type": "Local",
        "BackupLocation": backup_path,
        "DatabaseName": new_db,
        "DisableOngoingTasks": True,
        "EncryptionKey": None,
    }
    trigger_path = "/admin/restore/database"
    status, resp = request("POST", target, p["ravendb_domain"], trigger_path,
                           p["client_cert"], p["ca_cert"], body=body)
    if status not in (200, 201):
        raise RuntimeError("trigger restore failed: HTTP %d" % status)
    op_id = int(json.loads(resp)["OperationId"])

    # 2. Wait for the restore to finish (server-level operation, not DB-scoped).
    state, elapsed = wait_for_operation(p, target, "", op_id, timeout, interval, p["progress_log"])

    if state.get("Status") != "Completed":
        raise RuntimeError("restore ended with Status=%s: %s" %
                           (state.get("Status"), json.dumps(state)))

    return ("RESTORED %s -> new DB '%s' on %s "
            "(OperationId=%d, took %.1fs)" %
            (backup_path, new_db, target, op_id, elapsed))


KINDS = {
    "backup":          k_backup,
    "restore":         k_restore,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        target=dict(required=True),
        # backup
        db_name=dict(default=None),
        backup_type=dict(default=None, choices=["Backup", "Snapshot", None]),
        backup_path=dict(default=None),
        # restore
        new_db_name=dict(default=None),
        # timing (used by backup + restore)
        timeout=dict(type="int", default=300),
        poll_interval=dict(type="int", default=3),
        # optional: write per-poll progress lines here so users can tail -F it
        progress_log=dict(type="path", default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
