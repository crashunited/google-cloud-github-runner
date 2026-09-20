from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import requests

import tools.reconcile as reconcile

JOB = ('org/repo', 1, 'gcp-ubuntu-latest', None)


def _response(status=200, payload=None, links=None):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {}
    response.links = links or {}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            str(status), response=response)
    return response


def _runner(online=True, busy=False, runner_id=1):
    return {'id': runner_id, 'busy': busy, 'online': online}


def _github(routes):
    """Fake _request serving canned payloads keyed by URL substring."""
    calls = []

    def fake(method, url, token, **kwargs):
        calls.append((method, url))
        for needle, payload in routes.items():
            if needle in url:
                return _response(200, payload)
        raise AssertionError(f'unexpected {method} {url}')

    fake.calls = calls
    return fake


@pytest.fixture
def session():
    with patch.object(reconcile, '_session') as factory, \
            patch.object(reconcile.time, 'sleep') as sleep:
        session = Mock()
        session.sleep = sleep
        factory.return_value = session
        yield session


class TestRequest:
    def test_sends_bearer_token(self, session):
        session.request.return_value = _response(200, {'ok': True})

        response = reconcile._request('GET', 'https://api.github.com/x', 'tok')

        assert response.json() == {'ok': True}
        headers = session.request.call_args.kwargs['headers']
        assert headers['Authorization'] == 'Bearer tok'

    def test_retries_server_error_then_succeeds(self, session):
        session.request.side_effect = [_response(502),
                                       _response(200, {'ok': True})]

        response = reconcile._request('GET', 'https://api.github.com/x', 'tok')

        assert response.json() == {'ok': True}
        assert session.request.call_count == 2
        session.sleep.assert_called_once_with(reconcile.API_BACKOFF_SECONDS)

    def test_retries_rate_limit(self, session):
        session.request.side_effect = [_response(429), _response(200)]

        reconcile._request('GET', 'https://api.github.com/x', 'tok')

        assert session.request.call_count == 2

    def test_client_error_is_not_retried(self, session):
        session.request.return_value = _response(404)

        with pytest.raises(requests.HTTPError):
            reconcile._request('GET', 'https://api.github.com/x', 'tok')

        assert session.request.call_count == 1
        session.sleep.assert_not_called()

    def test_connection_errors_exhaust_attempts_with_backoff(self, session):
        session.request.side_effect = requests.ConnectionError('refused')

        with pytest.raises(requests.ConnectionError):
            reconcile._request('GET', 'https://api.github.com/x', 'tok')

        assert session.request.call_count == reconcile.API_ATTEMPTS
        delays = [call.args[0] for call in session.sleep.call_args_list]
        assert delays == [reconcile.API_BACKOFF_SECONDS * 2 ** attempt
                          for attempt in range(reconcile.API_ATTEMPTS - 1)]

    def test_persistent_server_error_raises_last_response(self, session):
        session.request.return_value = _response(503)

        with pytest.raises(requests.HTTPError) as raised:
            reconcile._request('GET', 'https://api.github.com/x', 'tok')

        assert raised.value.response.status_code == 503
        assert session.request.call_count == reconcile.API_ATTEMPTS


class TestPaged:
    def test_follows_next_links(self, session):
        second = 'https://api.github.com/x?page=2'
        session.request.side_effect = [
            _response(200, {'items': [1, 2]}, {'next': {'url': second}}),
            _response(200, {'items': [3]}),
        ]

        items = list(reconcile._paged('https://api.github.com/x', 'tok', 'items'))

        assert items == [1, 2, 3]
        assert session.request.call_args_list[1].args[1] == second


