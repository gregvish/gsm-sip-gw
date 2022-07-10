import re
import logging
import subprocess
import contextlib


CID_PATTERN = re.compile(rb'.*\sCID\:\s\'(\d+)\'.*', re.MULTILINE | re.DOTALL)
logger = logging.getLogger('QmiVoice')


class QmiVoiceException(Exception):
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

