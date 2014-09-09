# Task scheduler

The task scheduling core has to be read in order like a story in 4 parts; each
block depends on the previous ones:

* task_request.py
* task_to_run.py
* task_result.py
* task_scheduler.py


## General workflow

* A client wants to run something (a task) on the infrastructure and sends a
  HTTP POST to the Swarming server:
  * A TaskRequest describing this request is saved to note that a new request
    exists. The details of the task is saved in TaskProperties embedded in
    TaskRequest.
  * A TaskResultSummary is created to describe the request's overall status,
    taking in account retries.
  * A TaskToRun is created to dispatch this request so it can be run on a
    bot. It is marked as ready to be triggered when created.
* Bots poll for work. Once a bot reaps a TaskToRun, it creates the corresponding
  TaskRunResult for this run and updates it as required until completed. The
  TaskRunResult describes the result for this run on this specific bot. If the
  TaskRequest is retried automatically due to the bot dying, an automatic on
  task failure or another infrastructure related failure, another TaskRunResult
  will be created when another bot reaps the task again. TaskResultSummary is
  the summary of the last relevant TaskRunResult.


## Overall schema graph of a task request with 2 tries

       +------Root------+
       |TaskRequestShard|                                        task_request.py
       +----------------+
               ^
               |
    +---------------------+
    |TaskRequest          |
    |    +--------------+ |                                      task_request.py
    |    |TaskProperties| |
    |    +--------------+ |
    +---------------------+
         ^           ^
         |           |
    +---------+      |
    |TaskToRun|      |                                            task_to_run.py
    +---------+      |
                     |
                     |
            +-----------------+
            |TaskResultSummary|                                   task_result.py
            +-----------------+
               ^          ^
               |          |
               |          |
      +-------------+  +-------------+
      |TaskRunResult|  |TaskRunResult|                            task_result.py
      +-------------+  +-------------+
                 ^
                 |
               +----------+
               |TaskOutput|                                       task_result.py
               +----------+
                 ^      ^
                 |      |
    +---------------+  +---------------+
    |TaskOutputChunk|  |TaskOutputChunk|                          task_result.py
    +---------------+  +---------------+


## Keys

AppEngine's automatic key numbering is never used. The entities are directly
created with predefined keys so entity sharding can be tightly controlled to
reduce DB contention.

* TaskRequest has almost monotonically increasing key ids. The key is based on
  time but the multiple servers may not have a fully synchronized clock. 8 bits
  of randomness is injected to help reduce key id contention. The low 8 bits is
  set to 0.
* TaskResultSummary has key id = 1.
* TaskRunResult has monotonically increasing key id starting at 1.
* TaskToRun has the same key id as the corresponding TaskRequest.

### Notes

* Each root entity is tagged as Root.
* Each line is annoted with the file that define the entities on this line.
* Dotted line means a similar key relationship without actual entity hierarchy.