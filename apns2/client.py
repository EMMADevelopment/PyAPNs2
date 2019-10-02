import collections
import json
import logging
import time
import weakref

from json import JSONEncoder
from typing import Deque, Dict, Iterable, Optional, Type, Union
from enum import Enum
from threading import Thread
from .errors import ConnectionFailed

# We don't generally need to know about the Credentials subclasses except to
# keep the old API, where APNsClient took a cert_file
from .credentials import CertificateCredentials, Credentials
from .response import Response, BatchResponse
from .payload import Payload


class NotificationPriority(Enum):
    Immediate = '10'
    Delayed = '5'


RequestStream = collections.namedtuple('RequestStream', ['stream_id', 'token'])
Notification = collections.namedtuple('Notification', ['token', 'payload'])

DEFAULT_APNS_PRIORITY = NotificationPriority.Immediate
CONCURRENT_STREAMS_SAFETY_MAXIMUM = 1000
MAX_CONNECTION_RETRIES = 3

logger = logging.getLogger(__name__)


class APNsOptions(object):
    def __init__(self, use_sandbox, use_alternative_port, proto, proxy_host, proxy_port,
                 json_encoder, heartbeat_period):

        self.use_sandbox = use_sandbox
        self.use_alternative_port = use_alternative_port
        self.proto = proto
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.json_encoder = json_encoder
        self.heartbeat_period = heartbeat_period


class APNsClient(object):

    def __init__(
            self,
            credentials:  Union[Credentials, str],
            use_sandbox: bool = False,
            use_alternative_port: bool = False,
            proto: Optional[str] = None,
            json_encoder: Optional[Type[JSONEncoder]] = None,
            password: Optional[str] = None,
            proxy_host: Optional[str] = None,
            proxy_port: Optional[int] = None,
            heartbeat_period: Optional[float] = None
    ) -> None:
        if credentials is None or isinstance(credentials, str):
            self.__credentials = CertificateCredentials(credentials, password)  # type: Credentials
        else:
            self.__credentials = credentials

        self.__options = APNsOptions(use_sandbox, use_alternative_port, proto, proxy_host, proxy_port,
                                     json_encoder, heartbeat_period)

    def create_connection(self):
        connection = APNsConnection(self.__credentials, self.__options)
        return connection


