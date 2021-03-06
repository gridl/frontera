# -*- coding: utf-8 -*-
from __future__ import absolute_import

import threading
from time import asctime

from six.moves import map

from frontera.exceptions import NotConfigured
from frontera.utils.url import parse_domain_from_url_fast
from . import DBWorkerThreadComponent


class BatchGenerator(DBWorkerThreadComponent):
    """Component to get data from backend and send it to spider feed log."""

    NAME = 'batchgen'

    def __init__(self, worker, settings, stop_event, no_batches=False, **kwargs):
        super(BatchGenerator, self).__init__(worker, settings, stop_event, **kwargs)
        if no_batches:
            raise NotConfigured('BatchGenerator is disabled with --no-batches')

        self.run_backoff = settings.get('NEW_BATCH_DELAY')
        self.backend = worker.backend
        self.spider_feed = worker.message_bus.spider_feed()
        self.spider_feed_producer = self.spider_feed.producer()

        self.get_key_function = self.get_fingerprint
        if settings.get('QUEUE_HOSTNAME_PARTITIONING'):
            self.get_key_function = self.get_hostname

        self.domains_blacklist = settings.get('DOMAINS_BLACKLIST')
        self.max_next_requests = settings.MAX_NEXT_REQUESTS
        # create an event to disable/enable batches generation via RPC
        self.disabled_event = threading.Event()

    def run(self):
        if self.disabled_event.is_set():
            return True

        partitions = self.spider_feed.available_partitions()
        if not partitions:
            return True
        self.logger.info("Getting new batches for partitions %s",
                         str(",").join(map(str, partitions)))

        count = 0
        for request in self.backend.get_next_requests(self.max_next_requests,
                                                      partitions=partitions):
            if self._is_domain_blacklisted(request):
                continue
            try:
                request.meta[b'jid'] = self.worker.job_id
                eo = self.worker._encoder.encode_request(request)
            except Exception as e:
                self.logger.error("Encoding error, %s, fingerprint: %s, url: %s" %
                                  (e, self.get_fingerprint(request), request.url))
                continue
            else:
                self.spider_feed_producer.send(self.get_key_function(request), eo)
            finally:
                count += 1
        if not count:
            return True
        self.update_stats(increments={'pushed_since_start': count, 'batches_after_start': 1},
                          replacements={'last_batch_size': count,
                                        'last_batch_generated': asctime()})

    def _is_domain_blacklisted(self, request):
        if not self.domains_blacklist:
            return
        if 'domain' in request.meta:
            hostname = request.meta['domain'].get('name')
        else:
            _, hostname, _, _, _, _ = parse_domain_from_url_fast(request.url)
        if hostname:
            hostname = hostname.lower()
            if hostname in self.domains_blacklist:
                self.logger.debug("Dropping black-listed hostname, URL %s", request.url)
                return True
        return False

    def close(self):
        self.spider_feed_producer.close()

    # --------------------------- Auxiliary tools --------------------------------

    def get_fingerprint(self, request):
        return request.meta[b'fingerprint']

    def get_hostname(self, request):
        try:
            _, hostname, _, _, _, _ = parse_domain_from_url_fast(request.url)
        except Exception as e:
            self.logger.error("URL parsing error %s, fingerprint %s, url %s" %
                              (e, request.meta[b'fingerprint'], request.url))
        else:
            return hostname.encode('utf-8', 'ignore')
