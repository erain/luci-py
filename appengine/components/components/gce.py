# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

"""Wrapper around GCE REST API."""

import re

from components import net


MACHINE_TYPES = {
    'f1-micro':       {'cpus': 1,  'memory': 0.6},
    'g1-small':       {'cpus': 1,  'memory': 1.7},
    'n1-standard-1':  {'cpus': 1,  'memory': 3.75},
    'n1-standard-2':  {'cpus': 2,  'memory': 7.5},
    'n1-standard-4':  {'cpus': 4,  'memory': 15},
    'n1-standard-8':  {'cpus': 8,  'memory': 30},
    'n1-standard-16': {'cpus': 16, 'memory': 60},
    'n1-standard-32': {'cpus': 32, 'memory': 120},
    'n1-highcpu-2':   {'cpus': 2,  'memory': 1.8},
    'n1-highcpu-4':   {'cpus': 4,  'memory': 3.6},
    'n1-highcpu-8':   {'cpus': 8,  'memory': 7.2},
    'n1-highcpu-16':  {'cpus': 16, 'memory': 12.4},
    'n1-highcpu-32':  {'cpus': 32, 'memory': 28.8},
    'n1-highmem-2':   {'cpus': 2,  'memory': 13},
    'n1-highmem-4':   {'cpus': 4,  'memory': 26},
    'n1-highmem-8':   {'cpus': 8,  'memory': 52},
    'n1-highmem-16':  {'cpus': 16, 'memory': 104},
    'n1-highmem-32':  {'cpus': 32, 'memory': 208},
}


