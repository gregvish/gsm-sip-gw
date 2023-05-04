import asyncio
import logging
import argparse
import functools

from qmi import QmiManager
from quectelmodem import QuectelModemManager


def parse_cmdline():
    parser = argparse.ArgumentParser(description='Connect via modem')
    parser.add_argument('--modem_tty', help='Modem TTY', required=True)
    parser.add_argument('--modem_dev', help='Modem device for QMI', required=True)
    parser.add_argument('--pin', help='Explicitly given PIN', default=None)
    parser.add_argument('--preferred_network', help='GSM/UMTS/LTE', default='LTE')
    parser.add_argument('--network', help='Start QMI network', type=bool, default=False)
    parser.add_argument('--apn', help='APN', default=None, required=False)
    return parser.parse_args()


async def main():
    logging.basicConfig(level=logging.INFO)

    def sms_forwarder(number, text):
        class Cls:
            async def send(self):
                logging.info('SMS from %s: %s', number, text)
                await asyncio.sleep(1)
        return Cls()

    def call_forwarder(number, connected_cb, ended_cb):
        class Cls:
            async def run(self):
                logging.info('Rejecting call from %s', number)
                await asyncio.sleep(1)
                await ended_cb()
        return Cls()

    args = parse_cmdline()
    modem = QuectelModemManager(
        args.modem_tty,
        sim_card_pin=args.pin,
        preferred_network=args.preferred_network,
        disregard_volte=True,
        sms_forwarder=sms_forwarder,
        call_forwarder=call_forwarder,
        apn=args.apn,
    )
    qmi = QmiManager(args.modem_dev, modem.is_running_event)

    with qmi.alloc_voice_cid():
        tasks = [modem.run()] + ([qmi.network_task()] if args.network else [])
        await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
