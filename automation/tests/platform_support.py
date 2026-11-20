"""Run the Linux/Wine-path tests on any OS; skip ones needing POSIX kernels.

`linux_path` patches `platform_compat.IS_WINDOWS` to False, so modules take
their Linux branch (X11 sender, /proc readers, ss parsing) while the tests'
mocks stand in for the OS. `posix_only` skips tests that need real POSIX
features (process groups, getsid, O_NOFOLLOW) on Windows.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import platform_compat as pc

ON_WINDOWS = os.name == 'nt'


def linux_path(target):
    """Class or function decorator: exercise the Linux code path."""
    return patch.object(pc, 'IS_WINDOWS', False)(target)


posix_only = unittest.skipIf(ON_WINDOWS, 'needs POSIX process groups / O_NOFOLLOW')


def _symlinks_work():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(os.path.join(tmp, 'a'), os.path.join(tmp, 'b'))
            return True
        except (OSError, NotImplementedError):
            return False


needs_symlinks = unittest.skipUnless(
    not ON_WINDOWS or _symlinks_work(),
    'creating symlinks needs Developer Mode or admin rights on Windows')
