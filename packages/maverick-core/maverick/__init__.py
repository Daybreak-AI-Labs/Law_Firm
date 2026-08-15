"""Lightwork: recursive multi-agent swarm for long-horizon work."""

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    __version__ = _distribution_version("maverick-agent")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.1.7"

_LIGHTWORK_PREFIX = "LIGHTWORK_"
_MAVERICK_PREFIX = "MAVERICK_"


def _mirror_lightwork_env() -> None:
    """Mirror ``LIGHTWORK_*`` env vars onto their ``MAVERICK_*`` twins.

    Product-name compatibility shim: every ``MAVERICK_X`` setting can also
    be spelled ``LIGHTWORK_X``. ``setdefault`` means an explicitly-set
    ``MAVERICK_X`` always wins -- the mirror never overwrites, so the call
    is idempotent. Guarded so it can never break an import.
    """
    try:
        for key, value in list(os.environ.items()):
            if key.startswith(_LIGHTWORK_PREFIX):
                os.environ.setdefault(
                    _MAVERICK_PREFIX + key[len(_LIGHTWORK_PREFIX):], value
                )
    except Exception:
        pass  # never let env quirks break `import maverick`


_mirror_lightwork_env()


def _install_egress_guard() -> None:
    """Wrap the HTTP client send paths so the enterprise egress lock applies.

    Installed at import rather than from each entry point on purpose. The
    boundary previously depended on every connector author remembering to call
    ``enterprise_egress_denial``, and 87 of 94 modules that make outbound HTTP
    did not -- so the one property enterprise mode is sold on was false
    wherever someone forgot. Wiring that can be forgotten is the defect, and
    import is the only hook every code path capable of making a request has
    necessarily run.

    A no-op unless enterprise mode (or a compliance floor) is on, so the
    default zero-config posture is unchanged. Guarded so it can never break
    ``import maverick``; ``egress_guard.uninstall()`` reverses it.
    """
    try:
        from .egress_guard import install
        install()
    except Exception:  # failure-policy: best_effort
        pass


_install_egress_guard()