class Project(object):
  """Wrapper around GCE REST API endpoints for some project."""

  def __init__(self, project_id, service_account_key=None):
    """
    Args:
      project_id: Cloud Project ID.
      service_account_key: auth.ServiceAccountKey to use JSON service account,
          or None to use GAE app's service account.
    """
    assert is_valid_project_id(project_id), project_id
    self._project_id = project_id
    self._service_account_key = service_account_key

  @property
  def project_id(self):
    return self._project_id

  def call_api(
      self,
      endpoint,
      method='GET',
      payload=None,
      params=None,
      deadline=None,
      version='v1',
      service='compute'):
    """Sends JSON request (with retries) to GCE API endpoint.

    Args:
      endpoint: endpoint URL relative to the project URL (e.g. /regions).
      method: HTTP method to use, e.g. GET, POST, PUT.
      payload: object to serialize to JSON and put in request body.
      params: dict with query GET parameters (i.e. ?key=value&key=value).
      deadline: deadline for a single call attempt.
      version: API version to use.
      service: API service to call (compute or replicapool).

    Returns:
      Deserialized JSON response.

    Raises:
      net.Error on errors.
    """
    assert service in ('compute', 'replicapool')
    assert endpoint.startswith('/'), endpoint
    url = 'https://www.googleapis.com/%s/%s/projects/%s%s' % (
        service, version, self._project_id, endpoint)
    return net.json_request(
        url=url,
        method=method,
        payload=payload,
        params=params,
        scopes=['https://www.googleapis.com/auth/compute'],
        service_account_key=self._service_account_key,
        deadline=30 if deadline is None else deadline)

  def get_instance(self, zone, instance, fields=None):
    """Returns dict with info about an instance or None if no such instance.

    Args:
      zone: name of a zone, e.g. 'us-central1-a'.
      instance: name of an instance, e.g. 'slave123-c4'.
      fields: enumeration of dict fields to fetch (or None for all).

    Returns:
      See https://cloud.google.com/compute/docs/reference/v1/instances#resource.
    """
    assert is_valid_zone(zone), zone
    assert is_valid_instance(instance), instance
    try:
      return self.call_api(
          '/zones/%s/instances/%s' % (zone, instance),
          params={'fields': ','.join(fields)} if fields else None)
    except net.NotFoundError:  # pragma: no cover
      return None

  def yield_instances(self, instance_filter=None):
    """Yields dicts with all project instances across all zones.

    The format of the instance dict is defined here:
      https://cloud.google.com/compute/docs/reference/v1/instances#resource

    Returns instances in all possible states (instance['status'] attribute):
      PROVISIONING
      STAGING
      RUNNING
      STOPPING
      STOPPED
      TERMINATED

    Very slow call (can run for minutes). Should be used only from task queues.

    Args:
      instance_filter: optional filter to apply to instance names when scanning.
    """
    if instance_filter and set("\"\\'").intersection(instance_filter):
      raise ValueError('Invalid instance filter: %s' % instance_filter)
    page_token = None
    while True:
      params = {'maxResults': 250}
      if instance_filter:
        params['filter'] = 'name eq "%s"' % instance_filter
      if page_token:
        params['pageToken'] = page_token
      resp = self.call_api('/aggregated/instances', params=params, deadline=120)
      items = resp.get('items', {})
      for zone in sorted(items):
        for instance in items[zone].get('instances', []):
          yield instance
      page_token = resp.get('nextPageToken')
      if not page_token:
        break

  def set_metadata(self, zone, instance, fingerprint, items):
    """Initiates metadata update operation.

    Args:
      zone: name of a zone, e.g. 'us-central1-a'.
      instance: name of an instance, e.g. 'slave123-c4'.
      fingerprint: fingerprint of existing metadata.
      items: list of {'key': ..., 'value': ...} dicts with new metadata.

    Returns:
      ZoneOperation object that can be polled to wait for result.
    """
    assert is_valid_zone(zone), zone
    assert is_valid_instance(instance), instance
    op_info = self.call_api(
        endpoint='/zones/%s/instances/%s/setMetadata' % (zone, instance),
        method='POST',
        payload={
          'kind': 'compute#metadata',
          'fingerprint': fingerprint,
          'items': items,
        })
    return ZoneOperation(self, zone, op_info)

  def add_access_config(self, zone, instance, network_interface, external_ip):
    """Attaches external IP (given as IPv4 string) to an instance's NIC."""
    assert is_valid_zone(zone), zone
    assert is_valid_instance(instance), instance
    op_info = self.call_api(
        endpoint='/zones/%s/instances/%s/addAccessConfig' % (zone, instance),
        params={'networkInterface': network_interface},
        method='POST',
        payload={
          'kind': 'compute#accessConfig',
          'type': 'ONE_TO_ONE_NAT',
          'name': 'External NAT',
          'natIP': external_ip,
        })
    return ZoneOperation(self, zone, op_info)

  def list_addresses(self, region):
    """Yields dicts with reserved IPs in a region.

    Very slow call (can run for minutes). Should be used only from task queues.
    """
    assert is_valid_region(region), region
    page_token = None
    while True:
      params = {'maxResults': 250}
      if page_token:
        params['pageToken'] = page_token
      resp = self.call_api(
          '/regions/%s/addresses' % region, params=params, deadline=120)
      for addr in resp.get('items', []):
        yield addr
      page_token = resp.get('nextPageToken')
      if not page_token:
        break

  def create_instance_group_manager(self, template, size, zone):
    """Creates an instance group manager from the given template.

    Args:
     template: A dict describing a GCE instance template.
     size: Number of instances the group manager should maintain.
     zone: Zone to create the instance group in.

    Returns:
      A compute#operation dict.
    """
    return self.call_api(
        '/zones/%s/instanceGroupManagers' % zone,
        method='POST',
        payload={
            'baseInstanceName': template['name'],
            'description': template['description'],
            'instanceTemplate': template['selfLink'],
            'name': template['name'],
            'targetSize': size,
        },
    )

  def get_instance_group_managers(self, zone):
    """Returns the GCE instance group managers associated with this project.

    Args:
      zone: Zone to list the instance group managers in.

    Returns:
      A dict mapping instance group manager names to
      compute#instanceGroupManager dicts.
    """
    response = self.call_api('/zones/%s/instanceGroupManagers' % zone)
    return {manager['name']: manager for manager in response.get('items', [])}

  def get_instance_templates(self):
    """Returns the GCE instance templates associated with this project.

    Returns:
      A dict mapping instance template names to compute#instanceTemplate dicts.
    """
    response = self.call_api('/global/instanceTemplates')
    return {
        template['name']: template for template in response.get('items', [])
    }

  def get_managed_instances(self, manager, zone):
    """Returns the GCE instances managed by the given instance group manager.

    Args:
      manager: Name of the instance group manager.
      zone: Zone to list the managed instances in.

    Returns:
      A dict mapping instance names to dicts describing those managed instances.
    """
    response = self.call_api(
        '/zones/%s/instanceGroupManagers/%s/listManagedInstances' % (
            zone,
            manager,
        ),
        method='POST',
    )
    return {
        # Extract instance name from a link to the instance.
        instance['instance'].split('/')[-1]: instance
        for instance in response.get('managedInstances', [])
    }


