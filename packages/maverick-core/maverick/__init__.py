"""The firm's agent runtime: recursive multi-agent swarm for long-horizon work."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    __version__ = _distribution_version("maverick-agent")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.1.7"


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
