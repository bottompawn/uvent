# coding=utf8

# Copyright (C) 2012 Saúl Ibarra Corretgé <saghul@gmail.com>
#

__all__ = ['UVLoop']

import functools
import os
import pyuv
import signal
import sys


class UVLoop(object):
    MINPRI = -2
    MAXPRI = 2

    def __init__(self, flags=None, default=True):
        if default:
            self._loop = pyuv.Loop.default_loop()
        else:
            self._loop = pyuv.Loop()
        self._loop.excepthook = functools.partial(self.handle_error, None)
        self._ticker = Ticker(self)
        self._child_watchers = {}
        self._watchers = set()
        self._sigchld_handle = None
        self._signal_checker = pyuv.SignalChecker(self._loop)

    def destroy(self):
        self._watchers.clear()
        self._ticker = None
        self._sigchld_handle = None
        self._signal_checker = None
        self._loop = None

    def _handle_syserr(self, message, errno):
        self.handle_error(None, SystemError, SystemError(message + ': ' + os.strerror(errno)), None)

    def handle_error(self, context, type, value, tb):
        error_handler = self.error_handler
        if error_handler is not None:
            # we do want to do getattr every time so that setting Hub.handle_error property just works
            handle_error = getattr(error_handler, 'handle_error', error_handler)
            handle_error(context, type, value, tb)
        else:
            self._default_handle_error(context, type, value, tb)

    def _default_handle_error(self, context, type, value, tb):
        import traceback
        traceback.print_exception(type, value, tb)
        self.break_(None)

    def run(self, nowait=False, once=False):
        self._signal_checker.start()
        if nowait:
            raise RuntimeError('nowait is not supported')
        if once:
            self._loop.run_once()
        else:
            self._loop.run()

    def reinit(self):
        pass

    def ref(self):
        raise NotImplementedError

    def unref(self):
        raise NotImplementedError

    def break_(self, how):
        # TODO: replace with uv_break when available
        def cb(handle):
            try:
                handle.close()
            except Exception:
                pass
        self._loop.walk(cb)

    def verify(self):
        pass

    def now(self):
        return self._loop.now()

    def update(self):
        self._loop.update_time()

    @property
    def default(self):
        return self._loop.default

    @property
    def iteration(self):
        raise NotImplementedError

    @property
    def depth(self):
        raise NotImplementedError

    @property
    def backend(self):
        raise NotImplementedError

    @property
    def backend_int(self):
        raise NotImplementedError

    @property
    def pendingcnt(self):
        raise NotImplementedError

    @property
    def activecnt(self):
        raise NotImplementedError

    @property
    def origflags(self):
        raise NotImplementedError

    @property
    def origflags_int(self):
        raise NotImplementedError

    def io(self, fd, events, ref=True, priority=None):
        return Io(self, fd, events, ref)

    def timer(self, after, repeat=0.0, ref=True, priority=None):
        return Timer(self, after, repeat, ref)

    def prepare(self, ref=True, priority=None):
        return Prepare(self, ref)

    def idle(self, ref=True, priority=None):
        return Idle(self, ref)

    def check(self, ref=True, priority=None):
        return Check(self, ref)

    def async(self, ref=True, priority=None):
        return Async(self, ref)

    def stat(self, path, interval=0.0, ref=True, priority=None):
        return Stat(self, path, interval, ref)

    def fork(self, ref=True, priority=None):
        return NoOp(self, ref)

    def child(self, pid, trace=False, ref=True):
        if sys.platform == 'win32':
            raise NotImplementedError
        return Child(self, pid, ref)

    def install_sigchld(self):
        if sys.platform == 'win32':
            raise NotImplementedError
        if self._loop.default and self._sigchld_handle is None:
            self._sigchld_handle = pyuv.Signal(self._loop)
            self._sigchld_handle.start(self._handle_SIGCHLD, signal.SIGCHLD)

    def signal(self, signum, ref=True, priority=None):
        return Signal(self, signum, ref)

    def callback(self, priority=None):
        return Callback(self)

    def run_callback(self, func, *args, **kw):
        result = Callback(self)
        result.start(func, *args)
        return result

    def fileno(self):
        raise NotImplementedError

    def _handle_SIGCHLD(self, handle, signum):
        pid, status, usage = os.wait3(os.WNOHANG)
        child = self._child_watchers.get(pid, None) or self._child_watchers.get(0, None)
        if child is not None:
            child._set_status(status)

    def _format(self):
        msg = ''
        if self.default:
            msg += ' default'
        return msg

    def __repr__(self):
        return '<%s at 0x%x%s>' % (self.__class__.__name__, id(self), self._format())


class Ticker(object):

    def __init__(self, loop):
        self._handle = pyuv.Idle(loop._loop)

    def _cb(self, handle):
        self._handle.stop()

    def tick(self):
        if not self._handle.active:
            self._handle.start(self._cb)
            self._handle.unref()


