# Copyright 2014 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""High level tasks execution scheduling API.

This is the interface closest to the HTTP handlers.
"""

import datetime
import logging
import math
import random
import time

from google.appengine.ext import ndb

from components import auth
from components import datastore_utils
from components import pubsub
from components import utils

import event_mon_metrics
import ts_mon_metrics

from server import acl
from server import config
from server import task_pack
from server import task_queues
from server import task_request
from server import task_result
from server import task_to_run


### Private stuff.


_PROBABILITY_OF_QUICK_COMEBACK = 0.05


def _secs_to_ms(value):
  """Converts a seconds value in float to the number of ms as an integer."""
  return int(round(value * 1000.))


def _expire_task(to_run_key, request):
  """Expires a TaskResultSummary and unschedules the TaskToRun.

  Returns:
    True on success.
  """
  # Look if the TaskToRun is reapable once before doing the check inside the
  # transaction. This reduces the likelihood of failing this check inside the
  # transaction, which is an order of magnitude more costly.
  if not to_run_key.get().is_reapable:
    logging.info('Not reapable anymore')
    return None

  result_summary_key = task_pack.request_key_to_result_summary_key(request.key)
  now = utils.utcnow()

  def run():
    # 2 concurrent GET, one PUT. Optionally with an additional serialized GET.
    to_run_future = to_run_key.get_async()
    result_summary_future = result_summary_key.get_async()
    to_run = to_run_future.get_result()
    if not to_run or not to_run.is_reapable:
      result_summary_future.wait()
      return False

    to_run.queue_number = None
    result_summary = result_summary_future.get_result()
    if result_summary.try_number:
      # It's a retry that is being expired. Keep the old state. That requires an
      # additional pipelined GET but that shouldn't be the common case.
      run_result = result_summary.run_result_key.get()
      result_summary.set_from_run_result(run_result, request)
    else:
      result_summary.state = task_result.State.EXPIRED
    result_summary.abandoned_ts = now
    result_summary.modified_ts = now

    futures = ndb.put_multi_async((to_run, result_summary))
    _maybe_pubsub_notify_via_tq(result_summary, request)
    for f in futures:
      f.check_success()

    return True

  # Add it to the negative cache *before* running the transaction. Either way
  # the task was already reaped or the task is correctly expired and not
  # reapable.
  task_to_run.set_lookup_cache(to_run_key, False)

  # It'll be caught by next cron job execution in case of failure.
  try:
    success = datastore_utils.transaction(run)
  except datastore_utils.CommitError:
    success = False
  if success:
    logging.info(
        'Expired %s', task_pack.pack_result_summary_key(result_summary_key))
  return success


def _reap_task(bot_dimensions, bot_version, to_run_key, request):
  """Reaps a task and insert the results entity.

  Returns:
    (TaskRunResult, SecretBytes) if successful, (None, None) otherwise.
  """
  assert request.key == task_to_run.task_to_run_key_to_request_key(to_run_key)
  result_summary_key = task_pack.request_key_to_result_summary_key(request.key)
  bot_id = bot_dimensions[u'id'][0]

  now = utils.utcnow()
  # Log before the task id in case the function fails in a bad state where the
  # DB TX ran but the reply never comes to the bot. This is the worst case as
  # this leads to a task that results in BOT_DIED without ever starting. This
  # case is specifically handled in cron_handle_bot_died().
  logging.info(
      '_reap_task(%s)', task_pack.pack_result_summary_key(result_summary_key))

  def run():
    # 3 GET, 1 PUT at the end.
    to_run_future = to_run_key.get_async()
    result_summary_future = result_summary_key.get_async()
    if request.properties.has_secret_bytes:
      secret_bytes_future = request.secret_bytes_key.get_async()
    to_run = to_run_future.get_result()
    result_summary = result_summary_future.get_result()
    orig_summary_state = result_summary.state
    secret_bytes = None
    if request.properties.has_secret_bytes:
      secret_bytes = secret_bytes_future.get_result()
    if not to_run:
      logging.error('Missing TaskToRun?\n%s', result_summary.task_id)
      return None, None
    if not to_run.is_reapable:
      logging.info('%s is not reapable', result_summary.task_id)
      return None, None
    if result_summary.bot_id == bot_id:
      # This means two things, first it's a retry, second it's that the first
      # try failed and the retry is being reaped by the same bot. Deny that, as
      # the bot may be deeply broken and could be in a killing spree.
      # TODO(maruel): Allow retry for bot locked task using 'id' dimension.
      logging.warning(
          '%s can\'t retry its own internal failure task',
          result_summary.task_id)
      return None, None
    to_run.queue_number = None
    run_result = task_result.new_run_result(
        request, (result_summary.try_number or 0) + 1, bot_id, bot_version,
        bot_dimensions)
    # Upon bot reap, both .started_ts and .modified_ts matches. They differ on
    # the first ping.
    run_result.started_ts = now
    run_result.modified_ts = now
    result_summary.set_from_run_result(run_result, request)
    ndb.put_multi([to_run, run_result, result_summary])
    if result_summary.state != orig_summary_state:
      _maybe_pubsub_notify_via_tq(result_summary, request)
    return run_result, secret_bytes

  # Add it to the negative cache *before* running the transaction. This will
  # inhibit concurrently readers to try to reap this task. The downside is if
  # this request fails in the middle of the transaction, the task may stay
  # unreapable for up to 15 seconds.
  task_to_run.set_lookup_cache(to_run_key, False)

  try:
    run_result, secret_bytes = datastore_utils.transaction(run, retries=0)
  except datastore_utils.CommitError:
    # The challenge here is that the transaction may have failed because:
    # - The DB had an hickup and the TaskToRun, TaskRunResult and
    #   TaskResultSummary haven't been updated.
    # - The entities had been updated by a concurrent transaction on another
    #   handler so it was not reapable anyway. This does cause exceptions as
    #   both GET returns the TaskToRun.queue_number != None but only one succeed
    #   at the PUT.
    #
    # In the first case, we may want to reset the negative cache, while we don't
    # want to in the later case. The trade off are one of:
    # - negative cache is incorrectly set, so the task is not reapable for 15s
    # - resetting the negative cache would cause even more contention
    #
    # We chose the first one here for now, as the when the DB starts misbehaving
    # and the index becomes stale, it means the DB is *already* not in good
    # shape, so it is preferable to not put more stress on it, and skipping a
    # few tasks for 15s may even actively help the DB to stabilize.
    logging.info('CommitError; reaping failed')
    # The bot will reap the next available task in case of failure, no big deal.
    run_result = None
    secret_bytes = None
  return run_result, secret_bytes


def _handle_dead_bot(run_result_key):
  """Handles TaskRunResult where its bot has stopped showing sign of life.

  Transactionally updates the entities depending on the state of this task. The
  task may be retried automatically, canceled or left alone.

  Returns:
    True if the task was retried, False if the task was killed, None if no
    action was done.
  """
  result_summary_key = task_pack.run_result_key_to_result_summary_key(
      run_result_key)
  request_key = task_pack.result_summary_key_to_request_key(result_summary_key)
  request_future = request_key.get_async()
  now = utils.utcnow()
  server_version = utils.get_app_version()
  packed = task_pack.pack_run_result_key(run_result_key)
  request = request_future.get_result()
  to_run_key = task_to_run.request_to_task_to_run_key(request)

  def run():
    """Returns tuple(task_is_retried or None, bot_id)."""
    # Do one GET, one PUT at the end.
    run_result, result_summary, to_run = ndb.get_multi(
        (run_result_key, result_summary_key, to_run_key))
    if run_result.state != task_result.State.RUNNING:
      # It was updated already or not updating last. Likely DB index was stale.
      return None, run_result.bot_id
    if run_result.modified_ts > now - task_result.BOT_PING_TOLERANCE:
      # The query index IS stale.
      return None, run_result.bot_id

    run_result.signal_server_version(server_version)
    old_modified = run_result.modified_ts
    run_result.modified_ts = now

    orig_summary_state = result_summary.state
    if result_summary.try_number != run_result.try_number:
      # Not updating correct run_result, cancel it without touching
      # result_summary.
      to_put = (run_result,)
      run_result.state = task_result.State.BOT_DIED
      run_result.internal_failure = True
      run_result.abandoned_ts = now
      task_is_retried = None
    elif (result_summary.try_number == 1 and now < request.expiration_ts and
          (request.properties.idempotent or
            run_result.started_ts == old_modified)):
      # Retry it. It fits:
      # - first try
      # - not yet expired
      # - One of:
      #   - idempotent
      #   - task hadn't got any ping at all from task_runner.run_command()
      # TODO(maruel): Allow retry for bot locked task using 'id' dimension.
      to_put = (run_result, result_summary, to_run)
      to_run.queue_number = task_to_run.gen_queue_number(request)
      run_result.state = task_result.State.BOT_DIED
      run_result.internal_failure = True
      run_result.abandoned_ts = now
      # Do not sync data from run_result to result_summary, since the task is
      # being retried.
      result_summary.reset_to_pending()
      result_summary.modified_ts = now
      task_is_retried = True
    else:
      # Kill it as BOT_DIED, there was more than one try, the task expired in
      # the meantime or it wasn't idempotent.
      to_put = (run_result, result_summary)
      run_result.state = task_result.State.BOT_DIED
      run_result.internal_failure = True
      run_result.abandoned_ts = now
      result_summary.set_from_run_result(run_result, request)
      task_is_retried = False

    futures = ndb.put_multi_async(to_put)
    # if result_summary.state != orig_summary_state:
    if orig_summary_state != result_summary.state:
      _maybe_pubsub_notify_via_tq(result_summary, request)
    for f in futures:
      f.check_success()

    return task_is_retried

  # Remove it from the negative cache *before* running the transaction. Either
  # way the TaskToRun.queue_number was not set so there was no contention on
  # this entity. At best the task is reenqueued for a retry.
  task_to_run.set_lookup_cache(to_run_key, True)

  try:
    task_is_retried = datastore_utils.transaction(run)
  except datastore_utils.CommitError:
    task_is_retried = None
  if task_is_retried:
    logging.info('Retried %s', packed)
  elif task_is_retried == False:
    logging.debug('Ignored %s', packed)
  return task_is_retried


def _copy_summary(src, dst, skip_list):
  """Copies the attributes of entity src into dst.

  It doesn't copy the key nor any member in skip_list.
  """
  assert type(src) == type(dst), '%s!=%s' % (src.__class__, dst.__class__)
  # Access to a protected member _XX of a client class - pylint: disable=W0212
  kwargs = {
    k: getattr(src, k) for k in src._properties_fixed() if k not in skip_list
  }
  dst.populate(**kwargs)


def _maybe_pubsub_notify_now(result_summary, request):
  """Examines result_summary and sends task completion PubSub message.

  Does it only if result_summary indicates a task in some finished state and
  the request is specifying pubsub topic.

  Returns False to trigger the retry (on transient errors), or True if retry is
  not needed (e.g. messages was sent successfully or fatal error happened).
  """
  assert not ndb.in_transaction()
  assert isinstance(
      result_summary, task_result.TaskResultSummary), result_summary
  assert isinstance(request, task_request.TaskRequest), request
  if (result_summary.state in task_result.State.STATES_NOT_RUNNING and
      request.pubsub_topic):
    task_id = task_pack.pack_result_summary_key(result_summary.key)
    try:
      _pubsub_notify(
          task_id, request.pubsub_topic,
          request.pubsub_auth_token, request.pubsub_userdata)
    except pubsub.TransientError:
      logging.exception('Transient error when sending PubSub notification')
      return False
    except pubsub.Error:
      logging.exception('Fatal error when sending PubSub notification')
      return True # do not retry it
  return True


def _maybe_pubsub_notify_via_tq(result_summary, request):
  """Examines result_summary and enqueues a task to send PubSub message.

  Must be called within a transaction.

  Raises CommitError on errors (to abort the transaction).
  """
  assert ndb.in_transaction()
  assert isinstance(
      result_summary, task_result.TaskResultSummary), result_summary
  assert isinstance(request, task_request.TaskRequest), request
  if request.pubsub_topic:
    task_id = task_pack.pack_result_summary_key(result_summary.key)
    ok = utils.enqueue_task(
        url='/internal/taskqueue/pubsub/%s' % task_id,
        queue_name='pubsub',
        transactional=True,
        payload=utils.encode_to_json({
          'task_id': task_id,
          'topic': request.pubsub_topic,
          'auth_token': request.pubsub_auth_token,
          'userdata': request.pubsub_userdata,
        }))
    if not ok:
      raise datastore_utils.CommitError('Failed to enqueue task')


def _pubsub_notify(task_id, topic, auth_token, userdata):
  """Sends PubSub notification about task completion.

  Raises pubsub.TransientError on transient errors. Fatal errors are logged, but
  not retried.
  """
  logging.debug(
      'Sending PubSub notify to "%s" (with userdata "%s") about '
      'completion of "%s"', topic, userdata, task_id)
  msg = {'task_id': task_id}
  if userdata:
    msg['userdata'] = userdata
  try:
    pubsub.publish(
        topic=topic,
        message=utils.encode_to_json(msg),
        attributes={'auth_token': auth_token} if auth_token else None)
  except pubsub.Error:
    logging.exception('Fatal error when sending PubSub notification')


def _check_dimension_acls(request):
  """Raises AuthorizationError if some requested dimensions are forbidden.

  Uses 'dimension_acls' field from the settings. See proto/config.proto.
  """
  dim_acls = config.settings().dimension_acls
  if not dim_acls or not dim_acls.entry:
    return # not configured, this is fine

  ident = request.authenticated
  dims = request.properties.dimensions
  assert ident is not None # see task_request.init_new_request

  for k, v in sorted(dims.iteritems()):
    if not _can_use_dimension(dim_acls, ident, k, v):
      raise auth.AuthorizationError(
          'User %s is not allowed to schedule tasks with dimension "%s:%s"' %
          (ident.to_bytes(), k, v))


def _can_use_dimension(dim_acls, ident, k, v):
  """Returns True if 'dimension_acls' allow the given dimension to be used.

  Args:
    dim_acls: config_pb2.DimensionACLs message.
    ident: auth.Identity to check.
    k: dimension name.
    v: dimension value.
  """
  for e in dim_acls.entry:
    if '%s:%s' % (k, v) in e.dimension or '%s:*' % k in e.dimension:
      return auth.is_group_member(e.usable_by, ident)
  # A dimension not mentioned in 'dimension_acls' is allowed by default.
  return True


def _find_dupe_task(now, h):
  """Finds a previously run task that is also idempotent and completed.

  Fetch items that can be used to dedupe the task. See the comment for this
  property for more details.

  Do not use "task_result.TaskResultSummary.created_ts > oldest" here because
  this would require a composite index. It's unnecessary because TaskRequest.key
  is equivalent to decreasing TaskRequest.created_ts, ordering by key works as
  well and doesn't require a composite index.
  """
  # TODO(maruel): Make a reverse map on successful task completion so this
  # becomes a simple ndb.get().
  cls = task_result.TaskResultSummary
  q = cls.query(cls.properties_hash==h).order(cls.key)
  for i, dupe_summary in enumerate(q.iter(batch_size=1)):
    # It is possible for the query to return stale items.
    if (dupe_summary.state != task_result.State.COMPLETED or
        dupe_summary.failure):
      if i == 2:
        # Indexes are very inconsistent, give up.
        return None
      continue

    # Refuse tasks older than X days. This is due to the isolate server
    # dropping files.
    # TODO(maruel): The value should be calculated from the isolate server
    # setting and be unbounded when no isolated input was used.
    oldest = now - datetime.timedelta(
        seconds=config.settings().reusable_task_age_secs)
    if dupe_summary.created_ts <= oldest:
      return None
    return dupe_summary
  return None


### Public API.


def exponential_backoff(attempt_num):
  """Returns an exponential backoff value in seconds."""
  assert attempt_num >= 0
  if random.random() < _PROBABILITY_OF_QUICK_COMEBACK:
    # Randomly ask the bot to return quickly.
    return 1.0

  # If the user provided a max then use it, otherwise use default 60s.
  max_wait = config.settings().max_bot_sleep_time or 60.
  return min(max_wait, math.pow(1.5, min(attempt_num, 10) + 1))


def schedule_request(request, secret_bytes, check_acls=True):
  """Creates and stores all the entities to schedule a new task request.

  Checks ACLs first. Raises auth.AuthorizationError if caller is not authorized
  to post this request.

  The number of entities created is 3: TaskRequest, TaskToRun and
  TaskResultSummary.

  All 4 entities in the same entity group (TaskReqest, TaskToRun,
  TaskResultSummary, SecretBytes) are saved as a DB transaction.

  Arguments:
  - request: TaskRequest entity to be saved in the DB. It's key must not be set
             and the entity must not be saved in the DB yet.
  - secret_bytes: SecretBytes entity to be saved in the DB. It's key will be set
             and the entity will be stored by this function. None is allowed if
             there are no SecretBytes for this task.
  - check_acls: Whether the request should check ACLs.

  Returns:
    TaskResultSummary. TaskToRun is not returned.
  """
  assert isinstance(request, task_request.TaskRequest), request
  assert not request.key, request.key

  # Raises AuthorizationError with helpful message if the request.authorized
  # can't use some of the requested dimensions.
  if check_acls:
    _check_dimension_acls(request)

  # This does a DB GET, occasionally triggers a task queue. May throw, which is
  # surfaced to the user but it is safe as the task request wasn't stored yet.
  task_queues.assert_task(request)

  now = utils.utcnow()
  request.key = task_request.new_request_key()
  task = task_to_run.new_task_to_run(request)
  result_summary = task_result.new_result_summary(request)
  result_summary.modified_ts = now
  if secret_bytes:
    secret_bytes.key = request.secret_bytes_key

  def get_new_keys():
    # Warning: this assumes knowledge about the hierarchy of each entity.
    key = task_request.new_request_key()
    task.key = ndb.Key(task.key.kind(), task.key.id(), parent=key)
    if secret_bytes:
      secret_bytes.key = ndb.Key(
          secret_bytes.key.kind(), secret_bytes.key.id(), parent=key)
    old = result_summary.task_id
    result_summary.key = ndb.Key(
        result_summary.key.kind(), result_summary.key.id(), parent=key)
    logging.info('%s conflicted, using %s', old, result_summary.task_id)
    return key

  deduped = False
  if request.properties.idempotent:
    dupe_summary = _find_dupe_task(now, request.properties_hash)
    if dupe_summary:
      # Setting task.queue_number to None removes it from the scheduling.
      task.queue_number = None
      _copy_summary(
          dupe_summary, result_summary,
          ('created_ts', 'modified_ts', 'name', 'user', 'tags'))
      # Zap irrelevant properties. PerformanceStats is also not copied over,
      # since it's not relevant.
      result_summary.properties_hash = None
      result_summary.try_number = 0
      result_summary.cost_saved_usd = result_summary.cost_usd
      # Only zap after.
      result_summary.costs_usd = []
      result_summary.deduped_from = task_pack.pack_run_result_key(
          dupe_summary.run_result_key)
      # In this code path, there's not much to do as the task will not be run,
      # previous results are returned. We still need to store all the entities
      # correctly. However, since the has_secret_bytes property is already set
      # for UI purposes, and the task itself will never be run, we skip storing
      # the SecretBytes, as they would never be read and will just consume space
      # in the datastore (and the task we deduplicated with will have them
      # stored anyway, if we really want to get them again).
      datastore_utils.insert(
          request, get_new_keys, extra=[task, result_summary])
      logging.debug(
          'New request %s reusing %s', result_summary.task_id,
          dupe_summary.task_id)
      deduped = True

  if not deduped:
    # Storing these entities makes this task live. It is important at this point
    # that the HTTP handler returns as fast as possible, otherwise the task will
    # be run but the client will not know about it.
    datastore_utils.insert(request, get_new_keys,
        extra=filter(bool, [task, result_summary, secret_bytes]))
    logging.debug('New request %s', result_summary.task_id)

  # Get parent task details if applicable.
  if request.parent_task_id:
    parent_run_key = task_pack.unpack_run_result_key(request.parent_task_id)
    parent_task_keys = [
      parent_run_key,
      task_pack.run_result_key_to_result_summary_key(parent_run_key),
    ]

    def run_parent():
      # This one is slower.
      items = ndb.get_multi(parent_task_keys)
      k = result_summary.task_id
      for item in items:
        item.children_task_ids.append(k)
        item.modified_ts = now
      ndb.put_multi(items)

    # Raising will abort to the caller. There's a risk that for tasks with
    # parent tasks, the task will be lost due to this transaction.
    # TODO(maruel): An option is to update the parent task as part of a cron
    # job, which would remove this code from the critical path.
    datastore_utils.transaction(run_parent)

  ts_mon_metrics.update_jobs_requested_metrics(result_summary, deduped)
  return result_summary


def bot_reap_task(bot_dimensions, bot_version, deadline):
  """Reaps a TaskToRun if one is available.

  The process is to find a TaskToRun where its .queue_number is set, then
  create a TaskRunResult for it.

  Returns:
    tuple of (TaskRequest, SecretBytes, TaskRunResult) for the task that was
    reaped. The TaskToRun involved is not returned.
  """
  start = time.time()
  bot_id = bot_dimensions[u'id'][0]
  iterated = 0
  failures = 0
  try:
    q = task_to_run.yield_next_available_task_to_dispatch(
        bot_dimensions, deadline)
    for request, to_run in q:
      iterated += 1
      run_result, secret_bytes = _reap_task(
          bot_dimensions, bot_version, to_run.key, request)
      if not run_result:
        failures += 1
        # Sad thing is that there is not way here to know the try number.
        logging.info(
            'failed to reap: %s0',
            task_pack.pack_request_key(to_run.request_key))
        continue

      logging.info('Reaped: %s', run_result.task_id)
      return request, secret_bytes, run_result
    return None, None, None
  finally:
    logging.debug(
        'bot_reap_task(%s) in %.3fs: %d iterated, %d failure',
        bot_id, time.time()-start, iterated, failures)


def bot_update_task(
    run_result_key, bot_id, output, output_chunk_start, exit_code, duration,
    hard_timeout, io_timeout, cost_usd, outputs_ref, cipd_pins,
    performance_stats):
  """Updates a TaskRunResult and TaskResultSummary, along TaskOutputChunk.

  Arguments:
  - run_result_key: ndb.Key to TaskRunResult.
  - bot_id: Self advertised bot id to ensure it's the one expected.
  - output: Data to append to this command output.
  - output_chunk_start: Index of output in the stdout stream.
  - exit_code: Mark that this task completed.
  - duration: Time spent in seconds for this task, excluding overheads.
  - hard_timeout: Bool set if an hard timeout occured.
  - io_timeout: Bool set if an I/O timeout occured.
  - cost_usd: Cost in $USD of this task up to now.
  - outputs_ref: task_request.FilesRef instance or None.
  - cipd_pins: None or task_result.CipdPins
  - performance_stats: task_result.PerformanceStats instance or None. Can only
        be set when the task is completing.

  Invalid states, these are flat out refused:
  - A command is updated after it had an exit code assigned to.

  Returns:
    TaskRunResult.state or None in case of failure.
  """
  assert output_chunk_start is None or isinstance(output_chunk_start, int)
  assert output is None or isinstance(output, str)
  if cost_usd is not None and cost_usd < 0.:
    raise ValueError('cost_usd must be None or greater or equal than 0')
  if duration is not None and duration < 0.:
    raise ValueError('duration must be None or greater or equal than 0')
  if (duration is None) != (exit_code is None):
    raise ValueError(
        'had unexpected duration; expected iff a command completes\n'
        'duration: %r; exit: %r' % (duration, exit_code))
  if performance_stats and duration is None:
    raise ValueError(
        'duration must be set when performance_stats is set\n'
        'duration: %s; performance_stats: %s' %
        (duration, performance_stats))

  packed = task_pack.pack_run_result_key(run_result_key)
  logging.debug(
      'bot_update_task(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
      packed, bot_id, len(output) if output else output, output_chunk_start,
      exit_code, duration, hard_timeout, io_timeout, cost_usd, outputs_ref,
      cipd_pins, performance_stats)

  result_summary_key = task_pack.run_result_key_to_result_summary_key(
      run_result_key)
  request_key = task_pack.result_summary_key_to_request_key(result_summary_key)
  request_future = request_key.get_async()
  server_version = utils.get_app_version()
  request = request_future.get_result()
  now = utils.utcnow()

  def run():
    """Returns tuple(TaskRunResult, bool(completed), str(error)).

    Any error is returned as a string to be passed to logging.error() instead of
    logging inside the transaction for performance.
    """
    # 2 consecutive GETs, one PUT.
    run_result_future = run_result_key.get_async()
    result_summary_future = result_summary_key.get_async()
    run_result = run_result_future.get_result()
    if not run_result:
      result_summary_future.wait()
      return None, None, 'is missing'

    if run_result.bot_id != bot_id:
      result_summary_future.wait()
      return None, None, (
          'expected bot (%s) but had update from bot %s' % (
          run_result.bot_id, bot_id))

    if not run_result.started_ts:
      return None, None, 'TaskRunResult is broken; %s' % (
          run_result.to_dict())

    # Assumptions:
    # - duration and exit_code are both set or not set.
    # - same for run_result.
    if exit_code is not None:
      if run_result.exit_code is not None:
        # This happens as an HTTP request is retried when the DB write succeeded
        # but it still returned HTTP 500.
        if run_result.exit_code != exit_code:
          result_summary_future.wait()
          return None, None, 'got 2 different exit_code; %s then %s' % (
              run_result.exit_code, exit_code)
        if run_result.duration != duration:
          result_summary_future.wait()
          return None, None, 'got 2 different durations; %s then %s' % (
              run_result.duration, duration)
      else:
        run_result.duration = duration
        run_result.exit_code = exit_code

    if outputs_ref:
      run_result.outputs_ref = outputs_ref

    if cipd_pins:
      run_result.cipd_pins = cipd_pins

    if run_result.state in task_result.State.STATES_RUNNING:
      if hard_timeout or io_timeout:
        run_result.state = task_result.State.TIMED_OUT
        run_result.completed_ts = now
      elif run_result.exit_code is not None:
        run_result.state = task_result.State.COMPLETED
        run_result.completed_ts = now

    run_result.signal_server_version(server_version)
    run_result.validate(request)
    to_put = [run_result]
    if output:
      # This does 1 multi GETs. This also modifies run_result in place.
      to_put.extend(run_result.append_output(output, output_chunk_start or 0))
    if performance_stats:
      performance_stats.key = task_pack.run_result_key_to_performance_stats_key(
          run_result.key)
      to_put.append(performance_stats)

    run_result.cost_usd = max(cost_usd, run_result.cost_usd or 0.)
    run_result.modified_ts = now

    result_summary = result_summary_future.get_result()
    if (result_summary.try_number and
        result_summary.try_number > run_result.try_number):
      # The situation where a shard is retried but the bot running the previous
      # try somehow reappears and reports success, the result must still show
      # the last try's result. We still need to update cost_usd manually.
      result_summary.costs_usd[run_result.try_number-1] = run_result.cost_usd
      result_summary.modified_ts = now
    else:
      result_summary.set_from_run_result(run_result, request)

    result_summary.validate(request)
    to_put.append(result_summary)
    ndb.put_multi(to_put)

    return result_summary, run_result, None

  try:
    smry, run_result, error = datastore_utils.transaction(run)
  except datastore_utils.CommitError as e:
    logging.info('Got commit error: %s', e)
    # It is important that the caller correctly surface this error.
    return None
  assert bool(error) != bool(run_result), (error, run_result)
  if error:
    logging.error('Task %s %s', packed, error)
    return None
  # Caller must retry if PubSub enqueue fails.
  if not _maybe_pubsub_notify_now(smry, request):
    return None
  if smry.state not in task_result.State.STATES_RUNNING:
    event_mon_metrics.send_task_event(smry)
    ts_mon_metrics.update_jobs_completed_metrics(smry)
  return run_result.state


def bot_kill_task(run_result_key, bot_id):
  """Terminates a task that is currently running as an internal failure.

  Returns:
    str if an error message.
  """
  result_summary_key = task_pack.run_result_key_to_result_summary_key(
      run_result_key)
  request = task_pack.result_summary_key_to_request_key(
      result_summary_key).get()
  server_version = utils.get_app_version()
  now = utils.utcnow()
  packed = task_pack.pack_run_result_key(run_result_key)

  def run():
    run_result, result_summary = ndb.get_multi(
        (run_result_key, result_summary_key))
    if bot_id and run_result.bot_id != bot_id:
      return 'Bot %s sent task kill for task %s owned by bot %s' % (
          bot_id, packed, run_result.bot_id)

    if run_result.state == task_result.State.BOT_DIED:
      # Ignore this failure.
      return None

    run_result.signal_server_version(server_version)
    run_result.state = task_result.State.BOT_DIED
    run_result.internal_failure = True
    run_result.abandoned_ts = now
    run_result.modified_ts = now
    result_summary.set_from_run_result(run_result, None)

    futures = ndb.put_multi_async((run_result, result_summary))
    _maybe_pubsub_notify_via_tq(result_summary, request)
    for f in futures:
      f.check_success()

    return None

  try:
    msg = datastore_utils.transaction(run)
  except datastore_utils.CommitError as e:
    # At worst, the task will be tagged as BOT_DIED after BOT_PING_TOLERANCE
    # seconds passed on the next cron_handle_bot_died cron job.
    return 'Failed killing task %s: %s' % (packed, e)
  return msg


def cancel_task(request, result_key):
  """Cancels a task if possible.

  Ensures that the associated TaskToRun is canceled and updates the
  TaskResultSummary/TaskRunResult accordingly.

  Warning: ACL check must have been done before.
  """
  to_run_key = task_to_run.request_to_task_to_run_key(request)
  if result_key.kind() == 'TaskRunResult':
    result_key = task_pack.run_result_key_to_result_summary_key(result_key)
  now = utils.utcnow()

  def run():
    to_run, result_summary = ndb.get_multi((to_run_key, result_key))
    was_running = result_summary.state == task_result.State.RUNNING
    if not result_summary.can_be_canceled:
      return False, was_running
    to_run.queue_number = None
    result_summary.state = task_result.State.CANCELED
    result_summary.abandoned_ts = now
    result_summary.modified_ts = now

    futures = ndb.put_multi_async((to_run, result_summary))
    _maybe_pubsub_notify_via_tq(result_summary, request)
    for f in futures:
      f.check_success()

    return True, was_running

  # Add it to the negative cache *before* running the transaction. Either way
  # the task was already reaped or the task is correctly canceled thus not
  # reapable.
  task_to_run.set_lookup_cache(to_run_key, False)

  try:
    ok, was_running = datastore_utils.transaction(run)
  except datastore_utils.CommitError as e:
    packed = task_pack.pack_result_summary_key(result_key)
    return 'Failed killing task %s: %s' % (packed, e)

  # TODO(maruel): Add paper trail.
  return ok, was_running



### Cron job.


def cron_abort_expired_task_to_run(host):
  """Aborts expired TaskToRun requests to execute a TaskRequest on a bot.

  Three reasons can cause this situation:
  - Higher throughput of task requests incoming than the rate task requests
    being completed, e.g. there's not enough bots to run all the tasks that gets
    in at the current rate. That's normal overflow and must be handled
    accordingly.
  - No bot connected that satisfies the requested dimensions. This is trickier,
    it is either a typo in the dimensions or bots all died and the admins must
    reconnect them.
  - Server has internal failures causing it to fail to either distribute the
    tasks or properly receive results from the bots.

  Returns:
    Packed tasks ids of aborted tasks.
  """
  killed = []
  skipped = 0
  try:
    for to_run in task_to_run.yield_expired_task_to_run():
      request = to_run.request_key.get()
      if _expire_task(to_run.key, request):
        # TODO(maruel): Know which try it is.
        killed.append(request)
        ts_mon_metrics.tasks_expired.increment(
            fields=ts_mon_metrics.extract_job_fields(request.tags))
      else:
        # It's not a big deal, the bot will continue running.
        skipped += 1
  finally:
    if killed:
      logging.warning(
          'EXPIRED!\n%d tasks:\n%s',
          len(killed),
          '\n'.join(
            '  %s/user/task/%s  %s' % (host, i.task_id, i.properties.dimensions)
            for i in killed))
    logging.info('Killed %d task, skipped %d', len(killed), skipped)
  return [i.task_id for i in killed]


def cron_handle_bot_died(host):
  """Aborts or retry stale TaskRunResult where the bot stopped sending updates.

  If the task was at its first try, it'll be retried. Otherwise the task will be
  canceled.

  Returns:
  - task IDs killed
  - number of task retried
  - number of task ignored
  """
  ignored = 0
  killed = []
  retried = 0
  try:
    for run_result_key in task_result.yield_run_result_keys_with_dead_bot():
      result = _handle_dead_bot(run_result_key)
      if result is True:
        retried += 1
      elif result is False:
        killed.append(task_pack.pack_run_result_key(run_result_key))
      else:
        ignored += 1
  finally:
    if killed:
      logging.error(
          'BOT_DIED!\n%d tasks:\n%s',
          len(killed),
          '\n'.join('  %s/user/task/%s' % (host, i) for i in killed))
    logging.info(
        'Killed %d; retried %d; ignored: %d', len(killed), retried, ignored)
  return killed, retried, ignored


## Task queue tasks.


def task_handle_pubsub_task(payload):
  """Handles task enqueued by _maybe_pubsub_notify_via_tq."""
  # Do not catch errors to trigger task queue task retry. Errors should not
  # happen in normal case.
  _pubsub_notify(
      payload['task_id'], payload['topic'],
      payload['auth_token'], payload['userdata'])
