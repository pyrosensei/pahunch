import asyncio
import json
import statistics
import sys
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.calls import CallService
from app.config import settings
from app.llm import build_llm_client
from app.planner import PlannerService
from app.schemas import AgentTurnRequest, CustomerTurnRequest, PlanProposal
from app.seed import seed_database

CASES = Path(__file__).with_name("planner_cases.json")


def run_case(case: dict, llm) -> tuple[bool, str, dict]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db)
        calls = CallService(db)
        call = calls.create_call(case["order_id"])
        calls.connect(call.id)
        calls.confirm_context(call.id)

        if "preload_proposal" in case:
            proposal = PlanProposal.model_validate(case["preload_proposal"])
            prepared = calls.submit_turn(
                call.id,
                CustomerTurnRequest(text="preloaded plan", proposal=proposal),
            )
            if prepared.verdict is None or not prepared.verdict.valid:
                raise RuntimeError("preload proposal did not pass policy")

        planner = PlannerService(
            db,
            llm,
            min_confidence=settings.planner_min_confidence,
            min_transcript_confidence=settings.planner_min_transcript_confidence,
        )
        response = asyncio.run(
            planner.handle_turn(
                call.id,
                AgentTurnRequest(text=case["utterance"]),
            )
        )

        failures = []
        actual_action = response.planner.action.value if response.planner.action else None
        if actual_action != case["expected_action"]:
            failures.append(f"action {actual_action} != {case['expected_action']}")
        if response.planner.degraded:
            failures.append(f"degraded: {response.planner.error_code}")
        if "expected_valid" in case:
            actual_valid = (
                response.turn.verdict.valid if response.turn.verdict is not None else None
            )
            if actual_valid != case["expected_valid"]:
                failures.append(
                    f"valid {actual_valid} != {case['expected_valid']}"
                )
        if "expected_confirmed" in case:
            if response.turn.customer_confirmed != case["expected_confirmed"]:
                failures.append(
                    f"confirmed {response.turn.customer_confirmed} "
                    f"!= {case['expected_confirmed']}"
                )
            if response.turn.customer_confirmed:
                committed = calls.commit_plan(call.id, f"planner-eval-{case['id']}")
                if committed.state != "COMMITTED":
                    failures.append(f"commit state {committed.state}")

        metrics = {
            "latency_ms": response.planner.latency_ms,
            "prompt_tokens": response.planner.prompt_tokens or 0,
            "completion_tokens": response.planner.completion_tokens or 0,
        }
        if failures:
            return False, "; ".join(failures), metrics
        return True, actual_action or "no-action", metrics


def main() -> int:
    llm = build_llm_client(settings)
    if llm is None:
        print(
            "Live planner eval skipped: set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL."
        )
        return 2

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = []
    print(f"Model: {llm.model_name}")
    for case in cases:
        try:
            passed, detail, metrics = run_case(case, llm)
        except Exception as error:
            passed, detail = False, f"{type(error).__name__}: {error}"
            metrics = {"latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0}
        print(
            f"{case['id']}: {'PASS' if passed else 'FAIL'} - {detail} "
            f"({metrics['latency_ms']} ms, "
            f"{metrics['prompt_tokens'] + metrics['completion_tokens']} tokens)"
        )
        results.append((passed, metrics))

    passed = sum(1 for item, _ in results if item)
    latencies = [metrics["latency_ms"] for _, metrics in results]
    prompt_tokens = sum(metrics["prompt_tokens"] for _, metrics in results)
    completion_tokens = sum(metrics["completion_tokens"] for _, metrics in results)
    print(f"\n{passed}/{len(cases)} planner cases passed")
    if latencies:
        print(f"Median planner latency: {statistics.median(latencies):.0f} ms")
    print(f"Tokens: {prompt_tokens} prompt, {completion_tokens} completion")
    return int(passed != len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