class Watcher(object):

    def __init__(self, loop, ref=True):
        self.loop = loop
        self._ref = ref
        self._callback = None

    @property
    def callback(self):
        return self._callback

    @property
    def active(self):
        return self._handle and self._handle.active

    @property
    def pending(self):
        return False

    def _get_ref(self):
        return self._ref
    def _set_ref(self, value):
        self._ref = value
        if self._handle:
            op = self._handle.ref if value else self._handle.unref
            op()
    ref = property(_get_ref, _set_ref)
    del _get_ref, _set_ref

    def start(self, callback, *args):
        self.loop._watchers.add(self)
        self._callback = functools.partial(callback, *args)

    def stop(self):
        self.loop._watchers.discard(self)
        self._callback = None

    def feed(self, revents, callback, *args):
        raise NotImplementedError

    def _run_callback(self):
        if self._callback:
            try:
                self._callback()
            except:
                self.loop.handle_error(self, *sys.exc_info())
            finally:
                if not self.active:
                    self.stop()

    def _format(self):
        return ''

    def __repr__(self):
        result = '<%s at 0x%x%s' % (self.__class__.__name__, id(self), self._format())
        if self.active:
            result += ' active'
        if self.pending:
            result += ' pending'
        if self.callback is not None:
            result += ' callback=%r' % self.callback
        return result + '>'


class NoOp(Watcher):

    def __init__(self, loop, ref=True):
        super(self, NoOp).__init__(loop, ref)
        self._handle = None

    def start(self, *args, **kw):
        pass

    def stop(self):
        pass


class Callback(Watcher):

    def __init__(self, loop, ref=True):
        super(Callback, self).__init__(loop, ref)
        self._prepare_handle = pyuv.Prepare(self.loop._loop)
        self._check_handle = pyuv.Check(self.loop._loop)

    def _cb(self, handle):
        try:
            self._callback()
        except:
            self.loop.handle_error(self, *sys.exc_info())
        finally:
            self.stop()

    def _get_ref(self):
        return self._ref
    def _set_ref(self, value):
        self._ref = value
        for handle in (self._prepare_handle, self._check_handle):
            if handle:
                op = handle.ref if value else handle.unref
                op()
    ref = property(_get_ref, _set_ref)
    del _get_ref, _set_ref

    @property
    def active(self):
        return (self._prepare_handle and self._prepare_handle.active) or (self._check_handle and self._check_handle.active)

    def start(self, callback, *args):
        super(Callback, self).start(callback, *args)
        self.loop._ticker.tick()
        self._prepare_handle.start(self._cb)
        self._check_handle.start(self._cb)
        if not self._ref:
            self._prepare_handle.unref()
            self._check_handle.unref()

    def stop(self):
        self._prepare_handle.stop()
        self._check_handle.stop()
        super(Callback, self).stop()


class Timer(Watcher):

    def __init__(self, loop, after=0.0, repeat=0.0, ref=True):
        if repeat < 0.0:
            raise ValueError("repeat must be positive or zero: %r" % repeat)
        super(Timer, self).__init__(loop, ref)
        self._after = after
        self._repeat = repeat
        self._handle = pyuv.Timer(self.loop._loop)

    def _timer_cb(self, handle):
        self._run_callback()

    def start(self, callback, *args, **kw):
        super(Timer, self).start(callback, *args)
        if kw.get('update', True):
            self.loop.update()
        self._handle.start(self._timer_cb, self._after, self._repeat)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Timer, self).stop()

    def again(self, callback, *args, **kw):
        if not self._handle:
            raise RuntimeError('timer not started')
        self.loop._watchers.add(self)
        self._callback = functools.partial(callback, *args)
        if kw.get('update', True):
            self.loop.update()
        self._handle.again()

    @property
    def at(self):
        raise NotImplementedError


class Prepare(Watcher):

    def __init__(self, loop, ref=True):
        super(Prepare, self).__init__(loop, ref)
        self._handle = pyuv.Prepare(self.loop._loop)

    def _prepare_cb(self, handle):
        self._run_callback()

    def start(self, callback, *args):
        super(Prepare, self).start(callback, *args)
        self._handle.start(self._prepare_cb)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Prepare, self).stop()


class Idle(Watcher):

    def __init__(self, loop, ref=True):
        super(Idle, self).__init__(loop, ref)
        self._handle = pyuv.Idle(self.loop._loop)

    def _idle_cb(self, handle):
        self._run_callback()

    def start(self, callback, *args):
        super(Idle, self).start(callback, *args)
        self._handle.start(self._idle_cb)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Idle, self).stop()


class Check(Watcher):

    def __init__(self, loop, ref=True):
        super(Check, self).__init__(loop, ref)
        self._handle = pyuv.Check(self.loop._loop)

    def _check_cb(self, handle):
        self._run_callback()

    def start(self, callback, *args):
        super(Check, self).start(callback, *args)
        self._handle.start(self._check_cb)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Check, self).stop()