class TestScanRepositories:
    GROUPS = {'runner_groups': [
        {'id': 3, 'name': 'gcp-runners', 'visibility': 'selected'},
        {'id': 4, 'name': 'everyone', 'visibility': 'all'},
    ]}
    INSTALLED = {'repositories': [{'full_name': 'org/a'}, {'full_name': 'org/b'}]}

    def test_selected_group_limits_scan_to_its_members(self):
        fake = _github({
            '/actions/runner-groups?': self.GROUPS,
            '/runner-groups/3/repositories': {
                'repositories': [{'full_name': 'org/a'}]},
            '/installation/repositories': self.INSTALLED,
        })
        with patch.object(reconcile, '_request', fake):
            repos = reconcile.scan_repositories('tok', 'org', 'gcp-runners')

        assert repos == ['org/a']
        assert not any('/installation/' in url for _, url in fake.calls)

    def test_group_visible_to_all_scans_installation(self):
        fake = _github({
            '/actions/runner-groups?': self.GROUPS,
            '/installation/repositories': self.INSTALLED,
        })
        with patch.object(reconcile, '_request', fake):
            repos = reconcile.scan_repositories('tok', 'org', 'everyone')

        assert repos == ['org/a', 'org/b']

    def test_unknown_group_falls_back_to_installation(self):
        fake = _github({
            '/actions/runner-groups?': self.GROUPS,
            '/installation/repositories': self.INSTALLED,
        })
        with patch.object(reconcile, '_request', fake):
            repos = reconcile.scan_repositories('tok', 'org', 'missing')

        assert repos == ['org/a', 'org/b']

    def test_no_group_configured_skips_group_lookup(self):
        fake = _github({'/installation/repositories': self.INSTALLED})
        with patch.object(reconcile, '_request', fake):
            repos = reconcile.scan_repositories('tok', 'org', '')

        assert repos == ['org/a', 'org/b']
        assert len(fake.calls) == 1


class TestScanRepository:
    def test_collects_queued_jobs_with_our_label_once_per_job(self):
        run = {'id': 7}
        fake = _github({
            'status=queued': {'workflow_runs': [run]},
            'status=in_progress': {'workflow_runs': [run]},
            '/runs/7/jobs': {'jobs': [
                {'id': 1, 'status': 'queued', 'started_at': 'T',
                 'labels': ['self-hosted', 'gcp-ubuntu-latest']},
                {'id': 2, 'status': 'queued', 'labels': ['ubuntu-latest']},
                {'id': 3, 'status': 'completed', 'labels': ['gcp-ubuntu-latest']},
            ]},
        })
        with patch.object(reconcile, '_request', fake):
            jobs = reconcile._scan_repository('tok', 'org/repo', '2026-09-13T00:00:00Z')

        assert jobs == {1: ('org/repo', 1, 'gcp-ubuntu-latest', 'T')}
        assert sum('/runs/7/jobs' in url for _, url in fake.calls) == 1

    def test_bounds_run_listing_to_the_window(self):
        fake = _github({'actions/runs?': {'workflow_runs': []}})
        with patch.object(reconcile, '_request', fake):
            reconcile._scan_repository('tok', 'org/repo', '2026-09-13T00:00:00Z')

        listings = [url for _, url in fake.calls if 'actions/runs?' in url]
        assert len(listings) == 2
        assert all('created=%3E%3D2026-09-13T00%3A00%3A00Z' in url
                   for url in listings)


class TestQueuedRunnerJobs:
    def test_merges_results_across_repositories(self):
        found = {
            'org/a': {1: ('org/a', 1, 'gcp-x', None)},
            'org/b': {2: ('org/b', 2, 'gcp-x', None)},
        }
        with patch.object(reconcile, '_scan_repository',
                          side_effect=lambda token, name, since: found[name]):
            jobs = reconcile.queued_runner_jobs('tok', ['org/a', 'org/b'], 'S')

        assert sorted(jobs) == [('org/a', 1, 'gcp-x', None),
                                ('org/b', 2, 'gcp-x', None)]

    def test_repository_failure_propagates(self):
        with patch.object(reconcile, '_scan_repository',
                          side_effect=requests.HTTPError('500')):
            with pytest.raises(requests.HTTPError):
                reconcile.queued_runner_jobs('tok', ['org/a'], 'S')


class TestSplitStale:
    def test_splits_by_time_on_queue(self):
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)

        def at(seconds_ago):
            return (now - timedelta(seconds=seconds_ago)).strftime(
                '%Y-%m-%dT%H:%M:%SZ')

        jobs = [('r', 1, 'gcp-x', at(reconcile.STALE_JOB_SECONDS)),
                ('r', 2, 'gcp-x', at(5)),
                ('r', 3, 'gcp-x', None)]

        fresh, stale = reconcile.split_stale(jobs, now)

        assert [job[1] for job in fresh] == [2, 3]
        assert [job[1] for job in stale] == [1]