class APNsConnection(object):

    SANDBOX_SERVER = 'api.sandbox.push.apple.com'
    LIVE_SERVER = 'api.push.apple.com'

    DEFAULT_PORT = 443
    ALTERNATIVE_PORT = 2197

    def __init__(self, credentials: Union[Credentials, str], options: APNsOptions):
        self.__credentials = credentials
        self.__options = options
        self._connection = None
        self.__max_concurrent_streams = 0
        self.__previous_server_max_concurrent_streams = None

    def connect(self):
        server = self.SANDBOX_SERVER if self.__options.use_sandbox else self.LIVE_SERVER
        port = self.ALTERNATIVE_PORT if self.__options.use_alternative_port else self.DEFAULT_PORT
        self._connection = self.__credentials.create_connection(server, port, self.__options.proto,
                                                                self.__options.proxy_host, self.__options.proxy_port)

        if self.__options.heartbeat_period:
            self._start_heartbeat()

    def send_notification(self, token_hex: str, notification: Payload, topic: Optional[str] = None,
                          priority: NotificationPriority = NotificationPriority.Immediate,
                          expiration: Optional[int] = None, collapse_id: Optional[str] = None) -> Response:
        if not self.is_connected():
            raise ConnectionFailed("Can't send push. "
                                   "The connection with APNs has not been established: Use .connect() method")

        stream_id = self.send_notification_async(token_hex, notification, topic, priority, expiration, collapse_id)
        result = self.get_notification_result(stream_id, token_hex)
        return result

    def send_notification_async(self, token_hex: str, notification: Payload, topic: Optional[str],
                                priority=NotificationPriority.Immediate,
                                expiration: Optional[int] = None, collapse_id: Optional[str] = None) -> int:

        if not self.is_connected():
            raise ConnectionFailed("Can't send push. "
                                   "The connection with APNs has not been established: Use .connect() method")

        json_str = json.dumps(notification.dict(), cls=self.__options.json_encoder, ensure_ascii=False, separators=(',', ':'))
        json_payload = json_str.encode('utf-8')

        headers = {}
        if topic is not None:
            headers['apns-topic'] = topic

        if priority != DEFAULT_APNS_PRIORITY:
            headers['apns-priority'] = priority.value

        if expiration is not None:
            headers['apns-expiration'] = '%d' % expiration

        auth_header = self.__credentials.get_authorization_header(topic)
        if auth_header is not None:
            headers['authorization'] = auth_header

        if collapse_id is not None:
            headers['apns-collapse-id'] = collapse_id

        url = '/3/device/{}'.format(token_hex)
        stream_id = self._connection.request('POST', url, json_payload, headers)
        return stream_id

    def get_notification_result(self, stream_id, token) -> Response:
        apns_response = self._connection.get_response(stream_id)

        apns_ids = apns_response.headers["apns-id"]
        apns_id = apns_ids[0] if apns_ids else None
        response = Response(status_code=apns_response.status, apns_id=apns_id, token= token)

        try:
            if apns_response.status != 200:
                raw_data = apns_response.read().decode('utf-8')
                data = json.loads(raw_data) # type: Dict[str, str]
                response.reason = data["reason"]
                response.timestamp = data.get("timestamp")

        except Exception as e:
            response.error = e

        return response

    def is_connected(self):
        return self._connection is not None

    def close(self):
        self._connection.close()
        self._connection = None

    def send_notification_batch(self, notifications: Iterable[Notification], topic: Optional[str] = None,
                                priority: NotificationPriority = NotificationPriority.Immediate,
                                expiration: Optional[int] = None, collapse_id: Optional[str] = None) -> BatchResponse:
        """
        Send a notification to a list of tokens in batch. Instead of sending a synchronous request
        for each token, send multiple requests concurrently. This is done on the same connection,
        using HTTP/2 streams (one request per stream).

        APNs allows many streams simultaneously, but the number of streams can vary depending on
        server load. This method reads the SETTINGS frame sent by the server to figure out the
        maximum number of concurrent streams. Typically, APNs reports a maximum of 500.

        The function returns a dictionary mapping each token to its result. The result is "Success"
        if the token was sent successfully, or the string returned by APNs in the 'reason' field of
        the response, if the token generated an error.
        """

        if not self.is_connected():
            raise ConnectionFailed("Can't send push. "
                                   "The connection with APNs has not been established: Use .connect() method")

        notification_iterator = iter(notifications)
        next_notification = next(notification_iterator, None)
        # Make sure we're connected to APNs, so that we receive and process the server's SETTINGS
        # frame before starting to send notifications.
        self._connect()

        results = BatchResponse()

        open_streams = collections.deque() # type: Deque[RequestStream]
        # Loop on the tokens, sending as many requests as possible concurrently to APNs.
        # When reaching the maximum concurrent streams limit, wait for a response before sending
        # another request.
        while len(open_streams) > 0 or next_notification is not None:
            # Update the max_concurrent_streams on every iteration since a SETTINGS frame can be
            # sent by the server at any time.
            self.update_max_concurrent_streams()
            if next_notification is not None and len(open_streams) < self.__max_concurrent_streams:
                logger.info('Sending to token %s', next_notification.token)
                stream_id = self.send_notification_async(next_notification.token, next_notification.payload, topic,
                                                         priority, expiration, collapse_id)
                open_streams.append(RequestStream(stream_id, next_notification.token))

                next_notification = next(notification_iterator, None)
                if next_notification is None:
                    # No tokens remaining. Proceed to get results for pending requests.
                    logger.info('Finished sending all tokens, waiting for pending requests.')
            else:
                # We have at least one request waiting for response (otherwise we would have either
                # sent new requests or exited the while loop.) Wait for the first outstanding stream
                # to return a response.
                pending_stream = open_streams.popleft()
                result = self.get_notification_result(pending_stream.stream_id, pending_stream.token)
                if result.status_code == 200:
                    results.append_ok(result)
                else:
                    results.append_ko(result)

                logger.info('Got response for %s: %s', pending_stream.token, result)

        return results

    def update_max_concurrent_streams(self) -> None:
        # Get the max_concurrent_streams setting returned by the server.
        # The max_concurrent_streams value is saved in the H2Connection instance that must be
        # accessed using a with statement in order to acquire a lock.
        # pylint: disable=protected-access
        with self._connection._conn as connection:
            max_concurrent_streams = connection.remote_settings.max_concurrent_streams

        if max_concurrent_streams == self.__previous_server_max_concurrent_streams:
            # The server hasn't issued an updated SETTINGS frame.
            return

        self.__previous_server_max_concurrent_streams = max_concurrent_streams
        # Handle and log unexpected values sent by APNs, just in case.
        if max_concurrent_streams > CONCURRENT_STREAMS_SAFETY_MAXIMUM:
            logger.warning('APNs max_concurrent_streams too high (%s), resorting to default maximum (%s)',
                           max_concurrent_streams, CONCURRENT_STREAMS_SAFETY_MAXIMUM)
            self.__max_concurrent_streams = CONCURRENT_STREAMS_SAFETY_MAXIMUM
        elif max_concurrent_streams < 1:
            logger.warning('APNs reported max_concurrent_streams less than 1 (%s), using value of 1',
                           max_concurrent_streams)
            self.__max_concurrent_streams = 1
        else:
            logger.info('APNs set max_concurrent_streams to %s', max_concurrent_streams)
            self.__max_concurrent_streams = max_concurrent_streams

    def _connect(self) -> None:
        """
        Establish a connection to APNs. If already connected, the function does nothing. If the
        connection fails, the function retries up to MAX_CONNECTION_RETRIES times.
        """
        retries = 0
        while retries < MAX_CONNECTION_RETRIES:
            # noinspection PyBroadException
            try:
                self._connection.connect()
                logger.info('Connected to APNs')
                return
            except Exception:  # pylint: disable=broad-except
                # close the connnection, otherwise next connect() call would do nothing
                self._connection.close()
                retries += 1
                logger.exception('Failed connecting to APNs (attempt %s of %s)', retries, MAX_CONNECTION_RETRIES)

        raise ConnectionFailed()

    def _start_heartbeat(self) -> None:
        conn_ref = weakref.ref(self._connection)

        def watchdog() -> None:
            while True:
                conn = conn_ref()
                if conn is None:
                    break

                conn.ping('-' * 8)
                time.sleep(self.__options.heartbeat_period)

        thread = Thread(target=watchdog)
        thread.setDaemon(True)
        thread.start()
