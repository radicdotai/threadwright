import ctypes
import logging
import sys

from threadwright import config, github, listener, worker

_PR_SET_DUMPABLE = 4


def hide_own_environ() -> None:
    if sys.platform != "linux":
        return
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise SystemExit("prctl(PR_SET_DUMPABLE) failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "listener":
        hide_own_environ()
        listener.run(config.load_listener())
    elif role == "worker":
        worker.run(config.load())
    elif role == "receiver":
        github.run_receiver()
    else:
        raise SystemExit("usage: threadwright listener|worker|receiver")


if __name__ == "__main__":
    main()
