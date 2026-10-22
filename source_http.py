"""Bounded retries for public source GETs. Never retries Discord or GitHub writes."""
import json
import random
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

UTC = timezone.utc
TRANSIENT = {408, 429, 500, 502, 503, 504}
BACKOFF = (2, 5, 15)


class SourceDeferred(Exception):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__('source retry deferred')


def retry_seconds(value, now):
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0, (target - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def transient(error):
    if isinstance(error, HTTPError):
        return error.code in TRANSIENT
    if isinstance(error, URLError):
        return not isinstance(error.reason, ssl.SSLCertVerificationError)
    return isinstance(error, (TimeoutError, ConnectionError))


def safe_reason(error):
    if isinstance(error, SourceDeferred):
        return '再試行待機中'
    return 'HTTP {}'.format(error.code) if isinstance(error, HTTPError) else type(error).__name__


class SourceFetcher:
    def __init__(self, request, retry_after=None, *, clock=None, sleep=None, jitter=None):
        self.request = request
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.jitter = jitter or (lambda: random.uniform(0, 1))
        self.retry_after, self.stats, self.waited = {}, {}, 0
        for host, value in (retry_after or {}).items():
            target = datetime.fromisoformat(value)
            if target.tzinfo is None:
                raise ValueError('Invalid source retry time')
            if target > self.clock():
                self.retry_after[host] = target.isoformat()

    def get(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError('Invalid public source URL')
        host = parsed.hostname
        stats = self.stats.setdefault(host, dict(attempts=0, retries=0, recovered_requests=0,
                                                  failed_requests=0, deferred_requests=0))
        target = self.retry_after.get(host)
        if target and datetime.fromisoformat(target) > self.clock():
            stats['deferred_requests'] += 1
            raise SourceDeferred(target)
        self.retry_after.pop(host, None)
        for attempt in range(4):
            stats['attempts'] += 1
            try:
                result = self.request(url)  # GET only; no credentials or payload.
                if attempt:
                    stats['recovered_requests'] += 1
                return result
            except Exception as error:
                if not transient(error):
                    stats['failed_requests'] += 1
                    raise
                delay = retry_seconds(error.headers.get('Retry-After'), self.clock()) if isinstance(error, HTTPError) else None
                if delay is not None:
                    self.retry_after[host] = (self.clock() + timedelta(seconds=delay)).isoformat()
                wait = max(BACKOFF[min(attempt, 2)] + self.jitter(), delay or 0)
                if attempt == 3 or wait > 30 or self.waited + wait > 90:
                    stats['failed_requests'] += 1
                    # Stop hammering other endpoints on an unhealthy host.
                    if not delay:
                        self.retry_after[host] = (self.clock() + timedelta(minutes=5)).isoformat()
                    raise
                stats['retries'] += 1
                self.sleep(wait)
                self.waited += wait
                self.retry_after.pop(host, None)

    def json(self, url):
        text, headers = self.get(url)
        return json.loads(text), headers
