import pytest

from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.operations import execute_move_plan, execute_rename_plan


def test_execute_rename_plan_cancelled() -> None:
    with pytest.raises(OperationCancelled):
        execute_rename_plan(
            [],
            should_cancel=lambda: True,
        )


def test_execute_move_plan_cancelled() -> None:
    with pytest.raises(OperationCancelled):
        execute_move_plan(
            [],
            should_cancel=lambda: True,
        )
