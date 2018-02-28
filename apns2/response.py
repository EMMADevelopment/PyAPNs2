# coding: utf-8

class Response(object):

    def __init__(
        self, status_code, apns_id,
        timestamp=None, reason=None,
    ):

        # The HTTP status code retuened by APNs.
        # A 200 value indicates that the notification was successfully sent.
        # For a list of other possible status codes, see table 6-4 in the Apple Local
        # and Remote Notification Programming Guide.
        self.status_code = status_code

        # The APNs error string indicating the reason for the notification failure (if
        # any). The error code is specified as a string. For a list of possible
        # values, see the Reason constants above.
        # If the notification was accepted, this value will be "".
        self.reason = reason

        # The APNs ApnsID value from the Notification. If you didn't set an ApnsID on the
        # Notification, this will be a new unique UUID which has been created by APNs.
        self.apns_id = apns_id

        # If the value of StatusCode is 410, this is the last time at which APNs
        # confirmed that the device token was no longer valid for the topic.
        self.timestamp = timestamp