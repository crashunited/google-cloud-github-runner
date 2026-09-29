import pytest
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from google.api_core.exceptions import NotFound, ServiceUnavailable
from app.clients.gcloud_client import GCloudClient, ZoneCapacityError


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for GCloud client."""
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')


@pytest.fixture
def mock_compute_clients():
    """Mock the Compute Engine clients."""
    with patch('app.clients.gcloud_client.compute_v1.InstancesClient') as mock_instances, \
         patch('app.clients.gcloud_client.compute_v1.RegionInstanceTemplatesClient') as mock_templates:
        yield mock_instances, mock_templates


@pytest.fixture
def mock_gcloud_auth():
    """Mock Google Cloud authentication to prevent credential errors."""
    with patch('google.auth.default', return_value=(MagicMock(), 'test-project')):
        yield


class TestGCloudClient:
    def test_init_with_env_vars(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with environment variables."""
        client = GCloudClient()

        assert client.project_id == 'test-project'
        assert client.zone == 'us-central1-a'
        assert client.github_runner_group == ''
        assert client.region == 'us-central1'

    def test_init_default_zone(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with default zone."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.delenv('GOOGLE_CLOUD_ZONE', raising=False)

        client = GCloudClient()

        assert client.zone == 'us-central1-a'
        assert client.region == 'us-central1'

    def test_init_with_runner_group(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with runner group."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        monkeypatch.setenv('GITHUB_RUNNER_GROUP', 'platform-runners')

        client = GCloudClient()

        assert client.github_runner_group == 'platform-runners'

    def test_init_missing_project_id(self, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with missing project ID."""
        with patch.dict('os.environ', {}, clear=True):
            client = GCloudClient()
            assert client.project_id is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance(self, mock_compute, mock_env_vars):
        """Test creating a runner instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'operation-123'
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        # Mock RegionInstanceTemplatesClient
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        instance_name = client.create_runner_instance(
            'fake-token-12345678',
            'https://github.com/owner/repo',
            'gcp-ubuntu-24.04'
        )

        assert instance_name.startswith('gcp-runner-')
        mock_instance_client.insert.assert_called_once()

        startup_script = mock_compute.Items.call_args_list[0].kwargs['value']
        assert startup_script.startswith('cd /actions-runner && ')
        assert 'sudo -u runner ./config.sh' in startup_script
        assert 'sudo -u runner ./run.sh' in startup_script
        assert '--runnergroup' not in startup_script

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_with_runner_group(self, mock_compute, monkeypatch, mock_env_vars):
        """Test creating a runner instance with runner group."""
        monkeypatch.setenv('GITHUB_RUNNER_GROUP', 'platform-runners')

        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'operation-123'
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        client.create_runner_instance(
            'fake-token-12345678',
            'https://github.com/owner/repo',
            'gcp-ubuntu-24.04'
        )

        startup_script = mock_compute.Items.call_args_list[0].kwargs['value']
        assert startup_script.startswith('cd /actions-runner && ')
        assert '--runnergroup platform-runners' in startup_script

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_error(self, mock_compute, mock_env_vars):
        """Test error handling when creating instance fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.insert.side_effect = Exception("API Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        # Mock RegionInstanceTemplatesClient
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with pytest.raises(Exception, match="API Error"):
            client.create_runner_instance(
                'fake-token',
                'https://github.com/owner/repo',
                'gcp-ubuntu-24.04'
            )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance(self, mock_compute, mock_env_vars):
        """Test deleting a runner instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'delete-operation-123'
        mock_instance_client.delete.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        client.delete_runner_instance('runner-12345')

        mock_instance_client.delete.assert_called_once_with(
            project='test-project',
            zone='us-central1-a',
            instance='runner-12345'
        )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_error(self, mock_compute, mock_env_vars):
        """Test error handling when deleting instance fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.delete.side_effect = Exception("Delete Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with pytest.raises(Exception, match="Delete Error"):
            client.delete_runner_instance('runner-12345')

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_found(self, mock_compute, mock_env_vars):
        """Test finding a template by prefix."""
        mock_templates_client = MagicMock()
        mock_template1 = MagicMock()
        mock_template1.name = 'other-template'
        mock_template2 = MagicMock()
        mock_template2.name = 'gcp-target-template-123456789012345'

        mock_templates_client.list.return_value = [mock_template1, mock_template2]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('gcp-target-template')

        assert result.name == 'gcp-target-template-123456789012345'

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_not_found(self, mock_compute, mock_env_vars):
        """Test not finding a template."""
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'other-template'

        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('non-existent')

        assert result is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_exception(self, mock_compute, mock_env_vars):
        """Test handling exception when getting template."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.side_effect = Exception("API Error")
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('any-template')

        assert result is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_no_template(self, mock_compute, mock_env_vars):
        """Test creating runner instance when no template is found."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.return_value = []
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        result = client.create_runner_instance(
            'fake-token',
            'https://github.com/owner/repo',
            'non-existent-template'
        )

        assert result is None
        mock_instance_client.insert.assert_not_called()


class TestGCloudClientDeliveryIdLogging:
    """Tests to verify that delivery_id is logged in GCloudClient methods."""

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when creating an instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = "operation-123"
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = "gcp-ubuntu-24-04-12345678901234"
        mock_template.self_link = (
            "https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/"
            "instanceTemplates/gcp-ubuntu-24-04-12345678901234"
        )
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with caplog.at_level(logging.INFO, logger="app.clients.gcloud_client"):
            client.create_runner_instance(
                "fake-token",
                "https://github.com/owner/repo",
                "gcp-ubuntu-24.04",
                delivery_id="gce-create-delivery-001",
            )

        delivery_logs = [
            r for r in caplog.records if "gce-create-delivery-001" in r.message
        ]
        assert (
            len(delivery_logs) >= 2
        ), "Expected delivery_id in at least 2 log lines (template match + creating + operation)"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_delete_runner_instance_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when deleting an instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = "delete-operation-123"
        mock_instance_client.delete.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.INFO, logger="app.clients.gcloud_client"):
            client.delete_runner_instance(
                "runner-12345", delivery_id="gce-delete-delivery-001"
            )

        delivery_logs = [
            r for r in caplog.records if "gce-delete-delivery-001" in r.message
        ]
        assert (
            len(delivery_logs) >= 2
        ), "Expected delivery_id in at least 2 log lines (deleting + operation)"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_no_template_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when no matching template found."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.return_value = []
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.WARNING, logger="app.clients.gcloud_client"):
            client.create_runner_instance(
                "fake-token",
                "https://github.com/owner/repo",
                "non-existent",
                delivery_id="gce-notemplate-delivery-001",
            )

        assert any(
            "gce-notemplate-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in warning log for missing template"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_error_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when instance creation fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.insert.side_effect = Exception("API Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = "gcp-ubuntu-24-04-12345678901234"
        mock_template.self_link = (
            "https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/"
            "instanceTemplates/gcp-ubuntu-24-04-12345678901234"
        )
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with caplog.at_level(logging.ERROR, logger="app.clients.gcloud_client"):
            with pytest.raises(Exception, match="API Error"):
                client.create_runner_instance(
                    "fake-token",
                    "https://github.com/owner/repo",
                    "gcp-ubuntu-24.04",
                    delivery_id="gce-error-delivery-001",
                )

        assert any(
            "gce-error-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in error log on instance creation failure"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_delete_runner_instance_error_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when instance deletion fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.delete.side_effect = Exception("Delete Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.ERROR, logger="app.clients.gcloud_client"):
            with pytest.raises(Exception, match="Delete Error"):
                client.delete_runner_instance(
                    "runner-12345", delivery_id="gce-delerr-delivery-001"
                )

        assert any(
            "gce-delerr-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in error log on instance deletion failure"


ZONES = 'us-central1-b,us-central1-a,us-central1-c'


def _operation(end_time, codes=(), operation_type='insert', target='gcp-runner-1'):
    operation = MagicMock()
    operation.operation_type = operation_type
    operation.end_time = end_time
    operation.target_link = f'https://www.googleapis.com/compute/v1/projects/p/zones/z/instances/{target}'
    operation.error.errors = [MagicMock(code=code) for code in codes]
    return operation


def _minutes_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class TestZones:
    @pytest.fixture
    def zone_env(self, monkeypatch):
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONES', ZONES)

    @pytest.fixture
    def compute(self, zone_env):
        with patch('app.clients.gcloud_client.compute_v1') as mock_compute:
            template = MagicMock()
            template.name = 'gcp-ubuntu-24-04-12345678901234'
            template.self_link = 'projects/test-project/regions/us-central1/instanceTemplates/' + template.name
            mock_compute.RegionInstanceTemplatesClient.return_value.list.return_value = [template]
            mock_compute.InsertInstanceRequest.side_effect = lambda **kwargs: kwargs
            yield mock_compute

    def _operations(self, compute, by_zone):
        compute.ZoneOperationsClient.return_value.list.side_effect = \
            lambda request, **kwargs: by_zone.get(request.zone, [])
        compute.ListZoneOperationsRequest.side_effect = lambda **kwargs: MagicMock(**kwargs)

    def _outcomes(self, compute, *codes):
        """Make zoneOperations.wait report each insert done with these error codes, in order."""
        done = []
        for errors in codes:
            operation = MagicMock()
            operation.status = compute.Operation.Status.DONE
            operation.error.errors = [MagicMock(code=code) for code in errors]
            done.append(operation)
        compute.ZoneOperationsClient.return_value.wait.side_effect = done

    def _inserted_zones(self, compute):
        return [c.kwargs['request']['zone'] for c in compute.InstancesClient.return_value.insert.call_args_list]

    def test_zones_come_from_the_zone_list(self, compute):
        client = GCloudClient()

        assert client.zones == ['us-central1-b', 'us-central1-a', 'us-central1-c']
        assert client.zone == 'us-central1-b'
        assert client.region == 'us-central1'

    def test_zones_outside_the_region_are_rejected(self, compute, monkeypatch):
        monkeypatch.setenv('GOOGLE_CLOUD_ZONES', 'us-central1-b,us-east1-b')

        with pytest.raises(ValueError, match='us-east1-b'):
            GCloudClient()

    def test_zone_order_moves_a_zone_out_of_capacity_last(self, compute):
        self._operations(compute, {
            'us-central1-b': [_operation(_minutes_ago(2), ['ZONE_RESOURCE_POOL_EXHAUSTED'])],
            'us-central1-a': [_operation(_minutes_ago(5))],
        })

        assert GCloudClient().zone_order() == ['us-central1-a', 'us-central1-c', 'us-central1-b']

    def test_zone_order_reads_the_latest_finished_insert(self, compute):
        self._operations(compute, {'us-central1-b': [
            _operation(_minutes_ago(1), operation_type='delete'),
            _operation(''),
            _operation(_minutes_ago(3)),
            _operation(_minutes_ago(4), ['ZONE_RESOURCE_POOL_EXHAUSTED']),
        ]})

        assert GCloudClient().zone_order() == ['us-central1-b', 'us-central1-a', 'us-central1-c']

    def test_zone_order_counts_only_runner_instance_inserts(self, compute):
        self._operations(compute, {'us-central1-b': [
            _operation(_minutes_ago(1), target='image-builder'),
            _operation(_minutes_ago(2), ['ZONE_RESOURCE_POOL_EXHAUSTED']),
        ]})

        assert GCloudClient().zone_order()[-1] == 'us-central1-b'

    def test_zone_order_bounds_the_lookup(self, compute):
        self._operations(compute, {})

        GCloudClient().zone_order()

        for call in compute.ZoneOperationsClient.return_value.list.call_args_list:
            assert call.kwargs['retry'] is None
            assert call.kwargs['timeout'] <= 3

    def test_zone_order_ignores_old_capacity_failures(self, compute):
        self._operations(compute, {
            'us-central1-b': [_operation(_minutes_ago(120), ['ZONE_RESOURCE_POOL_EXHAUSTED'])],
        })

        assert GCloudClient().zone_order() == ['us-central1-b', 'us-central1-a', 'us-central1-c']

    def test_zone_order_keeps_the_configured_order_when_the_lookup_fails(self, compute):
        compute.ZoneOperationsClient.return_value.list.side_effect = RuntimeError('permission denied')

        assert GCloudClient().zone_order() == ['us-central1-b', 'us-central1-a', 'us-central1-c']

    def test_waiting_create_falls_back_to_the_next_zone(self, compute):
        self._operations(compute, {})
        self._outcomes(compute, ['ZONE_RESOURCE_POOL_EXHAUSTED'], [])

        name = GCloudClient().create_runner_instance('token', 'https://github.com/org', 'gcp-ubuntu-24.04', wait=True)

        assert name.startswith('gcp-runner-')
        assert self._inserted_zones(compute) == ['us-central1-b', 'us-central1-a']
        waits = compute.ZoneOperationsClient.return_value.wait.call_args_list
        assert [c.kwargs['zone'] for c in waits] == ['us-central1-b', 'us-central1-a']
        assert all(c.kwargs['retry'] is None and c.kwargs['timeout'] <= 45 for c in waits)

    def test_waiting_create_raises_when_no_zone_has_capacity(self, compute):
        self._operations(compute, {})
        self._outcomes(compute, *[['ZONE_RESOURCE_POOL_EXHAUSTED']] * 3)

        with pytest.raises(ZoneCapacityError):
            GCloudClient().create_runner_instance('token', 'https://github.com/org', 'gcp-ubuntu-24.04', wait=True)
        assert self._inserted_zones(compute) == ['us-central1-b', 'us-central1-a', 'us-central1-c']

    def test_waiting_create_does_not_fall_back_on_other_errors(self, compute):
        self._operations(compute, {})
        self._outcomes(compute, ['QUOTA_EXCEEDED'])

        with pytest.raises(RuntimeError, match='QUOTA_EXCEEDED'):
            GCloudClient().create_runner_instance('token', 'https://github.com/org', 'gcp-ubuntu-24.04', wait=True)
        assert self._inserted_zones(compute) == ['us-central1-b']

    def test_waiting_create_retries_a_failed_wait(self, compute):
        self._operations(compute, {})
        done = MagicMock()
        done.status = compute.Operation.Status.DONE
        done.error.errors = []
        compute.ZoneOperationsClient.return_value.wait.side_effect = [ServiceUnavailable('busy'), done]

        with patch('app.clients.gcloud_client.time.sleep'):
            GCloudClient().create_runner_instance('token', 'https://github.com/org', 'gcp-ubuntu-24.04', wait=True)

        assert self._inserted_zones(compute) == ['us-central1-b']

    def test_waiting_create_leaves_a_slow_insert_to_finish(self, compute, monkeypatch):
        self._operations(compute, {})
        monkeypatch.setattr('app.clients.gcloud_client.INSERT_TIMEOUT_SECONDS', 0)

        name = GCloudClient().create_runner_instance('token', 'https://github.com/org', 'gcp-ubuntu-24.04', wait=True)

        assert name.startswith('gcp-runner-')
        assert self._inserted_zones(compute) == ['us-central1-b']

    def test_create_without_wait_uses_the_first_zone_in_order(self, compute):
        self._operations(compute, {
            'us-central1-b': [_operation(_minutes_ago(2), ['ZONE_RESOURCE_POOL_EXHAUSTED'])],
        })

        GCloudClient().create_runner_instance('token', 'https://github.com/org', 'gcp-ubuntu-24.04')

        assert self._inserted_zones(compute) == ['us-central1-a']
        compute.ZoneOperationsClient.return_value.wait.assert_not_called()

    def test_delete_finds_the_instance_zone(self, compute):
        instances = compute.InstancesClient.return_value
        instances.delete.side_effect = [NotFound('missing'), MagicMock()]

        GCloudClient().delete_runner_instance('gcp-runner-1')

        assert [c.kwargs['zone'] for c in instances.delete.call_args_list] == ['us-central1-b', 'us-central1-a']

    def test_delete_uses_a_known_zone(self, compute):
        instances = compute.InstancesClient.return_value

        GCloudClient().delete_runner_instance('gcp-runner-1', zone='us-central1-c')

        instances.delete.assert_called_once_with(project='test-project', zone='us-central1-c', instance='gcp-runner-1')

    def test_delete_of_a_missing_instance_logs_and_returns(self, compute, caplog):
        compute.InstancesClient.return_value.delete.side_effect = NotFound('missing')

        with caplog.at_level(logging.WARNING, logger='app.clients.gcloud_client'):
            GCloudClient().delete_runner_instance('gcp-runner-1')

        assert any('not found' in r.message for r in caplog.records)
