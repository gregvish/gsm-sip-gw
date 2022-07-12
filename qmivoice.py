import re
import signal
import asyncio
import logging
import subprocess
import contextlib


CID_PATTERN = re.compile(rb'.*\sCID\:\s\'(\d+)\'.*', re.MULTILINE | re.DOTALL)
logger = logging.getLogger('QmiVoice')


class QmiVoiceException(Exception):
    pass

class QmiNetworkException(Exception):
    pass


class QmiVoice:
    '''
    Wraps the qmicli utility by parsing its output
    '''
    def __init__(self, device):
        self._device = device

    def _release_cid(self, cid):
        subprocess.run(
            ['qmicli', '-d', self._device, '--client-cid', str(cid), '--voice-noop']
        )

    @contextlib.contextmanager
    def alloc_cid(self):
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

    async def _follow_network(self, is_running_event):
        await is_running_event.wait()

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

        raise QmiNetworkException('Network qmicli died')

    def network_task(self, is_running_event):
        return asyncio.create_task(self._follow_network(is_running_event))

