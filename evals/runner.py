import json
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.calls import CallService
from app.models import CallState
from app.schemas import CustomerTurnRequest, PlanProposal
from app.seed import seed_database

SCENARIOS = Path(__file__).with_name("scenarios.json")


def run_scenario(scenario: dict) -> tuple[bool, str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db)
        service = CallService(db)
        call = service.create_call(scenario["order_id"])
        service.connect(call.id)
        service.confirm_context(call.id)
        proposal = PlanProposal.model_validate(scenario["proposal"])
        result = service.submit_turn(
            call.id,
            CustomerTurnRequest(text=f"Scenario {scenario['id']}", proposal=proposal),
        )
        actual_valid = result.verdict.valid if result.verdict else False
        if actual_valid != scenario["expected_valid"]:
            return False, f"expected valid={scenario['expected_valid']}, got {actual_valid}"

        if actual_valid:
            service.submit_turn(
                call.id,
                CustomerTurnRequest(text="confirm", confirmation=True),
            )
            commit = service.commit_plan(call.id, f"eval-{scenario['id']}-commit")
            if commit.state != CallState.COMMITTED.value:
                return False, f"expected COMMITTED, got {commit.state}"
        elif result.state != CallState.NEGOTIATION.value:
            return False, f"expected NEGOTIATION, got {result.state}"

        return True, result.state


def main() -> int:
    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    failures = 0
    for scenario in scenarios:
        try:
            passed, detail = run_scenario(scenario)
        except Exception as error:
            passed, detail = False, f"{type(error).__name__}: {error}"
        print(f"{scenario['id']}: {'PASS' if passed else 'FAIL'} — {detail}")
        failures += not passed
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} scenarios passed")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
