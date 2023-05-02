import os
import re
import socket
import struct
import asyncio
import logging
import argparse
import functools

from quectelmodem import QuectelModemManager, AtCommandError


RESP_BYTES_STILL_AVAIL = 0x61


def parse_cmdline():
    parser = argparse.ArgumentParser(description='APDU interface to SIM in modem')
    parser.add_argument('--modem_tty', help='Modem TTY', required=True)
    parser.add_argument('--apdu_port', help='Local port for LPAdesktop', default=11321)
    return parser.parse_args()


class AtError(Exception): pass


class ApduProxy:
    def __init__(self, args, at, urc_q):
        self._args = args
        self._at = at
        self._urc_q = urc_q

        self._serv = socket.socket()
        self._serv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._serv.bind(('0.0.0.0', args.apdu_port))
        self._serv.listen(5)

    async def reset_wait(self, wait=False):
        self._at.verify_ok(await self._at.do_cmd('AT+CFUN=0'))
        self._at.verify_ok(await self._at.do_cmd('AT+CFUN=4'))

        if not wait:
            return

        while True:
            urc = await self._urc_q.get()
            print('URC: %s' % (urc, ))
            if '+CPIN' in urc:
                break

    async def _wait_for_csim(self):
        while True:
            await asyncio.sleep(1)
            full_res = await self._at.do_cmd('AT+CSIM=10,"01C0000000"')

            try:
                self._at.verify_ok(full_res)
                break
            except AtCommandError as e:
                print(e)
                continue
        
    async def _do_apdu(self, apdu):
        # Logical channel thing
        if apdu[0] == 0:
            apdu = b'\x01' + apdu[1:]
        else:
            apdu = b'\x81' + apdu[1:]

        logging.debug('<<< %s', apdu.hex())

        apdu_hex = apdu.hex().upper()
        res = await self._at.do_cmd('AT+CSIM=%d,"%s"' % (len(apdu_hex), apdu_hex))
        self._at.verify_ok(res)

        res_data = re.match(r'\+CSIM:\ [0-9]+\,\"(.*)\"', res)
        if not res_data:
            raise AtError("No CSIM response")

        res_data = bytes.fromhex(res_data.groups()[0])
        if res_data[0] != RESP_BYTES_STILL_AVAIL:
            logging.debug('Immeidate >>> %s', res_data.hex())
            return res_data

        total = []

        while True:
            full_res = await self._at.do_cmd('AT+CSIM=10,"01C0000000"')
            self._at.verify_ok(full_res)

            full_res_data = re.match(r'\+CSIM:\ [0-9]+\,\"(.*)\"', full_res)
            if not full_res_data:
                raise AtError("No CSIM response for GET RESPONSE")

            full_res_data = bytes.fromhex(full_res_data.groups()[0])
            logging.debug('>>> %s', full_res_data.hex())
            total.append(full_res_data[:-2])

            if full_res_data[-2] != RESP_BYTES_STILL_AVAIL:
                break

        return b''.join(total)


    async def run(self):
        self._at.verify_ok(await self._at.do_cmd('AT+COPS=2'))
        await self.reset_wait(wait=False)

        loop = asyncio.get_event_loop()
        while True:
            conn, _ = await loop.sock_accept(self._serv)
            print(' [+] Got conn')
            await self._wait_for_csim()

            try:
                while True:
                    buf = await loop.sock_recv(conn, 2)
                    if not buf:
                        break

                    size, = struct.unpack('>H', buf)
                    buf = await loop.sock_recv(conn, size)
                    if not buf:
                        break
                    print(' [+] Got APDU with size %d' % (size,))

                    res = await self._do_apdu(buf)
                    await loop.sock_sendall(conn, struct.pack('>H', len(res)) + res)
                    print(' [+] Send response with size %d' % (len(res), ))

            finally:
                print(' [-] Client disconnect')
                conn.close()

        return False


async def main():
    logging.basicConfig(level=logging.DEBUG)

    args = parse_cmdline()
    at = QuectelModemManager(
        args.modem_tty,
        extra_initer=functools.partial(ApduProxy, args)
    )

    await asyncio.gather(at.run())


if __name__ == '__main__':
    asyncio.run(main())
