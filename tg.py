import requests
import logging


logger = logging.getLogger('TgForwarder')


class TgForwarder:
    def __init__(self, bot_auth, chat_id):
        self._bot_auth = bot_auth
        self._chat_id = chat_id
        logger.info('TG forwarder')

    def forward(self, callerid, msg):
        msg_text = 'SMS from: %s\n%s' % (callerid, msg)
        requests.post(
            'https://api.telegram.org/%s/sendMessage' % (self._bot_auth, ),
            json={"chat_id": self._chat_id, "text": msg_text}
        )
        logger.info('Forwarded a msg to TG from %s' % (callerid, ))

