import re
import os
import asyncio
import logging
import argparse

import serial_asyncio


MODEM_BAUD = 115200
AT_SHORT_TIMEOUT = 0.2
AT_MEDIUM_TIMEOUT = 0.5
AT_LONG_TIMEOUT = 5
MIN_ALLOWED_UNLOCK_ATTEMPTS = 3
NETWORK_COPS_ATTEMPTS = 10
COPS_SLEEP = 2
COPS_PASSIVE_SCAN_TIMEOUT = 4 * 60
MANUAL_COPS_WAIT_SECONDS = 2 * 60
MAX_MESSAGES = 20
VOLTE_CHECK_ATTEMPTS = 20

NET_TYPES = {
    0: 'GSM',
    2: 'UMTS',
    7: 'LTE',
}
SCANMODE_FOR_NET_TYPE = {
    'GSM': 1,
    'UMTS': 2,
    'LTE': 3,
}
STATUS_REJECTED = 2

logger = logging.getLogger('QuectelModem')


class AtCommandError(Exception):
    pass
class AtStateError(Exception):
    pass
class NetworkError(Exception):
    pass


class QuectelModemManager:
    def __init__(self, modem_tty, modem_baud=MODEM_BAUD, call_forwarder=None,
                 sms_forwarder=None, sim_card_pin=None, preferred_network='LTE',
                 disregard_volte=False, extra_initer=None):
        self._call_forwarder = call_forwarder
        self._sms_forwarder = sms_forwarder
        self._modem_tty = modem_tty
        self._modem_baud = modem_baud
        self._extra_initer = extra_initer
        self._preferred_network = preferred_network
        self._disregard_volte = disregard_volte
        self.sim_card_pin = sim_card_pin

        self._last_cmd = b''
        self._response_q = asyncio.Queue()
        self._urc_q = asyncio.Queue()
        self._in_call = False
        self._call_fwd_task = None
        self._cur_csq = 0
        self.is_running_event = asyncio.Event()

    async def _reset_at(self):
        self._modem_w.write(b'\rATE\r')
        await asyncio.sleep(AT_MEDIUM_TIMEOUT)
        # Cleanout buffer
        while True:
            try:
                await asyncio.wait_for(self._modem_r.read(1), timeout=AT_MEDIUM_TIMEOUT)
            except asyncio.exceptions.TimeoutError:
                break

    async def _tty_rx_handler(self):
        async def getline(timeout=None):
            rx = await asyncio.wait_for(self._modem_r.readline(), timeout=timeout)
            return rx.strip()

        while True:
            line = await getline()

            # If the line isn't the echo of _last_cmd, treat it as a URC
            if not line.startswith(self._last_cmd) and line != b'':
                await self._urc_q.put(line.decode())
                continue

            elif line == b'':
                continue

            # Treat the line as the start of the response to _last_cmd
            lines = []
            while True:
                try:
                    # Append lines until there is a short RX timeout, or OK/ERROR
                    while True:
                        lines.append(await getline(timeout=AT_SHORT_TIMEOUT))
                        if lines[-1] in (b'OK', b'ERROR'):
                            break
                except asyncio.exceptions.TimeoutError:
                    pass

                # Try to send a new AT command to probe if last command finished
                self._modem_w.write(b'AT\r')
                line = await getline(timeout=AT_SHORT_TIMEOUT)

                # If we got AT back, get the OK too and finish
                if line == b'AT':
                    line = await getline(timeout=AT_SHORT_TIMEOUT)
                    if line == b'OK':
                        break
                    else:
                        line.append(line)
                else:
                    # Otherwise, this line is part of the response. Continue
                    lines.append(line)

            await self._response_q.put((b'\n'.join(lines)).decode())


    async def do_cmd(self, cmd, timeout=AT_LONG_TIMEOUT):
        self._last_cmd = cmd.encode()
        self._modem_w.write(b'%s\r' % (self._last_cmd,))
        result = await asyncio.wait_for(self._response_q.get(), timeout=timeout)
        logger.debug('%s -> %r' % (cmd, result))
        return result

    def verify_ok(self, result):
        if not result.endswith('OK'):
            raise AtCommandError(result)

    async def get_unlock_attempts(self):
        pin_counters = await self.do_cmd('AT+QPINC?')
        left, total = re.match(r'.*\"SC\",(\d+),(\d+)', pin_counters).groups()
        return int(left), int(total)

    async def sim_unlock(self, pin):
        left, total = await self.get_unlock_attempts()
        if left < MIN_ALLOWED_UNLOCK_ATTEMPTS:
            raise AtStateError(
                'SIM unlock attempts below %d (%d/%d)' % (
                    MIN_ALLOWED_UNLOCK_ATTEMPTS, left, total
                )
            )
        self.verify_ok(await self.do_cmd('AT+CPIN=%s' % (pin,)))

    async def _measure_csq(self):
        csq = await self.do_cmd('AT+CSQ')

        m = re.match(r'^\+CSQ\:\ (\d+),(\d+)', csq)
        if not m:
            return

        signal, unk = m.groups()
        signal, unk = int(signal), int(unk)
        if signal != self._cur_csq:
            logger.info('CSQ changed! %d -> %d (%d)' % (self._cur_csq, signal, unk))
            self._cur_csq = signal

    async def _wait_for_network(self, disregard_pref=False):
        connected = False

        for i in range(NETWORK_COPS_ATTEMPTS):
            await asyncio.sleep(COPS_SLEEP)

            cops = await self.do_cmd('AT+COPS?')
            m = re.match(r'^\+COPS\:\ (\d+),(\d+),(.*?),(\d+)', cops)
            await self._measure_csq()

            if not m:
                m = re.match(r'^\+COPS\:\ (\d+)', cops)
                if not m:
                    logger.warning('AT+COPS bad output: %r' % (cops, ))
                    continue

                status, = m.groups()
                if int(status) == STATUS_REJECTED:
                    logger.warning('AT+COPS got rejected status')
                    break
                continue

            status, _, operator, net_type = m.groups()
            status, net_type = int(status), int(net_type)
            logger.info('Network: %s (%s), status: %s' % (
                operator, NET_TYPES[net_type], status)
            )

            if disregard_pref or NET_TYPES[net_type] == self._preferred_network:
                connected = True
                break

        return connected

    async def _network_selection(self):
        logger.info('Waiting for network...')
        if await self._wait_for_network():
            logger.info('Auto-connected!')
            return

        self.verify_ok(await self.do_cmd('AT+COPS=2'))
        logger.warning('Passive scanning available networks...')

        all_cops = await self.do_cmd('AT+COPS=?', timeout=COPS_PASSIVE_SCAN_TIMEOUT)
        m = re.match(r'^\+COPS\: \((.*)\)\,\,', all_cops)
        if not m:
            raise NetworkError('Bad network list: %r' % (all_cops, ))
        nets = m.groups()[0].split('),(')
        if not nets:
            raise NetworkError('Empty network list: %r' % (all_cops, ))

        logger.info('Available networks:')
        net_dict = {t: [] for t in NET_TYPES.keys()}

        for net in nets:
            status, long_name, short_name, number, net_type = net.split(',')
            status, net_type = int(status), int(net_type)
            logger.info('    %s (%s) (%s) type: %s' % (
                long_name, short_name, number, NET_TYPES[net_type]
            ))
            net_dict[net_type].append((long_name, short_name, number))

        connected = False
        disregard_pref = False
        preferred = {v: k for k, v in NET_TYPES.items()}[self._preferred_network]
        net_types_to_try = list(sorted(NET_TYPES.keys(), reverse=True))
        net_types_to_try.remove(preferred)
        net_types_to_try.insert(0, preferred)

        while True:
            if not net_types_to_try:
                break

            cur_type = net_types_to_try[0]
            if not net_dict[cur_type]:
                net_types_to_try.remove(cur_type)
                disregard_pref = True
                continue

            long_name, _, _ = net_dict[cur_type].pop(0)
            logger.info('Trying %s (%s)' % (long_name, NET_TYPES[cur_type]))

            cops = await self.do_cmd('AT+COPS=1,0,%s,%d' % (long_name, cur_type),
                                     timeout=MANUAL_COPS_WAIT_SECONDS)
            await self._measure_csq()
            if 'ERROR' in cops:
                continue

            if await self._wait_for_network(disregard_pref):
                logger.info('Finally! Connected.')
                connected = True
                break

        if not connected:
            raise NetworkError('Failed connecting to all networks')

    async def _cfun_restart(self):
        self.verify_ok(await self.do_cmd('AT+CFUN=0'))
        self.verify_ok(await self.do_cmd('AT+CFUN=1'))

        while True:
            urc = await asyncio.wait_for(self._urc_q.get(), timeout=AT_LONG_TIMEOUT)
            logger.info('URC -> %r' % (urc,))

            if '+CPIN: SIM PIN' in urc:
                if not self.sim_card_pin:
                    raise AtStateError('SIM unlock needed but no PIN setup')

                await self.sim_unlock(self.sim_card_pin)

            elif 'PB DONE' in urc:
                break

    async def _reset(self):
        retval = True
        self.verify_ok(await self.do_cmd('AT'))
        self.verify_ok(await self.do_cmd('AT+QURCCFG="urcport","all"'))
        self.verify_ok(await self.do_cmd('ATH0'))

        if self._extra_initer:
            retval = await self._extra_initer(self, self._urc_q).run()

        scanmode = SCANMODE_FOR_NET_TYPE[self._preferred_network]
        self.verify_ok(await self.do_cmd('AT+QCFG="nwscanmode",%d' % (scanmode, )))

        await self._cfun_restart()
        self.verify_ok(await self.do_cmd('AT+CMGF=1'))
        self.verify_ok(await self.do_cmd('AT+CSDH=1'))
        self.verify_ok(await self.do_cmd('AT+CPMS="ME","ME","ME"'))

        await self._network_selection()
        return retval

    async def _handle_call(self):
        result = await self.do_cmd('AT+CLCC')

        for call in [c for c in result.split('\n') if c.startswith('+CLCC')]:
            call = call[len('+CLCC: '):]
            idx, dir, state, mode, multiparty, number, type = call.split(',')
            # Make sure it's a Voice call, Mobile Terminated and Incoming state
            if mode == '0' and dir == '1' and state == '4':
                break
        else:
            logger.warning('Tried to handle a bad call: %r' % ((mode, dir, state, number),))
            return

        self._in_call = True
        number = number.replace('"', '')
        logger.info('Got call! #%s, number: %s, type: %s' % (idx, number, type))

        async def call_ended_cb():
            self._in_call = False
            self._call_fwd_task = None
            logger.info('Call disconnected. Sending ATH0!')
            self.verify_ok(await self.do_cmd('ATH0'))

        async def call_connected_cb():
            logger.info('Call connected. Sending ATA!')
            self.verify_ok(await self.do_cmd('ATA'))

        self._call_fwd_task = self._call_forwarder(
            number, call_connected_cb, call_ended_cb
        ).run()

    async def _handle_sms(self):
        messages = []
        segmented_messages = {}

        for msg_index in range(MAX_MESSAGES):
            msg = await self._parse_sms_single(msg_index, segmented_messages)
            if msg:
                messages.append(msg)

        logger.info('Got %d ready SMS, %d segmented' % (
            len(messages), len(segmented_messages)
        ))

        for text, number, date, time, msg_indexes in messages:
            text = '%s %s\n%s' % (date, time, text)
            await self._sms_forwarder(number, text).send()

            for idx in msg_indexes:
                self.verify_ok(await self.do_cmd('AT+CMGD=%d,0' % idx))

    def _xlate_sms_number(self, number):
        # Is it an actual number?
        if number.startswith('+') or number.startswith('0'):
            return number

        res = []
        i = 0
        while i < len(number):
            if number[i] == '1':
                char = int(number[i: i+3])
                i += 3
            else:
                char = int(number[i: i+2])
                i += 2
            res.append(char)

        return bytes(res).decode()

    async def _parse_sms_single(self, msg_index, seg_dict):
        result = await self.do_cmd('AT+QCMGR=%d' % msg_index)
        self.verify_ok(result)
        msg_lines = [s for s in result.split('\n') if s]

        head_match = re.match(r'^\+QCMGR\:\ (.*?)$', msg_lines[0])
        if not head_match:
            return None

        head = head_match.groups()[0].split(',')
        head = [s.replace('"', '') for s in head]

        segmented = False
        _, number, _, date, time, _, _, _, _, _, _, size = head[:12]

        number = self._xlate_sms_number(number)

        # Could be a multi-part message. Has 3 extra fields in header
        if len(head) == 15:
            msg_uid, msg_seg, tot_seg = head[12:]
            msg_seg, tot_seg = int(msg_seg), int(tot_seg)
            segmented = True
            # Make 0 based
            msg_seg -= 1

        text = '\n'.join(msg_lines[1:])
        if text.endswith('OK'):
            text = text[:-2]
        text = text.strip()

        # Could be hex-encoded UTF16
        if size != len(text) and re.match(r'^[0-9A-F]+$', text) and len(text) % 2 == 0:
            text = bytes.fromhex(text).decode('utf-16-be')

        if not segmented:
            return text, number, date, time, [msg_index]

        if msg_uid not in seg_dict:
            seg_dict[msg_uid] = ([None] * tot_seg, [None] * tot_seg)
        seg_dict[msg_uid][0][msg_seg] = text
        seg_dict[msg_uid][1][msg_seg] = msg_index

        # Segmented message not complete yet
        if None in seg_dict[msg_uid][0]:
            return None

        return ''.join(seg_dict[msg_uid][0]), number, date, time, seg_dict[msg_uid][1]

    async def _check_volte(self):
        for i in range(VOLTE_CHECK_ATTEMPTS):
            res = await self.do_cmd('AT+QCFG="ims"')
            match = re.match(r'^\+QCFG\:\ \"ims\",(.*?),(.*?)$', res, re.MULTILINE)
            if not match:
                raise AtCommandError('Unexpected: %r', res)

            _, volte = match.groups()
            if volte == '1':
                logger.info('IMS registered (VoLTE)')
                break
            await asyncio.sleep(AT_MEDIUM_TIMEOUT)

        else:
            raise NetworkError('IMS not registered (no VoLTE): %r', res)

    async def _urc_handler(self):
        # On boot, look at potentially missed SMS
        await self._handle_sms()

        if self._preferred_network == 'LTE' and not self._disregard_volte:
            await self._check_volte()

        while True:
            urc = await self._urc_q.get()
            logger.info('URC -> %r' % (urc,))

            if 'RING' == urc and not self._in_call:
                await self._handle_call()

            if 'NO CARRIER' in urc and self._in_call:
                logger.info('Got GSM hangup. Cancelling call task!')
                self._call_fwd_task.cancel()

            elif '+CMTI:' in urc:
                await self._handle_sms()

            elif '+CPIN: NOT READY' in urc:
                raise AtStateError(urc)

            else:
                logger.warning('Uhandled URC: %r' % (urc,))

    async def run(self):
        self._modem_r, self._modem_w = await serial_asyncio.open_serial_connection(
            url=self._modem_tty, baudrate=self._modem_baud
        )

        await self._reset_at()
        rx_task = asyncio.create_task(self._tty_rx_handler())

        logger.info('Got AT shell to modem. Resetting')
        if not await self._reset():
            return

        self.is_running_event.set()
        urc_task = asyncio.create_task(self._urc_handler())
        await asyncio.gather(rx_task, urc_task)

