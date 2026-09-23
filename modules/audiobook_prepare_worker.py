"""Single-process least-privilege audiobook derivative worker."""
import argparse

from .audiobook_streaming import recover_interrupted_worker, run_forever, work_once, worker_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        with worker_lock():
            recover_interrupted_worker()
            if args.once:
                print(work_once())
            else:
                run_forever()
    except RuntimeError as error:
        if str(error) != "worker_already_running":
            raise
        raise SystemExit("audiobook preparation worker is already running")


if __name__ == "__main__":
    main()
