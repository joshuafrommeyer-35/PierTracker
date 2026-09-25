"""The once-a-day job the tracker starts at the first frame after midnight (idle priority).

  1. Fetch the pier's conditions (environment.py).
  2. Retrain the camera classifier from the review window's answers (ml/train_classifier.py).
  3. Refit "what brings animals in" (ml/conditions_model.py), once there's enough data.
  4. With --publish: update the README's results and push them (publish_results.py).

Each step runs even if an earlier one fails, and failures go to logs/nightly.log.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

LIVECAMS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(publish: bool):
    handler = logging.handlers.RotatingFileHandler(LIVECAMS / "logs" / "nightly.log", maxBytes=500_000,
                                                   backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(name)s %(message)s")
    log = logging.getLogger("nightly")

    def step(name, fn):
        try:
            fn()
            log.info("%s: done", name)
        except Exception:
            log.exception("%s failed", name)

    import environment
    step("conditions", environment.update)
    from ml import conditions_model, train_classifier
    step("camera classifier", train_classifier.main)
    step("conditions model", conditions_model.main)
    if publish:
        import publish_results
        step("publish", lambda: publish_results.main(update_environment=False))


if __name__ == "__main__":
    main(publish="--publish" in sys.argv)
