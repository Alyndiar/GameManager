from __future__ import annotations

from gamemanager.services.gpu_orchestrator import (
    GpuMemoryOrchestrator,
    GpuResourceSpec,
    get_gpu_memory_orchestrator,
    reset_gpu_memory_orchestrator_for_tests,
)


def test_orchestrator_warm_release_and_snapshot() -> None:
    orchestrator = GpuMemoryOrchestrator()
    release_calls: list[dict[str, object]] = []

    def _warm():
        return {"status": "warm"}

    def _release(**kwargs):
        release_calls.append(dict(kwargs))
        return {"status": "released"}

    def _status():
        return "GPU"

    def _state():
        return {"loaded": True}

    orchestrator.register_resource(
        GpuResourceSpec(
            resource_id="r1",
            label="Resource 1",
            warm_fn=_warm,
            release_fn=_release,
            status_fn=_status,
            state_fn=_state,
            estimated_vram_mb=512,
            release_priority=10,
            supports_ram_parking=True,
        )
    )

    warmed = orchestrator.warm(["r1"])
    assert "r1" in warmed["warmed"]
    assert not warmed["errors"]

    released = orchestrator.release(
        ["r1"],
        park_in_ram=True,
        aggressive=True,
        drop_parked=True,
    )
    assert "r1" in released["released"]
    assert not released["errors"]
    assert release_calls and release_calls[-1]["park_in_ram"] is True
    assert release_calls[-1]["aggressive"] is True
    assert release_calls[-1]["drop_parked"] is True

    snapshot = orchestrator.status_snapshot(include_state=True, include_gpu_memory=False)
    assert "resources" in snapshot
    assert snapshot["resources"]["r1"]["status"] == "GPU"
    assert snapshot["resources"]["r1"]["state"] == {"loaded": True}


def test_orchestrator_ensure_free_vram_releases_by_priority() -> None:
    orchestrator = GpuMemoryOrchestrator()
    order: list[str] = []
    calls = {"count": 0}

    def _status():
        return "ok"

    def _mk_release(resource_id: str):
        def _release(**_kwargs):
            order.append(resource_id)
            return {"released": resource_id}

        return _release

    orchestrator.register_resource(
        GpuResourceSpec(
            resource_id="heavy",
            label="Heavy",
            warm_fn=lambda: "ok",
            release_fn=_mk_release("heavy"),
            status_fn=_status,
            release_priority=100,
        )
    )
    orchestrator.register_resource(
        GpuResourceSpec(
            resource_id="light",
            label="Light",
            warm_fn=lambda: "ok",
            release_fn=_mk_release("light"),
            status_fn=_status,
            release_priority=10,
        )
    )

    def _fake_query():
        calls["count"] += 1
        if calls["count"] == 1:
            return {"total_mb": 12000, "used_mb": 11500, "free_mb": 500}
        if calls["count"] == 2:
            return {"total_mb": 12000, "used_mb": 9500, "free_mb": 2500}
        return {"total_mb": 12000, "used_mb": 9500, "free_mb": 2500}

    orchestrator.query_gpu_memory_mb = _fake_query  # type: ignore[method-assign]
    outcome = orchestrator.ensure_free_vram(2000, park_in_ram=True, aggressive=False)
    assert outcome["satisfied"] is True
    assert outcome["released_resource_ids"] == ["heavy"]
    assert order == ["heavy"]


def test_default_orchestrator_registration() -> None:
    reset_gpu_memory_orchestrator_for_tests()
    orchestrator = get_gpu_memory_orchestrator()
    ids = [spec.resource_id for spec in orchestrator.list_resources()]
    assert "background_stack" in ids
    assert "text_stack" in ids
    assert "torch_runtime" in ids
