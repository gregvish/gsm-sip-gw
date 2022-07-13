import re
import time
import signal
import asyncio
import logging
import subprocess
import contextlib


NETWORK_QUICK_FAIL_TIMEOUT = 60
CID_PATTERN = re.compile(rb'.*\sCID\:\s\'(\d+)\'.*', re.MULTILINE | re.DOTALL)

logger = logging.getLogger('QmiManager')


class QmiVoiceException(Exception):
    pass

class QmiNetworkException(Exception):
    pass


class QmiManager:
    '''
    Wraps the qmicli utility by parsing its output
    '''
    def __init__(self, device, is_running_event):
        self._device = device
        self._is_running_event = is_running_event

    def _release_cid(self, cid):
        subprocess.run(
            ['qmicli', '-d', self._device, '--client-cid', str(cid), '--voice-noop']
        )

    @contextlib.contextmanager
    def alloc_voice_cid(self):
        # HACK: Allocate this CID first, so that set_current_host_app runs on openqti
        subprocess.run(['qmicli', '-d', self._device, '--dms-noop'])

        proc = subprocess.run(
            ['qmicli', '-d', self._device, '--client-no-release-cid', '--voice-noop'],
            check=True, capture_output=True
        )

        match = re.match(CID_PATTERN, proc.stdout)
        if not match:
            raise QmiVoiceException(proc.stdout)
        cid = int(match.groups()[0].decode())
        logger.info('QMI allocated voice CID: %d' % (cid,))

        try:
            yield
        finally:
            self._release_cid(cid)
            logger.info('QMI released voice CID: %d' % (cid,))

    async def _follow_network_once(self):
        proc = await asyncio.create_subprocess_shell(
            'qmicli --device=%s --wds-start-network="ip-type=4"' % (self._device, ) +
            ' --wds-follow-network | stdbuf -oL -eL uniq',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            logger.info('qmicli: %s' % (line.decode().strip(), ))

        await proc.wait()

    async def _follow_network(self):
        await self._is_running_event.wait()

        while True:
            start = time.monotonic()
            await self._follow_network_once()
            if time.monotonic() - start < NETWORK_QUICK_FAIL_TIMEOUT:
                raise QmiNetworkException('Network disconnected too quickly')

    def network_task(self):
        return asyncio.create_task(self._follow_network())

