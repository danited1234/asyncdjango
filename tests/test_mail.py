"""asyncdjango.mail: SMTP from native code on aiosmtplib."""

import smtplib
import socket
import threading
import unittest
from unittest import mock

from aiosmtpd.controller import Controller
from django.core.mail import EmailMessage, get_connection, send_mail
from django.test import override_settings

from asyncdjango import sync_to_native
from asyncdjango.diagnostics import ThreadOffloadDetector
from asyncdjango.mail import NativeSMTP, asend_mail


class Recorder:
    def __init__(self):
        self.envelopes = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address.startswith("refused@"):
            return "550 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 OK"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MailTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = Recorder()
        cls.port = free_port()
        cls.controller = Controller(cls.handler, hostname="127.0.0.1", port=cls.port)
        cls.controller.start()
        cls.addClassCleanup(cls.controller.stop)

    async def asyncSetUp(self):
        self.handler.envelopes.clear()
        self.settings = override_settings(EMAIL_BACKEND="asyncdjango.mail.EmailBackend",
                                          EMAIL_HOST="127.0.0.1", EMAIL_PORT=self.port)
        self.settings.enable()
        self.offload = ThreadOffloadDetector()
        await self.offload.__aenter__()

    async def asyncTearDown(self):
        await self.offload.__aexit__(None, None, None)
        self.settings.disable()

    async def test_asend_mail_is_native(self):
        with mock.patch.object(NativeSMTP, "sendmail", autospec=True,
                               side_effect=NativeSMTP.sendmail) as native:
            sent = await asend_mail("Hi", "Body é", "from@example.com", ["to@example.com"])
        self.assertEqual((sent, native.call_count), (1, 1))
        envelope, = self.handler.envelopes
        self.assertEqual(envelope.mail_from, "from@example.com")
        self.assertEqual(envelope.rcpt_tos, ["to@example.com"])
        self.assertIn(b"Subject: Hi", envelope.content)
        self.assertEqual(self.offload.calls, [])

    async def test_sync_send_from_native_code_stays_on_the_loop(self):
        def send():
            message = EmailMessage("Hi", "Body", "from@example.com", ["a@example.com"])
            message.send()
            return threading.get_ident()

        with mock.patch.object(NativeSMTP, "sendmail", autospec=True,
                               side_effect=NativeSMTP.sendmail) as native:
            self.assertEqual(await sync_to_native(send)(), threading.get_ident())
        self.assertEqual((len(self.handler.envelopes), native.call_count), (1, 1))

    async def test_refused_recipients_raise_smtplib_errors(self):
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            await asend_mail("Hi", "Body", "from@example.com", ["refused@example.com"])
        sent = await asend_mail("Hi", "Body", "from@example.com", ["refused@example.com"],
                                fail_silently=True)
        self.assertEqual(sent, 0)

    async def test_unreachable_server(self):
        connection = get_connection(port=free_port())
        with self.assertRaises(OSError):
            await asend_mail("Hi", "Body", "from@example.com", ["to@example.com"],
                             connection=connection)
        connection = get_connection(port=free_port(), fail_silently=True)
        self.assertEqual(await asend_mail("Hi", "Body", "from@example.com",
                                          ["to@example.com"], connection=connection), 0)

    def test_synchronous_code_uses_smtplib(self):
        with (override_settings(EMAIL_BACKEND="asyncdjango.mail.EmailBackend",
                                EMAIL_HOST="127.0.0.1", EMAIL_PORT=self.port),
              mock.patch.object(NativeSMTP, "sendmail") as native):
            self.assertEqual(send_mail("Hi", "Body", "f@example.com", ["t@example.com"]), 1)
        self.assertEqual((len(self.handler.envelopes), native.call_count), (1, 0))