class ZoneOperation(object):
  """Asynchronous GCE operation returned by some Project methods.

  Usage:
    op = project.set_metadata(...)
    while not op.poll():
      <operation is not finished yet>
    if op.error:
      <operation failed>
  """

  def __init__(self, project, zone, info):
    self._project = project
    self._zone = zone
    self._info = info

  def poll(self):
    """Refetches operation status, returns True if operation is done."""
    if not self.done:
      self._info = self._project.call_api(
          '/zones/%s/operations/%s' % (self._zone, self._info['name']))
    return self.done

  @property
  def done(self):
    """True when operation completes (successfully or not)."""
    return self._info['status'] == 'DONE'

  @property
  def error(self):
    """Error message on error or None on success or if not yet done."""
    errors = self._info.get('error', {}).get('errors')
    if not errors:
      return None
    return ' '.join(err.get('message', 'unknown') for err in errors)

  def has_error_code(self, code):
    """True if this operation has a suberror with given code."""
    errors = self._info.get('error', {}).get('errors')
    return any(err.get('code') == code for err in errors)


def is_valid_project_id(project_id):
  """True if string looks like a valid Cloud Project id."""
  return re.match(r'^[a-z0-9\-]+$', project_id)


def is_valid_region(region):
  """True if string looks like a GCE region name."""
  return re.match(r'^[a-z0-9\-]+$', region)


def is_valid_zone(zone):
  """True if string looks like a GCE zone name."""
  return re.match(r'^[a-z0-9\-]+$', zone)


def is_valid_instance(instance):
  """True if string looks like a valid GCE instance name."""
  return re.match(r'^[a-z0-9\-_]+$', instance)


def get_zone_url(project_id, zone):
  """Returns full zone URL given zone name."""
  assert is_valid_project_id(project_id), project_id
  assert is_valid_zone(zone), zone
  return 'https://www.googleapis.com/compute/v1/projects/%s/zones/%s' % (
      project_id, zone)


def extract_zone(zone_url):
  """Given zone URL (as in instance['zone']) returns zone name."""
  zone = zone_url[zone_url.rfind('/')+1:]
  assert is_valid_zone(zone), zone
  return zone


def get_region_url(project_id, region):
  """Returns full region URL given region name."""
  assert is_valid_project_id(project_id), project_id
  assert is_valid_region(region), region
  return 'https://www.googleapis.com/compute/v1/projects/%s/regions/%s' % (
      project_id, region)


def extract_region(region_url):
  """Given region URL (as in address['region']) returns region name."""
  region = region_url[region_url.rfind('/')+1:]
  assert is_valid_region(region), region
  return region


def region_from_zone(zone):
  """Given a zone name returns a region: us-central1-a -> us-central1."""
  assert is_valid_zone(zone), zone
  return zone[:zone.rfind('-')]


def machine_type_to_num_cpus(machine_type):
  """Given a machine type returns its number of CPUs."""
  assert machine_type in MACHINE_TYPES, machine_type
  return MACHINE_TYPES[machine_type]['cpus']


def machine_type_to_memory(machine_type):
  """Given a machine type returns its memory in GB."""
  assert machine_type in MACHINE_TYPES, machine_type
  return MACHINE_TYPES[machine_type]['memory']