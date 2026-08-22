"""Single-process least-privilege audiobook derivative worker."""
import argparse

from .audiobook_streaming import recover_interrupted_worker, run_forever, work_once


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(work_once())
    else:
        recover_interrupted_worker()
        run_forever()


if __name__ == "__main__":
    main()
