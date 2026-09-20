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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urlencode

import requests

_STARTED = time.monotonic()

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
# Only runs created this recently are scanned. Anything older is long past
# the stale threshold, and without the bound every run a repository has ever
# left queued is fetched again on every tick.
RUN_WINDOW_SECONDS = int(os.environ.get('RECONCILE_RUN_WINDOW_SECONDS',
                                        str(7 * 24 * 3600)))
# Repositories are scanned concurrently. GitHub allows an installation 100
# concurrent requests.
SCAN_WORKERS = int(os.environ.get('RECONCILE_SCAN_WORKERS', '8'))
API_ATTEMPTS = int(os.environ.get('RECONCILE_API_ATTEMPTS', '3'))
API_BACKOFF_SECONDS = float(os.environ.get('RECONCILE_API_BACKOFF_SECONDS', '2'))

_thread_local = threading.local()


def _session():
    """Return a keep-alive HTTP session owned by the calling thread."""
    session = getattr(_thread_local, 'session', None)
    if session is None:
        session = _thread_local.session = requests.Session()
    return session


def _request(method, url, token, **kwargs):
    """Call the GitHub API, retrying transient failures with backoff."""
    headers = {'Authorization': f'Bearer {token}',
               'Accept': 'application/vnd.github+json'}
    last = None
    for attempt in range(API_ATTEMPTS):
        try:
            response = _session().request(
                method, url, headers=headers, timeout=30, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            last = requests.HTTPError(
                f'{response.status_code} from {url}', response=response)
        except (requests.ConnectionError, requests.Timeout) as error:
            last = error
        if attempt + 1 < API_ATTEMPTS:
            delay = API_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning('%s %s failed (%s); retrying in %ss',
                           method, url, last, delay)
            time.sleep(delay)
    raise last


def _paged(url, token, key):
    """Yield items from a paginated GitHub list endpoint."""
    while url:
        response = _request('GET', url, token)
        payload = response.json()
        yield from (payload[key] if key else payload)
        url = response.links.get('next', {}).get('url')


def scan_repositories(token, org, group_name):
    """Return full names of the repositories whose jobs can reach our runners.

    A runner group with selected visibility limits that to its members.
    Otherwise every repository the app installation covers qualifies.
    """
    if group_name:
        group = next(
            (g for g in _paged(
                f'{API}/orgs/{org}/actions/runner-groups?per_page=100',
                token, 'runner_groups')
             if g['name'] == group_name), None)
        if group is None:
            logger.warning('runner group %r not found in %s; scanning every '
                           'installed repository', group_name, org)
        elif group.get('visibility') == 'selected':
            return [repo['full_name'] for repo in _paged(
                f'{API}/orgs/{org}/actions/runner-groups/{group["id"]}'
                '/repositories?per_page=100', token, 'repositories')]
    return [repo['full_name'] for repo in _paged(
        f'{API}/installation/repositories', token, 'repositories')]


def _scan_repository(token, full_name, since):
    """Return {job_id: (repo, job_id, label, started_at)} for queued jobs in
    one repository that want our runners."""
    # A run can be listed under both states, and a queued job can belong
    # to a run that is already in_progress, so collect run ids first and
    # key jobs by id to avoid provisioning a VM twice for one job.
    run_ids = set()
    for state in ('queued', 'in_progress'):
        query = urlencode(
            {'status': state, 'created': f'>={since}', 'per_page': 100})
        run_ids.update(run['id'] for run in _paged(
            f'{API}/repos/{full_name}/actions/runs?{query}',
            token, 'workflow_runs'))
    jobs = {}
    for run_id in sorted(run_ids):
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
    return jobs


def queued_runner_jobs(token, repositories, since):
    """Return (repo, job_id, label, started_at) for queued jobs wanting our
    runners across the given repositories."""
    jobs = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for found in pool.map(
                lambda name: _scan_repository(token, name, since),
                repositories):
            jobs.update(found)
    return list(jobs.values())


def split_stale(jobs, now):
    """Split queued jobs into (dispatchable, stale) by time on the queue."""
    fresh, stale = [], []
    for entry in jobs:
        started = entry[3]
        age = (now - datetime.fromisoformat(started)).total_seconds() \
            if started else 0
        (stale if age >= STALE_JOB_SECONDS else fresh).append(entry)
    return fresh, stale


def registered_runners(token, org):
    """Return {runner_name: {id, busy, online}} for runners GitHub knows about."""
    return {
        runner['name']: {
            'id': runner['id'],
            'busy': runner.get('busy', False),
            'online': runner.get('status') == 'online',
        }
        for runner in _paged(f'{API}/orgs/{org}/actions/runners?per_page=100',
                             token, 'runners')
    }


def delete_runner_registration(token, org, runner_id):
    """Remove a runner registration from the organization."""
    _request('DELETE', f'{API}/orgs/{org}/actions/runners/{runner_id}', token)


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


class Plan(NamedTuple):
    idle: list
    booting: list
    orphans: list
    surplus: list
    abandoned: list
    deficit: int


def plan(queued, runners, instances):
    """Classify runners and VMs against the dispatchable queue.

    queued: jobs GitHub can still hand to a new runner.
    runners: {name: {id, busy, online}} registered with GitHub.
    instances: {name: age_seconds} of runner VMs in the project.
    """
    # Only an online runner can accept a job. An offline registration whose VM
    # is gone would otherwise be counted as spare capacity on every tick and
    # suppress provisioning by one.
    idle = [name for name, runner in runners.items()
            if runner['online'] and not runner['busy']]

    # A VM counts as usable only once its runner is online, whether that
    # registration is missing or present but disconnected.
    def unusable(name):
        return name not in runners or not runners[name]['online']

    booting = [name for name, age in instances.items()
               if unusable(name) and age < BOOT_GRACE_SECONDS]
    orphans = [name for name, age in instances.items()
               if unusable(name) and age >= BOOT_GRACE_SECONDS]
    surplus = [name for name in idle
               if instances.get(name, 0) >= IDLE_GRACE_SECONDS]
    abandoned = [name for name, runner in runners.items()
                 if not runner['online']
                 and (name not in instances or name in orphans)]
    deficit = len(queued) - (len(idle) - len(surplus)) - len(booting)
    return Plan(idle, booting, orphans, surplus, abandoned, deficit)


def main():
    org = os.environ.get('GITHUB_ORG')
    if not org:
        logger.error('GITHUB_ORG is not set')
        return 1
    group_name = os.environ.get('GITHUB_RUNNER_GROUP', '').strip()
    load_seconds = time.monotonic() - _STARTED

    github = GitHubClient()
    gcloud = GCloudClient()
    token = github.get_installation_access_token()

    now = datetime.now(timezone.utc)
    since = (now - timedelta(seconds=RUN_WINDOW_SECONDS)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    started = time.monotonic()
    repositories = scan_repositories(token, org, group_name)
    all_queued = queued_runner_jobs(token, repositories, since)
    scan_seconds = time.monotonic() - started
    queued, stale = split_stale(all_queued, now)

    for repo, job_id, _, _ in stale:
        logger.warning(
            'job %s in %s has been queued over %ds and will not be '
            'redispatched; re-run the workflow to clear it',
            job_id, repo, STALE_JOB_SECONDS)

    started = time.monotonic()
    runners = registered_runners(token, org)
    instances = runner_instances(gcloud)
    inventory_seconds = time.monotonic() - started

    result = plan(queued, runners, instances)

    logger.info(
        'queued=%d stale=%d idle=%d booting=%d orphaned=%d surplus=%d '
        'abandoned=%d registered=%d instances=%d repos=%d '
        'load=%.1fs scan=%.1fs inventory=%.1fs',
        len(queued), len(stale), len(result.idle), len(result.booting),
        len(result.orphans), len(result.surplus), len(result.abandoned),
        len(runners), len(instances), len(repositories),
        load_seconds, scan_seconds, inventory_seconds)

    for name in result.orphans:
        logger.warning('deleting orphaned VM with no registered runner: %s', name)
        gcloud.delete_runner_instance(name)

    for name in result.surplus:
        logger.warning('deleting surplus VM idle for over %ds: %s',
                       IDLE_GRACE_SECONDS, name)
        gcloud.delete_runner_instance(name)

    for name in result.abandoned:
        logger.warning('deregistering offline runner with no VM: %s', name)
        delete_runner_registration(token, org, runners[name]['id'])

    if result.deficit <= 0:
        logger.info('no shortfall, nothing to create')
        return 0

    creating = min(result.deficit, MAX_CREATE)
    if creating < result.deficit:
        logger.warning('shortfall is %d, creating %d this run',
                       result.deficit, creating)

    url = f'https://github.com/{org}'
    for _, job_id, label, _ in queued[:creating]:
        registration_token = github.get_registration_token(org_name=org)
        name = gcloud.create_runner_instance(registration_token, url, label)
        logger.info('created %s for queued job %s (%s)', name, job_id, label)
    return 0


if __name__ == '__main__':
    sys.exit(main())
