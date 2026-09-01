#!/usr/bin/env python3
"""
Reconcile queued GitHub Actions jobs against live runner VMs.

The manager creates exactly one VM per workflow_job "queued" webhook and
GitHub never redelivers it, so a VM that fails to boot or register leaves
its job queued forever. This job compares the queue against runners that
are registered or still booting and creates VMs for the shortfall. It also
deletes VMs whose runner never appeared, which the manager cannot do
because it only removes a VM on a "completed" event.

Run periodically (Cloud Scheduler -> Cloud Run job).
"""
import importlib.util
import logging
import os
import sys
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger('reconcile')

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(parent_dir, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GCloudClient = _load('gcloud_client', 'app/clients/gcloud_client.py').GCloudClient
GitHubClient = _load('github_client', 'app/clients/github_client.py').GitHubClient

API = 'https://api.github.com'
RUNNER_LABEL_PREFIX = os.environ.get('RECONCILE_LABEL_PREFIX', 'gcp-')
# A VM that has not registered a runner by now is not going to.
BOOT_GRACE_SECONDS = int(os.environ.get('RECONCILE_BOOT_GRACE_SECONDS', '600'))
# Ceiling per run, so a stuck queue cannot exhaust the instance quota.
MAX_CREATE = int(os.environ.get('RECONCILE_MAX_CREATE', '10'))
# Runners are ephemeral and deregister after one job, so a runner still
# idle this long is surplus and will otherwise wait until max-run-duration.
IDLE_GRACE_SECONDS = int(os.environ.get('RECONCILE_IDLE_GRACE_SECONDS', '1800'))
# GitHub dispatches a queued job to an available runner within seconds. One
# still queued long after that was already handed to a runner that then
# died; GitHub does not redispatch it, so provisioning more runners cannot
# help and only the workflow being re-run will clear it.
STALE_JOB_SECONDS = int(os.environ.get('RECONCILE_STALE_JOB_SECONDS', '1800'))


def _paged(url, token, key):
    """Yield items from a paginated GitHub list endpoint."""
    headers = {'Authorization': f'Bearer {token}',
               'Accept': 'application/vnd.github+json'}
    while url:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        yield from (payload[key] if key else payload)
        url = response.links.get('next', {}).get('url')


def queued_runner_jobs(token):
    """Return (repo, job_id, label) for queued jobs wanting our runners."""
    jobs = {}
    for repo in _paged(f'{API}/installation/repositories', token, 'repositories'):
        full_name = repo['full_name']
        # A run can be listed under both states, and a queued job can belong
        # to a run that is already in_progress, so collect run ids first and
        # key jobs by id to avoid provisioning a VM twice for one job.
        run_ids = set()
        for state in ('queued', 'in_progress'):
            run_ids.update(
                run['id'] for run in _paged(
                    f'{API}/repos/{full_name}/actions/runs?status={state}&per_page=100',
                    token, 'workflow_runs'))
        for run_id in run_ids:
            run_jobs = _paged(
                f'{API}/repos/{full_name}/actions/runs/{run_id}/jobs?per_page=100',
                token, 'jobs')
            for job in run_jobs:
                if job.get('status') != 'queued':
                    continue
                label = next(
                    (l for l in job.get('labels', [])
                     if l.startswith(RUNNER_LABEL_PREFIX)), None)
                if label:
                    jobs[job['id']] = (full_name, job['id'], label,
                                       job.get('started_at'))
    return list(jobs.values())


def registered_runners(token, org):
    """Return {runner_name: busy} for runners GitHub currently knows about."""
    return {
        runner['name']: runner.get('busy', False)
        for runner in _paged(f'{API}/orgs/{org}/actions/runners?per_page=100',
                             token, 'runners')
    }


def runner_instances(gcloud):
    """Return {instance_name: age_seconds} for runner VMs in the project."""
    from google.cloud import compute_v1
    request = compute_v1.ListInstancesRequest(
        project=gcloud.project_id, zone=gcloud.zone)
    now = datetime.now(timezone.utc)
    instances = {}
    for instance in gcloud.instance_client.list(request=request):
        if not instance.name.startswith('gcp-runner-'):
            continue
        created = datetime.fromisoformat(instance.creation_timestamp)
        instances[instance.name] = (now - created).total_seconds()
    return instances


def main():
    org = os.environ.get('GITHUB_ORG')
    if not org:
        logger.error('GITHUB_ORG is not set')
        return 1

    github = GitHubClient()
    gcloud = GCloudClient()
    token = github.get_installation_access_token()

    all_queued = queued_runner_jobs(token)
    now = datetime.now(timezone.utc)
    queued, stale = [], []
    for entry in all_queued:
        started = entry[3]
        age = (now - datetime.fromisoformat(started)).total_seconds() \
            if started else 0
        (stale if age >= STALE_JOB_SECONDS else queued).append(entry)

    for repo, job_id, _, _ in stale:
        logger.warning(
            'job %s in %s has been queued over %ds and will not be '
            'redispatched; re-run the workflow to clear it',
            job_id, repo, STALE_JOB_SECONDS)

    runners = registered_runners(token, org)
    instances = runner_instances(gcloud)

    idle = [name for name, busy in runners.items() if not busy]
    booting = [name for name, age in instances.items()
               if name not in runners and age < BOOT_GRACE_SECONDS]
    orphans = [name for name, age in instances.items()
               if name not in runners and age >= BOOT_GRACE_SECONDS]
    surplus = [name for name, age in instances.items()
               if name in runners and not runners[name]
               and age >= IDLE_GRACE_SECONDS]

    logger.info(
        'queued=%d stale=%d idle=%d booting=%d orphaned=%d surplus=%d '
        'registered=%d instances=%d',
        len(queued), len(stale), len(idle), len(booting), len(orphans),
        len(surplus), len(runners), len(instances))

    for name in orphans:
        logger.warning('deleting orphaned VM with no registered runner: %s', name)
        gcloud.delete_runner_instance(name)

    for name in surplus:
        logger.warning('deleting surplus VM idle for over %ds: %s',
                       IDLE_GRACE_SECONDS, name)
        gcloud.delete_runner_instance(name)

    idle = [name for name in idle if name not in surplus]

    deficit = len(queued) - len(idle) - len(booting)
    if deficit <= 0:
        logger.info('no shortfall, nothing to create')
        return 0

    creating = min(deficit, MAX_CREATE)
    if creating < deficit:
        logger.warning('shortfall is %d, creating %d this run', deficit, creating)

    url = f'https://github.com/{org}'
    for _, job_id, label, _ in queued[:creating]:
        registration_token = github.get_registration_token(org_name=org)
        name = gcloud.create_runner_instance(registration_token, url, label)
        logger.info('created %s for queued job %s (%s)', name, job_id, label)
    return 0


if __name__ == '__main__':
    sys.exit(main())
