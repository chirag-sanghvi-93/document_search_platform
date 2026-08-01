"""Evaluation CLI.

    python -m eval.cli --collection corpus

⚠️ A CLI, not an endpoint. Evaluation is a development activity that drives the
engine directly — the same functions the API calls, without the HTTP layer in
between. Anything measured through the API would also be measuring FastAPI.

Exits non-zero when the run FAILS, which it does for reasons that are not "a
score was low":

  · an agent decision was constant across the whole run  (inert agent)
  · a multi-turn case leaked a fabricated claim          (memory poisoning)
  · a citation marker pointed at no passage              (invented reference)

Those are correctness failures, not quality measurements, and a build should
stop for them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.shared.config import get_settings
from app.shared.prompts import PromptRegistry
from app.shared.store.engine import get_session
from eval import calibrate as calibration_mod
from eval import ragas_metrics
from eval.harness import Report, evaluate

logger = logging.getLogger(__name__)


def render(report: Report) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add("EVALUATION REPORT")
    add("=" * 72)

    # ---- what RAGAs cannot tell you ----------------------------------------
    add("\n## Declining  (the half of the job RAGAs does not score)")
    add(f"{'kind':<16}{'declined':>10}{'  interpretation'}")
    for kind, good in (
        ("answerable", "LOW is good — high means uselessly timid"),
        ("out_of_scope", "HIGH is good — should be 100%"),
        ("near_miss", "HIGH is good — the honest test of the floor"),
    ):
        rate = report.decline_rate(kind)
        add(f"{kind:<16}{rate:>9.0%}   {good}")

    add("\n## Citation validity")
    for key, value in report.citation_validity().items():
        add(f"  {key:<34} {value}")

    add("\n## Decision distributions  (a constant here means an inert agent)")
    for name, counts in report.decision_distributions().items():
        add(f"  {name:<18} {counts}")

    add("\n## Latency (seconds)")
    add(f"  {report.latency_percentiles()}")

    add("\n## Model calls by question kind")
    add(f"  {report.call_efficiency()}")

    add("\n## Multi-turn  (the only construction that detects memory poisoning)")
    for case in report.multi_turn:
        mark = "PASS" if case["passed"] else "FAIL"
        add(f"  [{mark}] {case['case']}")
        if not case["passed"]:
            add(f"         leaked={case['leaked']} unresolved={case['unresolved_references']}")

    add("\n## Per-question")
    header = f"  {'kind':<14}{'intent':<14}{'provenance':<12}"
    add(header + f"{'cites':>6}{'calls':>7}{'sec':>7}  question")
    for outcome in report.outcomes:
        add(
            f"  {outcome.kind:<14}{outcome.intent:<14}{outcome.provenance:<12}"
            f"{outcome.citations:>6}{outcome.model_calls:>7}{outcome.latency_s:>7.1f}  "
            f"{outcome.question[:44]}"
        )

    if report.failures:
        add("\n" + "!" * 72)
        add("RUN FAILED")
        add("!" * 72)
        for failure in report.failures:
            add(f"  · {failure}")
    else:
        add("\nAll correctness assertions passed.")

    return "\n".join(lines)


def to_json(
    report: Report,
    ragas_report: ragas_metrics.RagasReport,
    cal: calibration_mod.Calibration | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "decline_rate": {
            kind: report.decline_rate(kind) for kind in ("answerable", "out_of_scope", "near_miss")
        },
        "citation_validity": report.citation_validity(),
        "decision_distributions": report.decision_distributions(),
        "latency": report.latency_percentiles(),
        "calls_by_kind": report.call_efficiency(),
        "multi_turn": report.multi_turn,
        "ragas": {
            "scores": ragas_report.scores,
            "samples": ragas_report.samples,
            "error": ragas_report.error,
        },
        "failures": report.failures,
        "calibration": (
            {
                "noise_ceiling": cal.noise_ceiling,
                "current_floor": cal.current_floor,
                "fitted_floor": cal.chosen_floor,
                "accuracy_calibration": cal.calibration_accuracy,
                "accuracy_evaluation": cal.evaluation_accuracy,
            }
            if cal
            else None
        ),
        "questions": [vars(o) for o in report.outcomes],
    }


async def _main(collection: str, out: Path | None, skip_ragas: bool = False) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = get_settings()
    prompts = PromptRegistry(settings.phoenix).resolve_all()

    with get_session() as session:
        report = await evaluate(session, settings=settings, prompts=prompts, collection=collection)

    print(render(report))

    # ⚠️ RAGAs runs SECOND and separately. It scores answers that were given;
    # the harness above scores whether answering was right at all. Reporting
    # them together is what stops either being mistaken for the whole picture.
    ragas_report = ragas_metrics.RagasReport(error="skipped")
    if not skip_ragas:
        with get_session() as session:
            samples = await ragas_metrics.collect_samples(
                session, settings=settings, prompts=prompts, collection=collection
            )
        ragas_report = ragas_metrics.score(samples, settings)
    print(ragas_metrics.render(ragas_report))

    # ⚠️ Calibration runs last, because it answers a question the numbers above
    # raise: every decline rate rests on `keep_floor`, which has never been fitted.
    cal = None
    if not skip_ragas:
        with get_session() as session:
            cal = await calibration_mod.calibrate(session, settings=settings, collection=collection)
        print(calibration_mod.render(cal))

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(to_json(report, ragas_report, cal), indent=2, default=str))
        print(f"\nwritten: {out}")

    return 1 if report.failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the read path")
    parser.add_argument("--collection", default="corpus")
    parser.add_argument("--out", type=Path, default=Path("eval/results/latest.json"))
    parser.add_argument(
        "--skip-ragas",
        action="store_true",
        help="Run only the custom harness. RAGAs adds a judging pass per answer.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.collection, args.out, args.skip_ragas)))


if __name__ == "__main__":
    main()
