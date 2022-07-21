import time
import asyncio
import logging
import contextlib

from application.notification import NotificationCenter
from sipsimple.account import Account
from sipsimple.application import SIPApplication
from sipsimple.storage import FileStorage
from sipsimple.core import SIPURI, ToHeader, Message, FromHeader, RouteHeader
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.session import Session
from sipsimple.streams.rtp.audio import AudioStream
from sipsimple.threading.green import run_in_green_thread
from sipsimple.configuration.datatypes import STUNServerAddress


logger = logging.getLogger('SIP')

STUN_SERVER = STUNServerAddress('stun.linphone.org')
MSG_STATUS_ACCEPTED = 202


class TsFuture(asyncio.Future):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

    def set_result(self, res):
        self._loop.call_soon_threadsafe(super().set_result, res)

    def set_exception(self, ex):
        self._loop.call_soon_threadsafe(super().set_exception, ex)

    def cancel(self):
        self._loop.call_soon_threadsafe(super().cancel)


class SIPMessageError(Exception):
    pass


class SIPClient(SIPApplication):
    def __init__(self, local_country_code):
        SIPApplication.__init__(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(self)

        self._did_app_start = TsFuture()
        self._accounts = {}
        self._session = None
        self._local_country_code = local_country_code
        self.rang = False

    def start(self, callee):
        self._callee_uri = callee
        super().start(FileStorage('sipconfig'))

    @run_in_green_thread
    def _NH_SIPApplicationDidStart(self, notification):
        self._callee = ToHeader(SIPURI.parse(self._callee_uri))
        try:
            routes = DNSLookup().lookup_sip_proxy(
                self._callee.uri, ['udp', 'tls']
            ).wait()
        except DNSLookupError as e:
            self._did_app_start.set_exception(e)
        else:
            self._routes = routes
            self._did_app_start.set_result(True)

    def _NH_SIPSessionGotRingIndication(self, notification):
        logger.info('Ringing!')
        self.rang = True

    def _NH_SIPSessionDidStart(self, notification):
        logger.info('Call connected, session started!')
        if not self._call_started.done():
            self._call_started.set_result(True)

    def _NH_SIPSessionDidFail(self, notification):
        logger.info('Call session connect failed')
        if not self._call_ended.done():
            self._call_ended.set_result(True)
        if not self._call_started.done():
            self._call_started.cancel()

    def _NH_SIPSessionDidEnd(self, notification):
        logger.info('Call session ended')
        if not self._call_ended.done():
            self._call_ended.set_result(True)
        if not self._call_started.done():
            self._call_started.cancel()

    def _NH_SIPMessageDidSucceed(self, notification):
        logger.info('Message was accepted by remote party')
        self._msg_sent.set_result(True)

    def _NH_SIPMessageDidFail(self, notification):
        if notification.data.code == MSG_STATUS_ACCEPTED:
            logger.info('Message is cached at the proxy. Hope for the best')
            self._msg_sent.set_result(True)
            return

        logger.info('Failed to deliver message: %d %s' % (
            notification.data.code, notification.data.reason)
        )
        self._msg_sent.set_exception(
            SIPMessageError(notification.data.code, notification.data.reason)
        )

    def _get_account(self, account_str):
        if account_str not in self._accounts:
            self._accounts[account_str] = Account(account_str)
        return self._accounts[account_str]

    def _callerid_to_account(self, callerid):
        if not callerid:
            callerid = 'Unknown'
        if self._local_country_code and callerid.startswith(self._local_country_code):
            callerid = callerid.replace(self._local_country_code, '0')

        account = self._get_account('%s@gsm' % callerid)

        account.display_name = callerid
        account.rtp.encryption.enabled = True
        account.rtp.encryption.key_negotiation = 'sdes_mandatory'
        #account.nat_traversal.use_ice = True
        #account.nat_traversal.stun_server_list = [STUN_SERVER]

        return account

    async def call(self, callerid):
        self.rang = False
        await self._did_app_start
        self._call_started = TsFuture()
        self._call_ended = TsFuture()

        self._session = Session(self._callerid_to_account(callerid))
        self._session.connect(self._callee, self._routes, [AudioStream()])

        await self._call_started

    async def end_call(self):
        if self._session:
            self._session.end()
            await self.wait_call()
            self._session = None

    async def wait_call(self):
        await self._call_ended

    async def message(self, callerid, msg):
        await self._did_app_start
        self._msg_sent = TsFuture()

        msg = Message(FromHeader(self._callerid_to_account(callerid).uri),
                      self._callee, RouteHeader(self._routes[0].uri),
                      'text/plain', msg)
        msg.send()
        await self._msg_sent

    @contextlib.contextmanager
    def context(self, callee):
        try:
            self.start(callee)
            yield
        finally:
            self.stop()


class SIPCallForwarder:
    def __init__(self, sip, callerid, connected_cb=None, ended_cb=None, call_timeout=90):
        self._sip = sip
        self._callerid = callerid
        self._connected_cb = connected_cb
        self._ended_cb = ended_cb
        self._call_timeout = call_timeout

    def run(self):
        return asyncio.create_task(self._call())

    async def _call(self):
        was_taken = False

        try:
            try:
                await asyncio.wait_for(self._sip.call(self._callerid),
                                       timeout=self._call_timeout)
            except asyncio.exceptions.TimeoutError:
                logger.info('Call timed out')
                return

            was_taken = True
            if self._connected_cb:
                await self._connected_cb()
            await self._sip.wait_call()

        finally:
            if self._ended_cb:
                await self._ended_cb()

            await self._sip.end_call()
            logger.info('Call ended')

            if was_taken:
                return
            logger.info('Notifying of missed call')
            await self._sip.message(self._callerid, 'Missed call at %s UTC %s' % (
                time.asctime(time.localtime()),
                '(It rang)' if self._sip.rang else ''
            ))


class SIPSmsForwarder:
    def __init__(self, sip, callerid, msg):
        self._sip = sip
        self._callerid = callerid
        self._msg = msg

    async def send(self):
        logger.info('Forwarding SMS from %s' % (self._callerid, ))
        await self._sip.message(self._callerid, self._msg)


