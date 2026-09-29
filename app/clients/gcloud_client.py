"""
Google Cloud Client for managing GCE instances.
"""
import itertools
import logging
import os
import re
import time
import uuid
import shlex
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import google.cloud.compute_v1 as compute_v1
import requests
from google.api_core import exceptions as api_exceptions

logger = logging.getLogger(__name__)

# Insert failures that mean the zone has no capacity for the machine type
# right now. The instance templates are regional, so another zone in the
# region can take the same request.
ZONE_CAPACITY_ERRORS = frozenset({
    'ZONE_RESOURCE_POOL_EXHAUSTED',
    'ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS',
})
# A zone whose latest finished insert ran out of capacity is tried last for
# this long. Older outcomes are ignored so the zone returns to its configured
# place in the order.
CAPACITY_HINT_SECONDS = int(os.environ.get('GOOGLE_CLOUD_CAPACITY_HINT_SECONDS', '1800'))
# Operations read per zone when looking for its latest finished runner insert.
CAPACITY_HINT_OPERATIONS = 25
# The capacity lookup runs before every webhook insert, inside GitHub's
# 10-second delivery timeout, so each zone gets one short request and no
# retries. The client's default is a 600-second budget.
CAPACITY_LOOKUP_TIMEOUT_SECONDS = 3
# How long a waiting create follows one zone's insert. A capacity failure
# takes 10 to 30 seconds to come back. An insert still running after this is
# left to finish on its own and counted as created. Four zones at this limit
# fit inside the reconcile job's 240-second timeout.
INSERT_TIMEOUT_SECONDS = int(os.environ.get('GOOGLE_CLOUD_INSERT_TIMEOUT_SECONDS', '45'))
# Transient failures while following an insert, retried until its deadline.
_WAIT_RETRYABLE = (
    api_exceptions.DeadlineExceeded,
    api_exceptions.ServiceUnavailable,
    api_exceptions.InternalServerError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


class ZoneCapacityError(Exception):
    """No configured zone had capacity for a runner instance."""


def _region(zone):
    return '-'.join(zone.split('-')[:-1])


def _configured_zones():
    """Return the zones from GOOGLE_CLOUD_ZONES, else GOOGLE_CLOUD_ZONE."""
    zones = [zone.strip() for zone in os.environ.get('GOOGLE_CLOUD_ZONES', '').split(',') if zone.strip()]
    return list(dict.fromkeys(zones)) or [os.environ.get('GOOGLE_CLOUD_ZONE', 'us-central1-a')]


def is_capacity_error(error):
    """Return True if an insert failed because its zone had no capacity."""
    codes = {getattr(item, 'code', None) for item in getattr(error, 'errors', None) or []}
    return bool(codes & ZONE_CAPACITY_ERRORS) or any(code in str(error) for code in ZONE_CAPACITY_ERRORS)


class GCloudClient:
    """Client for interacting with Google Cloud Compute Engine API."""

    def __init__(self):
        """Initialize GCloudClient with project and zone configuration."""
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')
        # Runners may be created in any of these zones, in this order of
        # preference. They must share one region because the instance
        # templates are regional.
        self.zones = _configured_zones()
        self.zone = self.zones[0]
        self.github_runner_group = os.environ.get('GITHUB_RUNNER_GROUP', '').strip()
        self.region = _region(self.zone)
        foreign = [zone for zone in self.zones if _region(zone) != self.region]
        if foreign:
            raise ValueError(f"GOOGLE_CLOUD_ZONES must all be in region {self.region}: {', '.join(foreign)}")

        if not self.project_id:
            logger.warning("GOOGLE_CLOUD_PROJECT not set. GCloudClient will not work correctly.")
        self._zone_operations_client = None

        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
        self.instance_client = compute_v1.InstancesClient()
        # Create a RegionInstanceTemplatesClient for retrieving templates in a specific region
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.region_instance_templates
        self.instance_templates_client = compute_v1.RegionInstanceTemplatesClient()

    def _get_template_name(self, template_name):
        """
        Find a matching instance template by name prefix.

        Args:
            template_name (str): The name prefix to search for.

        Returns:
            google.cloud.compute_v1.InstanceTemplate or None: The matching template resource.
        """
        # Replace dots with dashes for template name, so gcp-ubuntu-24.04 matches gcp-ubuntu-24-04
        prefix = template_name.replace('.', '-')
        # logger.info(f"Prefix: {prefix}")
        # Create regex pattern: prefix followed by dash, at least 12 digits, and optional alphanumeric characters
        pattern = re.compile(f"^{re.escape(prefix)}-\\d{{14,}}[a-z0-9]*$")
        try:
            # List all templates to find one that matches the pattern
            for template in self.instance_templates_client.list(project=self.project_id, region=self.region):
                # logger.info(f"Template: {template.name}")
                if pattern.match(template.name):
                    return template
            return None
        except Exception:
            return None

    def _zone_operations(self):
        if self._zone_operations_client is None:
            self._zone_operations_client = compute_v1.ZoneOperationsClient()
        return self._zone_operations_client

    def _latest_insert_ran_out_of_capacity(self, zone, now):
        """Return True if the zone's latest finished runner insert within the
        hint window failed for lack of capacity."""
        # The API rejects a list filter combined with a sort order, so this
        # reads the newest operations and filters them here.
        request = compute_v1.ListZoneOperationsRequest(
            project=self.project_id,
            zone=zone,
            order_by='creationTimestamp desc',
            max_results=CAPACITY_HINT_OPERATIONS,
        )
        cutoff = now - timedelta(seconds=CAPACITY_HINT_SECONDS)
        operations = self._zone_operations().list(
            request=request, retry=None, timeout=CAPACITY_LOOKUP_TIMEOUT_SECONDS)
        for operation in itertools.islice(operations, CAPACITY_HINT_OPERATIONS):
            if (operation.operation_type != 'insert' or not operation.end_time
                    or '/instances/gcp-runner-' not in operation.target_link):
                continue
            if datetime.fromisoformat(operation.end_time) < cutoff:
                return False
            return bool({error.code for error in operation.error.errors} & ZONE_CAPACITY_ERRORS)
        return False

    def _insert_outcome(self, zone, operation_name):
        """Follow an insert for up to INSERT_TIMEOUT_SECONDS.

        Returns:
            set or None: The insert's error codes, empty if it succeeded, or
            None if it is still running at the deadline.
        """
        deadline = time.monotonic() + INSERT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                # zoneOperations.wait returns once the operation is done or
                # after about two minutes, whichever is first.
                operation = self._zone_operations().wait(
                    project=self.project_id, zone=zone, operation=operation_name,
                    retry=None, timeout=remaining)
            except _WAIT_RETRYABLE as e:
                logger.warning("Waiting on %s in %s failed, retrying: %s", operation_name, zone, e)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            if operation.status == compute_v1.Operation.Status.DONE:
                return {error.code for error in operation.error.errors}

    def zone_order(self):
        """Return the configured zones, those that recently ran out of
        capacity moved to the end.

        Looking up recent outcomes is best effort. If it fails, the
        configured order is used as is.
        """
        if len(self.zones) < 2:
            return list(self.zones)
        now = datetime.now(timezone.utc)
        pool = ThreadPoolExecutor(max_workers=len(self.zones))
        try:
            futures = {zone: pool.submit(self._latest_insert_ran_out_of_capacity, zone, now) for zone in self.zones}
            exhausted = {zone: future.result(timeout=CAPACITY_LOOKUP_TIMEOUT_SECONDS + 1)
                         for zone, future in futures.items()}
        except Exception as e:
            logger.warning("Could not read recent zone capacity, using the configured order: %s", e)
            return list(self.zones)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return ([zone for zone in self.zones if not exhausted[zone]]
                + [zone for zone in self.zones if exhausted[zone]])

    def create_runner_instance(
        self,
        registration_token,
        repo_url,
        template_name,
        instance_label=None,
        delivery_id=None,
        wait=False,
    ):
        """
        Create a new GCE instance for a GitHub Actions runner.

        Without wait, the insert goes to the first zone in zone_order() and
        this returns once Compute Engine accepts it; a capacity failure only
        shows up later in the operation. With wait, each insert is followed
        to completion and a zone without capacity passes the request to the
        next one.

        Args:
            registration_token (str): The GitHub Actions runner registration token.
            repo_url (str): The URL of the repository or organization.
            template_name (str): The name of the instance template to use.
            instance_label (str): Label to add to the Instance for Cost Tracking.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
            wait (bool): Wait for the insert and fall back across zones.

        Returns:
            str: The name of the created instance.

        Raises:
            ZoneCapacityError: With wait, when no configured zone had capacity.
        """
        instance_template_resource = self._get_template_name(template_name)
        if instance_template_resource:
            logger.info(
                "Found matching instance template: %s, delivery_id: %s",
                instance_template_resource.name,
                delivery_id,
            )
        else:
            logger.warning(
                "No matching instance template found for label '%s' in region %s. "
                "Skipping instance creation. delivery_id: %s",
                template_name,
                self.region,
                delivery_id,
            )
            return None

        # Name must start with a lowercase letter followed by up to 62 lowercase letters,
        # numbers, or hyphens, and cannot end with a hyphen.
        instance_uuid = uuid.uuid4().hex[:16]
        if instance_template_resource.name.startswith("dependabot"):
            instance_name = f"gcp-runner-dependabot-{instance_uuid}"
        else:
            instance_name = f"gcp-runner-{instance_uuid}"

        logger.info(
            "Creating GCE instance %s with template %s, delivery_id: %s",
            instance_name,
            instance_template_resource.self_link,
            delivery_id,
        )

        # Set instance name
        instance_resource = compute_v1.Instance()  # google.cloud.compute_v1.types.Instance
        instance_resource.name = instance_name

        if instance_label is not None:
            owner, repo = instance_label.split("/")
            instance_resource.labels = {
                "gha-owner": owner.lower(),
                "gha-repo": repo.lower(),
                "gha-runner": template_name
            }

        # Set metadata (startup script) - use shlex.quote to prevent command injection
        runner_group_flag = ""
        if self.github_runner_group:
            runner_group_flag = f" --runnergroup {shlex.quote(self.github_runner_group)}"

        startup_script = (
            "cd /actions-runner && "
            f"sudo -u runner ./config.sh --url {shlex.quote(repo_url)} "
            f"--token {shlex.quote(registration_token)} "
            f"--name {shlex.quote(instance_name)} "
            f"--labels {shlex.quote(template_name)} "
            f"{runner_group_flag} "
            "--ephemeral "
            "--unattended "
            "--no-default-labels "
            "--disableupdate && "
            "sudo -u runner ./run.sh"
        )
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script),
            compute_v1.Items(key="vmDnsSetting", value="ZonalOnly"),
            compute_v1.Items(key="block-project-ssh-keys", value="true"),
        ]
        instance_resource.metadata = metadata

        zones = self.zone_order()
        for zone in zones if wait else zones[:1]:
            # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.types.InsertInstanceRequest
            request = compute_v1.InsertInstanceRequest(
                project=self.project_id,
                zone=zone,
                instance_resource=instance_resource,
                source_instance_template=instance_template_resource.self_link
            )
            try:
                # https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert
                operation = self.instance_client.insert(request=request)
            except Exception as e:
                logger.error(
                    "Failed to create instance: %s, delivery_id: %s", e, delivery_id
                )
                raise
            logger.info(
                "Instance creation operation started: %s in %s, delivery_id: %s",
                operation.name,
                zone,
                delivery_id,
            )
            if not wait:
                return instance_name
            codes = self._insert_outcome(zone, operation.name)
            if codes is None:
                logger.warning(
                    "Insert of %s in %s still running after %ds, leaving it to finish, delivery_id: %s",
                    instance_name,
                    zone,
                    INSERT_TIMEOUT_SECONDS,
                    delivery_id,
                )
                return instance_name
            if not codes:
                logger.info("Created instance %s in %s, delivery_id: %s", instance_name, zone, delivery_id)
                return instance_name
            if codes & ZONE_CAPACITY_ERRORS:
                logger.warning(
                    "%s has no capacity for %s, trying the next zone, delivery_id: %s",
                    zone,
                    instance_name,
                    delivery_id,
                )
                continue
            logger.error(
                "Insert of %s in %s failed: %s, delivery_id: %s",
                instance_name,
                zone,
                ', '.join(sorted(codes)),
                delivery_id,
            )
            raise RuntimeError(f"Insert of {instance_name} in {zone} failed: {', '.join(sorted(codes))}")
        raise ZoneCapacityError(f"No capacity for {instance_name} in {', '.join(zones)}")

    def delete_runner_instance(self, instance_name, delivery_id=None, zone=None):
        """
        Delete a GCE instance.

        Args:
            instance_name (str): The name of the instance to delete.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
            zone (str): The instance's zone. Without it, each configured zone
                is tried until one holds the instance.
        """
        logger.info(
            "Deleting GCE instance %s, delivery_id: %s", instance_name, delivery_id
        )
        for candidate in [zone] if zone else self.zones:
            try:
                operation = self.instance_client.delete(
                    project=self.project_id,
                    zone=candidate,
                    instance=instance_name
                )
            except api_exceptions.NotFound:
                continue
            except Exception as e:
                logger.error(
                    "Failed to delete instance %s: %s, delivery_id: %s",
                    instance_name,
                    e,
                    delivery_id,
                )
                raise
            logger.info(
                "Instance deletion operation started: %s, delivery_id: %s",
                operation.name,
                delivery_id,
            )
            return
        logger.warning(
            "Instance %s not found in %s, delivery_id: %s",
            instance_name,
            zone or ', '.join(self.zones),
            delivery_id,
        )
