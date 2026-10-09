"""Map a sample of a corpus survey's images: failures, time and region kind counts.

Usage: diskmap_corpus.py CORPUS ITEMS.json [--sample N] [--workers W] [--seed S]
"""

import argparse
import collections
import json
import multiprocessing
import os
import pathlib
import random
import time
import traceback

import numpy as np
from tqdm import tqdm

from nybulah import survey
from nybulah.analysis.diskmap import Cls, Kind, Stability, disk_map
from nybulah.formats import loads


def _map(job):
    root, item = job
    start = time.perf_counter()
    try:
        image = loads(survey.read_item(root, item), item[-1])
        r = disk_map(image, bins=256).regions
        odd = r[r["cls"] > Cls.STANDARD]
        kinds = collections.Counter(
            f"{Kind(k).name}/{Stability(s).name}"
            for k, s in zip(odd["kind"], odd["stability"])
        )
        return image.kind, time.perf_counter() - start, kinds, ""
    except Exception:  # pylint: disable=broad-exception-caught
        return "", time.perf_counter() - start, {}, traceback.format_exc()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus")
    ap.add_argument("items")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    items = [
        tuple(i)
        for i in json.loads(pathlib.Path(args.items).read_text(encoding="utf-8"))
    ]
    jobs = [
        (args.corpus, i) for i in random.Random(args.seed).sample(items, args.sample)
    ]
    total, seconds, failures = collections.Counter(), [], []
    with multiprocessing.get_context("spawn").Pool(args.workers) as pool:
        for _, took, kinds, error in tqdm(
            pool.imap_unordered(_map, jobs), total=len(jobs), unit="img"
        ):
            seconds.append(took)
            total.update(kinds)
            if error:
                failures.append(error)
    out = {
        "images": len(jobs),
        "failures": len(failures),
        "seconds": [round(float(q), 2) for q in np.quantile(seconds, [0.5, 0.99, 1])],
        "kinds": dict(total.most_common()),
    }
    print(json.dumps(out, indent=1))
    for error in failures[:5]:
        print(error)


if __name__ == "__main__":
    main()
