import os
import asyncio
import logging
import argparse
import functools

from qmi import QmiManager
from sip import SIPClient, SIPCallForwarder, SIPSmsForwarder
from tg import TgForwarder
from quectelmodem import QuectelModemManager


logger = logging.getLogger('GsmGw')


def parse_cmdline():
    parser = argparse.ArgumentParser(description='GSM to SIP Gateway')
    parser.add_argument('--sip_dest', help='Target SIP URI', required=True)
    parser.add_argument('--tg_bot', help='Backup TG bot auth', required=False)
    parser.add_argument('--tg_chat', help='Backup TG chat ID', required=False)
    parser.add_argument('--modem_tty', help='TTY device of the modem for AT', required=True)
    parser.add_argument('--modem_dev', help='Modem device for QMI', required=True)
    parser.add_argument('--call_timeout', help='Timeout for ringing before hangup',
                        type=int, default=90)
    parser.add_argument('--sim_pin', help='SIM card PIN', default=None)
    parser.add_argument('--preferred_network', help='GSM/UMTS/LTE', default='LTE')
    parser.add_argument('--local_country_code', help='E.g. +972, to remove from caller ID',
                        default=None)
    parser.add_argument('--network', help='Start QMI network', type=bool, default=False)
    parser.add_argument('--disregard_volte', help='Ignore if VoLTE is unavaliable',
                        type=bool, default=False)
    parser.add_argument('--apn', help='APN', default=None, required=False)
    return parser.parse_args()


async def main():
    logging.basicConfig(level=logging.INFO)

    args = parse_cmdline()

    tg_fwd = None
    if args.tg_bot:
        tg_fwd = TgForwarder(args.tg_bot, args.tg_chat)

    sip = SIPClient(args.local_country_code, tg_fwd)

    with sip.context(args.sip_dest):
        logger.info('Created SIP client')

        call_fwd = functools.partial(
            SIPCallForwarder, sip, call_timeout=args.call_timeout
        )

        sms_fwd = functools.partial(SIPSmsForwarder, sip)

        modem_manager = QuectelModemManager(
            args.modem_tty,
            call_forwarder=call_fwd,
            sms_forwarder=sms_fwd,
            sim_card_pin=args.sim_pin,
            preferred_network=args.preferred_network,
            disregard_volte=args.disregard_volte,
            apn=args.apn,
        )

        qmi = QmiManager(args.modem_dev, modem_manager.is_running_event)
        with qmi.alloc_voice_cid():
            tasks = [modem_manager.run()]
            if args.network:
                tasks.append(qmi.network_task())

            await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