class Io(Watcher):

    def __init__(self, loop, fd, events, ref=True):
        super(Io, self).__init__(loop, ref)
        self._fd = fd
        self._events = self._ev2uv(events)
        self._handle = pyuv.Poll(self.loop._loop, self._fd)

    @classmethod
    def _ev2uv(cls, events):
        uv_events = 0
        if events & 1:
            uv_events |= pyuv.UV_READABLE
        if events & 2:
            uv_events |= pyuv.UV_WRITABLE
        return uv_events

    def _poll_cb(self, handle, events, error):
        if error is not None:
            self._handle.stop()
            return
        try:
            self._callback()
        except:
            self.loop.handle_error(self, *sys.exc_info())
            self.stop()
        finally:
            if not self.active:
                self.stop()

    def start(self, callback, *args, **kw):
        super(Io, self).start(callback, *args)
        self._handle.start(self._events, self._poll_cb)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Io, self).stop()

    def _get_fd(self):
        return self._fd
    def _set_fd(self, value):
        self._fd = value
        self._handle.stop()
        self._handle = pyuv.Poll(self.loop._loop, self._fd)
    fd = property(_get_fd, _set_fd)
    del _get_fd, _set_fd

    def _get_events(self):
        return self._events
    def _set_events(self, value):
        self._events = self._ev2uv(value)
        self._handle.start(self._events, self._poll_cb)
    events = property(_get_events, _set_events)
    del _get_events, _set_events

    @property
    def events_str(self):
        r = []
        if self._events & pyuv.UV_READABLE:
            r.append('UV_READABLE')
        if self._events & pyuv.UV_WRITABLE:
            r.append('UV_WRITABLE')
        return '|'.join(r)

    def _format(self):
        return ' fd=%s events=%s' % (self.fd, self.events_str)


class Async(Watcher):

    def __init__(self, loop, ref=True):
        super(Async, self).__init__(loop, ref)
        self._handle = pyuv.Async(self.loop._loop, self._async_cb)

    def _async_cb(self, handle):
        self._run_callback()

    def start(self, callback, *args, **kw):
        super(Async, self).start(callback, *args)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        super(Async, self).stop()

    def send(self):
        self._handle.send()


class Child(Watcher):

    def __init__(self, loop, pid, ref=True):
        if not loop.default:
            raise TypeError("child watchers are only allowed in the default loop")
        super(Child, self).__init__(loop, ref)
        loop.install_sigchld()
        self._active = False
        self._pid = pid
        self.rpid = None
        self.rstatus = None
        self._handle = pyuv.Async(self.loop._loop, self._async_cb)

    @property
    def active(self):
        return self._active

    @property
    def pid(self):
        return self._pid

    def _async_cb(self, handle):
        self._run_callback()

    def start(self, callback, *args, **kw):
        super(Child, self).start(callback, *args)
        if not self._ref:
            self._handle.unref()
        self._active = True
        # TODO: should someone be able to register 2 child watchers for the same PID?
        self.loop._child_watchers[self._pid] = self

    def stop(self):
        self._active = False
        self.loop._child_watchers.pop(self._pid, None)
        super(Child, self).stop()

    def _set_status(self, status):
        self.rstatus = status
        self.rpid = os.getpid()
        self._handle.send()

    def _format(self):
        return ' pid=%r rstatus=%r' % (self.pid, self.rstatus)


class Signal(Watcher):

    def __init__(self, loop, signum, ref):
        super(Signal, self).__init__(loop, ref)
        self._signum = signum
        self._handle = pyuv.Signal(self.loop._loop)

    def _signal_cb(self, handle, signum):
        self._run_callback()

    def start(self, callback, *args):
        super(Signal, self).start(callback, *args)
        self._handle.start(self._signal_cb, self._signum)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Signal, self).stop()


class Stat(Watcher):

    def __init__(self, loop, path, interval, ref):
        super(Stat, self).__init__(loop, ref)
        self._path = path
        self._interval = interval
        self._attr = None
        self._prev = None
        self._handle = pyuv.fs.FSPoll(self.loop._loop)

    @property
    def path(self):
        return self._path

    @property
    def interval(self):
        return self._interval

    @property
    def attr(self):
        if self._attr is not None and self._attr.st_nlink:
            return self._attr
        return None

    @property
    def prev(self):
        if self._prev is not None and self._prev.st_nlink:
            return self._prev
        return None

    def _fspoll_cb(self, handle, prev_stat, curr_stat, error):
        if error is None:
            self._prev = prev_stat
            self._attr = curr_stat
        else:
            self._attr = None
        self._run_callback()

    def start(self, callback, *args):
        super(Stat, self).start(callback, *args)
        self._handle.start(self._path, self._fspoll_cb, self._interval)
        if not self._ref:
            self._handle.unref()

    def stop(self):
        self._handle.stop()
        super(Stat, self).stop()


