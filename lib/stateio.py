#!/usr/bin/env python3
"""
lib/stateio.py — concurrency-safe JSON state I/O.

THE PROBLEM THIS SOLVES
    The worker keeps small JSON documents (circuit breaker, attempt counters)
    that several workers in the same project update at once. The obvious
    implementation is:

        data = json.load(open(path))     # read
        data[k] += 1                     # modify
        json.dump(data, open(path,'w'))  # write

    That is a read-modify-write with no lock and a truncating write. Measured
    on this machine: 60 concurrent increments of one counter left 24, and
    concurrent readers hit JSONDecodeError against a half-written file. Two
    workers can also lose each other's increments even when the bytes happen to
    stay valid.

THE FIX
    1. An exclusive flock on a sidecar lock file, so read-modify-write is
       serialised across processes.
    2. Write to a temporary file in the SAME directory and os.replace() it
       into position. rename(2) within a filesystem is atomic, so a reader
       sees either the old document or the new one, never a partial one.
    3. Tolerate a corrupt or absent document on read by starting from the
       caller's default, so one damaged file cannot wedge the worker forever.
       (Fail-closed applies to ROUTING decisions; a counter losing its
       history must not brick the tool, and the default is the safe value.)

Deliberately not used: sqlite, redis, or any external service. The documents
are a few hundred bytes; a lock file and an atomic rename are proportionate.
"""

import errno
import fcntl
import json
import os
import tempfile


def read_json(path, default):
    """Read a JSON document, returning `default` on any problem."""
    try:
        with open(path) as fh:
            text = fh.read().strip()
        if not text:
            return default()
        return json.loads(text)
    except (IOError, OSError, ValueError):
        return default()


def _atomic_write(path, obj):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".stateio-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)   # atomic within the filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def locked_update(path, mutator, default=None):
    """
    Serialised read-modify-write.

    mutator(data) may mutate in place and may return a replacement document.
    """
    if default is None:
        def default():
            return {}

    lock_path = path + ".lock"
    lock_dir = os.path.dirname(os.path.abspath(lock_path)) or "."
    os.makedirs(lock_dir, exist_ok=True)

    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = read_json(path, default)
            result = mutator(data)
            _atomic_write(path, data if result is None else result)
            return data if result is None else result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_locked(path, default):
    """Consistent read: takes the lock so we never see a partial document."""
    lock_path = path + ".lock"
    lock_dir = os.path.dirname(os.path.abspath(lock_path)) or "."
    os.makedirs(lock_dir, exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            return read_json(path, default if default else dict)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
