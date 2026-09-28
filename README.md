# ansible-ravendb-chaos

A small Ansible harness that spins up N Docker containers (or talks to N SSH hosts),
installs RavenDB on each, and forms them into clusters. Use it to chaos-test cluster
behaviour locally: kill a node, partition the network, restart or upgrade a service, then
assert the data held up.

<p align="center">
  <img src="assets/banner2.png" alt="ansible-ravendb-chaos" width="1600">
</p>

## Prerequisites

- Docker
- Python 3
- Ansible (>= 2.15)

Install the required collections once per controller:

```bash
ansible-galaxy collection install -r requirements.yml
```

This pulls `ravendb.ravendb`, `community.docker`, `community.general`, and `ansible.posix`.

## Certificates & license

The harness runs RavenDB secured (HTTPS + client certificate). Place your cert bundle
(`ca.crt`, `ca.key`, `server.pfx`, `client.pfx`, `client.pem`) and a `license.json` in the
`cert_dir` directory (see `inventory/group_vars/all.yml`; override `cert_dir` to point
elsewhere).

## Node naming

Every node is `<cluster_id><letter>`: `cluster_id` an integer, `letter` in `a..z`. The node
ending in `a` is bootstrapped as the cluster leader.

| cluster_id | nodes |
|---|---|
| 1 | 1a, 1b, 1c |
| 2 | 2a, 2b, 2c |

Scale with `clusters_count` and `nodes_per_cluster` (in `group_vars/all.yml`, or `-e` on the CLI).

## Bring-up - Docker

```bash
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=1 -e nodes_per_cluster=3
ansible-playbook playbooks/install_ravendb.yml
ansible-playbook playbooks/form_clusters.yml -K
```

Studio: `https://1a.hubsink.test/studio`. Teardown: `ansible-playbook playbooks/teardown_containers.yml -K`.

`-K` prompts for the sudo password (needed for `/etc/hosts` edits).

## Bring-up - SSH

```bash
cp inventory/ssh_hosts.yml.example inventory/ssh_hosts.yml   # then fill in your hosts
ansible-playbook -i inventory/ssh_hosts.yml playbooks/setup_ssh_targets.yml
ansible-playbook -i inventory/ssh_hosts.yml playbooks/install_ravendb.yml
ansible-playbook -i inventory/ssh_hosts.yml playbooks/form_clusters.yml
```

Teardown: `ansible-playbook -i inventory/ssh_hosts.yml playbooks/cleanup_ssh_targets.yml`.

## Running a toolbox tool

Each `toolbox/<group>/<tool>.yml` runs standalone and is importable from your own playbook.
Its header comment lists the inputs. Example:

```bash
ansible-playbook toolbox/writes/write_docs.yml -e target=1a -e db_name=db1 -e count=50
```

Naming convention:

- unprefixed = mutator (changes state)
- `wait_for_*` = sync gate (blocks until a condition holds, with a timeout)
- `diagnostic_*` = read-only inspection / assertion

## Tests

```bash
PYTHONPATH=. pytest tests/unit -v          # fast, mocked HTTP
PYTHONPATH=. pytest tests/integration -v   # slower; needs a reachable RavenDB
```

## Layout

```
playbooks/     bring-up + teardown (provision / install / form / add / ssh setup / cleanup)
library/       custom Ansible modules (Python) that talk to RavenDB's REST API
module_utils/  shared HTTP client + polling helpers
toolbox/       single-purpose sub-playbooks (db / writes / service / network / wait /
               diagnostic / workloads / backup / consistency_check / control)
inventory/     host + variable defaults
scripts/       build a RavenDB .deb from source; local lab teardown
tests/         unit (mocked) + integration (against real RavenDB)
```
