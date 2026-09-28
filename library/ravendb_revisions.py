#!/usr/bin/python
"""ravendb_revisions -- Ansible module that configures the revisions policy on a RavenDB database."""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request


def seconds_to_hms(secs):
    """Encode an integer number of seconds as Raven's 'hh:mm:ss' string."""
    secs = int(secs)
    hours = secs // 3600
    remainder = secs % 3600
    minutes = remainder // 60
    seconds = remainder % 60
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def build_simple_body(minimum_revisions):
    """Build a revisions config body with only a Default keeping `minimum_revisions`."""
    return {
        "Default": {
            "MinimumRevisionsToKeep": minimum_revisions,
            "Disabled": False,
        }
    }


def build_per_collection_body(default_keep, default_max_age_secs, collections_config):
    """Build a revisions config body with a Default plus per-collection overrides."""
    default = {
        "MinimumRevisionsToKeep": default_keep,
        "MinimumRevisionAgeToKeep": seconds_to_hms(default_max_age_secs),
        "Disabled": False,
    }

    collections = {}
    for name, cfg in collections_config.items():
        collections[name] = {
            "MinimumRevisionsToKeep": cfg["keep"],
            "MinimumRevisionAgeToKeep": seconds_to_hms(cfg["max_age_secs"]),
            "Disabled": False,
        }

    return {"Default": default, "Collections": collections}


def main():
    module = AnsibleModule(argument_spec=dict(
        target=dict(required=True),
        db_name=dict(required=True),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        minimum_revisions=dict(type="int", default=100),
        collections_config=dict(type="dict"),
        default_keep=dict(type="int", default=25),
        default_max_age_secs=dict(type="int", default=21600),
    ))
    p = module.params

    if p["collections_config"]:
        body = build_per_collection_body(
            p["default_keep"],
            p["default_max_age_secs"],
            p["collections_config"],
        )
        summary = (
            "per-collection=%s  Default.MinimumRevisionsToKeep=%d  "
            "Default.MinimumRevisionAgeToKeep=%ds" % (
                list(p["collections_config"]),
                p["default_keep"],
                p["default_max_age_secs"],
            )
        )
    else:
        body = build_simple_body(p["minimum_revisions"])
        summary = "MinimumRevisionsToKeep=%d" % p["minimum_revisions"]

    path = "/databases/%s/admin/revisions/config" % p["db_name"]

    try:
        status, _ = request(
            "POST",
            p["target"], p["ravendb_domain"], path,
            p["client_cert"], p["ca_cert"],
            body=body,
        )
    except Exception as e:
        module.fail_json(msg=str(e))

    if status not in (200, 201, 204):
        module.fail_json(msg="unexpected HTTP status %d (target=%s)"
                             % (status, p["target"]))

    module.exit_json(
        changed=True,
        msg="REVISIONS configured on %s/%s -- %s" % (
            p["target"], p["db_name"], summary),
    )


if __name__ == "__main__":
    main()
