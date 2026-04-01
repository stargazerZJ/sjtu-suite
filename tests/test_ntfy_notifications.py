from email.header import decode_header
from unittest import TestCase
from unittest.mock import Mock

from sjtusuite.notifications.ntfy import NtfyNotifier


def _decode_mime_header(value: str) -> str:
    parts = decode_header(value)
    decoded_parts: list[str] = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded_parts.append(content.decode(charset or "ascii"))
        else:
            decoded_parts.append(content)
    return "".join(decoded_parts)


class TestNtfyNotifierHeaders(TestCase):
    def test_ascii_title_and_tags_keep_plain_headers(self) -> None:
        notifier = NtfyNotifier(topic="test-topic")
        response = Mock()
        response.status_code = 200
        response.text = "ok"
        response.headers = {}
        notifier.session.post = Mock(return_value=response)

        notifier.send("body", title="Booking ok", tags=["sports", "success"])

        _, kwargs = notifier.session.post.call_args
        headers = kwargs["headers"]
        self.assertEqual(headers["Title"], "Booking ok")
        self.assertEqual(headers["Tags"], "sports,success")

    def test_non_ascii_title_is_mime_encoded(self) -> None:
        notifier = NtfyNotifier(topic="test-topic")
        response = Mock()
        response.status_code = 200
        response.text = "ok"
        response.headers = {}
        notifier.session.post = Mock(return_value=response)

        title = "网球 🎾 预约成功"
        notifier.send("body", title=title)

        _, kwargs = notifier.session.post.call_args
        encoded_title = kwargs["headers"]["Title"]
        self.assertNotEqual(encoded_title, title)
        self.assertEqual(_decode_mime_header(encoded_title), title)

    def test_non_ascii_tags_are_mime_encoded(self) -> None:
        notifier = NtfyNotifier(topic="test-topic")
        response = Mock()
        response.status_code = 200
        response.text = "ok"
        response.headers = {}
        notifier.session.post = Mock(return_value=response)

        tags = ["sports", "预约成功", "sports"]
        notifier.send("body", tags=tags)

        _, kwargs = notifier.session.post.call_args
        encoded_tags = kwargs["headers"]["Tags"]
        self.assertEqual(_decode_mime_header(encoded_tags), "sports,预约成功")
