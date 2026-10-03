"""Download the pinned Kev checkpoint and its base model into HF_HOME (image build time).

    python prefetch.py <run> <release-date>

<run> is a Hub id pinned with @<revision>. Fetches what kev.serve loads: the adapter and head,
the base model's weights and config, and its tokenizer. Then sets the head.pt blob's time to
the release date: offline, Kev reports that file's time as the release date (GET /v1/models).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from kev.checkpoint import Checkpoint, load_tokenizer, resolve_run
from transformers import AutoConfig


def main() -> None:
    run, release_date = sys.argv[1], sys.argv[2]
    checkpoint = Checkpoint(run)
    meta = checkpoint.meta
    resolve_run(f"{meta.base}@{meta.base_revision or ''}")
    AutoConfig.from_pretrained(meta.base, revision=meta.base_revision)
    load_tokenizer(meta.base, revision=meta.base_revision)
    head = os.path.realpath(checkpoint.file("head.pt"))
    stamp = datetime.fromisoformat(release_date).timestamp()
    os.utime(head, (stamp, stamp))
    print(f"cached {run} on {meta.base}@{meta.base_revision or 'main'}")


if __name__ == "__main__":
    main()
