#!/bin/python
from __future__ import with_statement
import re
import os
import sys
import threading
import calendar
import datetime
import email
import Queue
import mimetypes
import logging
import binascii
from boto.s3.connection import S3Connection
from assetman.tools import make_output_path, make_static_path, get_static_pattern

class S3UploadThread(threading.Thread):
    """Thread that knows how to read asset file names from a queue and upload
    them to S3. Any exceptions encountered will be added to a shared errors
    list.

    Each asset will be uploaded twice:  Once with a special "/cdn/" prefix for
    assets to be served by a proxy on our own domain, and again without the
    prefix for assets to be served via CloudFront.  For the second upload,
    static URLs inside each asset will have to be rewritten *again* to point
    at CloudFront instead of our local CDN proxy.
    """

    def __init__(self, queue, errors, manifest, settings):
        threading.Thread.__init__(self)
        cx = S3Connection(settings.get('aws_access_key'), settings.get('aws_secret_key'))
        self.bucket = cx.get_bucket(settings.get('s3_assets_bucket'))
        self.queue = queue
        self.errors = errors
        self.manifest = manifest
        self.settings = settings

    def run(self):
        while True:
            file_name, file_path = self.queue.get()
            try:
                self.start_upload_file(file_name, file_path)
            except Exception, e:
                logging.error('Error uploading %s: %s', file_name, e)
                self.errors.append((sys.exc_info(), self))
            finally:
                self.queue.task_done()

    def start_upload_file(self, file_name, file_path):
        """Starts the procecss of uploading a file to S3. Each file will be
        uploaded twice (once for CDN and once for our local CDN proxy).
        """
        assert isinstance(file_name, (str, unicode))
        assert isinstance(file_path, (str, unicode))
        assert os.path.isfile(file_path)

        content_type, content_encoding = mimetypes.guess_type(file_name)
        if not content_type:
            content_type = 'application/octet-stream'
        headers = {
            'Content-Type': content_type,
            'Expires': self.get_expires(),
            'Cache-Control': self.get_cache_control(),
            'x-amz-acl': 'public-read',
        }

        with open(file_path, 'rb') as f:
            file_data = f.read()
            # First we will upload the asset for serving via CloudFront CDN,
            # so its S3 key will not have a prefix.
            key = self.bucket.new_key(file_name)
            self.upload_file(key, file_data, headers, for_cdn=True)

            # Next we will upload the same file with a prefixed key, to be
            # served by our "local CDN proxy".
            key_prefix = self.settings.get('local_cdn_url_prefix').lstrip('/').rstrip('/')
            key = self.bucket.new_key(key_prefix + '/' + file_name)
            self.upload_file(key, file_data, headers, for_cdn=False)

    def upload_file(self, key, file_data, headers, for_cdn):
        """Uploads the given file_data to the given S3 key. If the file is a
        compiled asset (ie, JS or CSS file), any static URL references it
        contains will be rewritten before upload.

        If use_cdn is True, static URL references will be updated to point to
        our CloudFront CDN domains. Otherwise, they will be updated to point
        to our local CDN proxy.
        """
        if not key.exists():
            # Do we need to do URL replacement?
            if re.search(r'\.(css|js)$', key.name):
                if for_cdn:
                    logging.info('Rewriting URLs => CDN: %s', key.name)
                    replacement_prefix = self.settings.get('cdn_url_prefix')
                else:
                    logging.info('Rewriting URLs => local proxy: %s', key.name)
                    replacement_prefix = self.settings.get('local_cdn_url_prefix')
                file_data = sub_static_version(
                    file_data,
                    self.manifest,
                    replacement_prefix,
                    self.settings.get('static_url_prefix'))
            key.set_contents_from_string(file_data, headers, replace=False)
            logging.info('Uploaded %s', key.name)
            logging.debug('Headers: %r', headers)
        else:
            logging.info('Skipping %s; already exists', key.name)

    def get_expires(self):
        # Get a properly formatted date and time, via Tornado's set_header()
        dt = datetime.datetime.utcnow() + datetime.timedelta(days=365*10)
        t = calendar.timegm(dt.utctimetuple())
        return email.utils.formatdate(t, localtime=False, usegmt=True)


    def get_cache_control(self):
        return 'public, max-age=%s' % (86400 * 365 * 10)


def upload_assets(manifest, settings, skip=False):
    """Uploads any assets that are in the given manifest and in our compiled
    output dir but missing from our static assets bucket to that bucket on S3.
    """

    # We will gather a set of (file_name, file_path) tuples to be uploaded
    to_upload = set()

    # We know we want to upload each asset block (these correspond to the
    # assetman.include_* blocks in each template)
    for depspec in manifest['blocks'].itervalues():
        file_name = depspec['versioned_path']
        file_path = make_output_path(file_name)
        assert os.path.isfile(file_path), 'Missing compiled asset %s' % file_path
        to_upload.add((file_name, file_path))

    # And we know that we'll want to upload any statically-referenced assets
    # (from assetman.static_url calls or referenced in any compiled assets),
    # but we'll need to filter out other entries in the complete 'assets'
    # block of the manifest.
    should_skip = re.compile(r'\.(scss|less|css|js|html)$', re.I).search
    for file_path, depspec in manifest['assets'].iteritems():
        if should_skip(file_path):
            continue
        assert os.path.isfile(file_path), 'Missing static asset %s' % file_path
        file_name = depspec['versioned_path']
        to_upload.add((file_name, file_path))

    logging.info('Found %d assets to upload to S3', len(to_upload))
    if skip:
        logging.warn('NOTE: Skipping uploads to S3')
        return True

    # Upload assets to S3 using 5 threads
    queue = Queue.Queue()
    errors = []
    for i in xrange(5):
        uploader = S3UploadThread(queue, errors, manifest, settings)
        uploader.setDaemon(True)
        uploader.start()
    map(queue.put, to_upload)
    queue.join()
    return len(errors) == 0

def get_shard_from_list(settings_list, shard_id):
    assert isinstance(settings_list, (list, tuple)), "must be a list not %r" % settings_list
    shard_id = _crc(shard_id)
    bucket = shard_id % len(settings_list)
    return settings_list[bucket]

def _crc(key):
    """crc32 hash a string"""
    return binascii.crc32(_utf8(key)) & 0xffffffff

def _utf8(s):
    """encode a unicode string as utf-8"""
    if isinstance(s, unicode):
        return s.encode("utf-8")
    assert isinstance(s, str), "_utf8 expected a str, not %r" % type(s)
    return s


def sub_static_version(src, manifest, replacement_prefix, static_url_prefix):
    """Adjusts any static URLs in the given source to point to a different
    location.

    Static URLs are determined based on the the 'static_url_prefix' setting.
    They will be updated to point to the given replacement_prefix, which can
    be a string or a list of strings (in which case the actual replacement
    prefix will be chosen by sharding each asset's base name).
    """
    def replacer(match):
        prefix, rel_path = match.groups()
        path = make_static_path(rel_path)
        if path in manifest['assets']:
            versioned_path = manifest['assets'][path]['versioned_path']
            if isinstance(replacement_prefix, (list, tuple)):
                prefix = get_shard_from_list(replacement_prefix, versioned_path)
            else:
                prefix = replacement_prefix
            return prefix.rstrip('/') + '/' + versioned_path.lstrip('/')
        logging.warn('Missing path %s in manifest, using %s', path, match.group(0))
        return match.group(0)
    pattern = get_static_pattern(static_url_prefix)
    return re.sub(pattern, replacer, src)