class TestPlan:
    def test_queued_job_without_capacity_is_a_deficit(self):
        result = reconcile.plan([JOB], {}, {})

        assert result.deficit == 1

    def test_booting_vm_covers_a_queued_job(self):
        result = reconcile.plan([JOB], {}, {'gcp-runner-a': 30})

        assert result.booting == ['gcp-runner-a']
        assert result.deficit == 0

    def test_unregistered_vm_past_grace_is_orphaned(self):
        instances = {'gcp-runner-a': reconcile.BOOT_GRACE_SECONDS}

        result = reconcile.plan([JOB], {}, instances)

        assert result.orphans == ['gcp-runner-a']
        assert result.booting == []
        assert result.deficit == 1

    def test_offline_runner_with_young_vm_is_still_booting(self):
        runners = {'gcp-runner-a': _runner(online=False)}

        result = reconcile.plan([], runners, {'gcp-runner-a': 30})

        assert result.booting == ['gcp-runner-a']
        assert result.abandoned == []

    def test_offline_runner_without_vm_is_abandoned(self):
        runners = {'gcp-runner-a': _runner(online=False)}

        result = reconcile.plan([JOB], runners, {})

        assert result.abandoned == ['gcp-runner-a']
        assert result.idle == []
        assert result.deficit == 1

    def test_offline_runner_with_orphaned_vm_is_abandoned_too(self):
        runners = {'gcp-runner-a': _runner(online=False)}
        instances = {'gcp-runner-a': reconcile.BOOT_GRACE_SECONDS}

        result = reconcile.plan([], runners, instances)

        assert result.orphans == ['gcp-runner-a']
        assert result.abandoned == ['gcp-runner-a']

    def test_idle_online_runner_covers_a_queued_job(self):
        runners = {'gcp-runner-a': _runner()}

        result = reconcile.plan([JOB], runners, {'gcp-runner-a': 30})

        assert result.idle == ['gcp-runner-a']
        assert result.deficit == 0

    def test_busy_runner_is_not_capacity(self):
        runners = {'gcp-runner-a': _runner(busy=True)}

        result = reconcile.plan([JOB], runners, {'gcp-runner-a': 30})

        assert result.idle == []
        assert result.surplus == []
        assert result.deficit == 1

    def test_long_idle_runner_is_surplus_not_capacity(self):
        runners = {'gcp-runner-a': _runner()}
        instances = {'gcp-runner-a': reconcile.IDLE_GRACE_SECONDS}

        result = reconcile.plan([JOB], runners, instances)

        assert result.surplus == ['gcp-runner-a']
        assert result.deficit == 1

    def test_no_queue_and_spare_capacity_is_not_a_deficit(self):
        runners = {'gcp-runner-a': _runner()}

        result = reconcile.plan([], runners, {'gcp-runner-a': 30})

        assert result.deficit == -1


class TestCreateRunners:
    JOBS = [('org/repo', 1, 'gcp-x', None), ('org/repo', 2, 'gcp-x', None),
            ('org/repo', 3, 'gcp-x', None)]

    def _clients(self, create_results):
        github = Mock()
        github.get_registration_token.return_value = 'reg'
        gcloud = Mock()
        gcloud.create_runner_instance.side_effect = create_results
        return github, gcloud

    def test_creates_one_vm_per_job(self):
        github, gcloud = self._clients(['vm-1', 'vm-2', 'vm-3'])

        created = reconcile.create_runners(github, gcloud, 'org', self.JOBS)

        assert created == 3
        gcloud.create_runner_instance.assert_called_with(
            'reg', 'https://github.com/org', 'gcp-x')

    def test_stops_at_the_compute_quota(self):
        github, gcloud = self._clients(
            ['vm-1', Exception("Quota 'E2_CPUS' exceeded. Limit: 24.0"), 'vm-3'])

        created = reconcile.create_runners(github, gcloud, 'org', self.JOBS)

        assert created == 1
        assert gcloud.create_runner_instance.call_count == 2

    def test_other_errors_propagate(self):
        github, gcloud = self._clients(['vm-1', RuntimeError('permission denied')])

        with pytest.raises(RuntimeError):
            reconcile.create_runners(github, gcloud, 'org', self.JOBS)
